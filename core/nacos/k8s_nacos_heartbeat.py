"""
K8s 容器 Nacos 心跳维持模块

与 k8s_nacos_register.py 配合，定时向 Nacos 发送心跳，防止 k8s 容器实例
因心跳超时被 Nacos 剔除。

流程：
    1. 通过 kubectl 发现所有 Running 状态的 Pod（缓存，5 分钟刷新一次）
    2. 解析 serviceName 并构造心跳 payload
    3. PUT /nacos/v1/ns/instance/beat 并发发送心跳
"""

import concurrent.futures
import json
import logging
import time
from typing import Any, Dict, List

from core.nacos.k8s_nacos_common import (
    _CONFIG,
    RateLimiter,
    discover_k8s_containers,
    get_session,
    is_token_invalid,
    nacos_login,
    resolve_service_name,
)

_LOG = "k8s_nacos_heartbeat"

DEFAULT_INTERVAL = 10
DISCOVERY_INTERVAL = 300        # Pod 发现间隔（秒），默认 5 分钟。稳定集群 Pod 变化少，无需每轮心跳都 kubectl
FORCE_DISCOVERY_FAIL_RATIO = 0.3  # 心跳失败比例超过此值自动强制重新发现 Pod
MAX_HEARTBEAT_WORKERS = 20      # 并发心跳 worker 数（300 Pod 时约 4.5s 完成一轮）
HEARTBEAT_MAX_RETRIES = 3       # 心跳失败最大重试次数
# 指数退避: retry_1=1s, retry_2=3s → 总延迟 4s（之前固定5s×2=10s）
HEARTBEAT_RETRY_BACKOFF = (1, 3)

HEARTBEAT_TIMEOUT = (3, 5)      # 心跳：3s 连接, 5s 读取
# 提交限速：防止 1000+ Pod 时瞬间涌入 Nacos（20 worker × 200/s 提交 = 稳态 ~100 req/s）
HEARTBEAT_SUBMIT_RATE = 200    # 每秒最多提交数，1000 实例约 5s 完成提交

# ── Pod 列表缓存（避免每轮心跳都 kubectl discover）──
_PODS_CACHE: List[Dict[str, Any]] = []
_PODS_CACHE_TIME: float = 0.0

# ── beat JSON 缓存（避免每轮对不变的数据重复 json.dumps）──
# key: namespace|pod_name|pod_ip|service_name|port|container_name|image|app_label
# 包含 image/app_label，确保 Deployment 升级或 metadata 变化时 cache 自动失效
_BEAT_JSON_CACHE: Dict[str, str] = {}


# =============================================================================
# 配置读取
# =============================================================================

def _get_nacos_config() -> Dict[str, Any]:
    """从 config.yaml 获取 Nacos 配置"""
    nacos_cfg = _CONFIG.get("nacos", {}) if isinstance(_CONFIG, dict) else {}
    if not isinstance(nacos_cfg, dict):
        nacos_cfg = {}

    # 地址
    from core.nacos.k8s_nacos_common import get_nacos_address
    address = get_nacos_address()

    # 心跳 API 路径
    api_cfg = nacos_cfg.get("api", {}) if isinstance(nacos_cfg.get("api"), dict) else {}
    heartbeat_api = str(
        api_cfg.get("heartbeat", "/nacos/v1/ns/instance/beat") or
        "/nacos/v1/ns/instance/beat"
    ).strip()

    return {
        "address": address,
        "heartbeat_api": heartbeat_api,
        "group_name": str(nacos_cfg.get("group_name", "DEFAULT_GROUP") or "DEFAULT_GROUP").strip(),
        "namespace_id": str(nacos_cfg.get("namespace_id", "") or "").strip(),
        "username": nacos_cfg.get("username"),
        "password": nacos_cfg.get("password"),
        "request_timeout": int(nacos_cfg.get("request_timeout", 10)),
    }


# =============================================================================
# 心跳发送
# =============================================================================

def _do_single_beat(task: Dict[str, Any], nacos_cfg: Dict[str, Any]) -> bool:
    """发送单个心跳（供线程池 worker 调用），支持重试和 token 自动刷新。
    返回 True 表示成功。"""
    url = task["url"]
    payload = dict(task["payload"])  # 浅拷贝，允许 token 更新
    service_name = task["service_name"]
    pod_namespace = task.get("pod_namespace", "")
    pod_name = task.get("pod_name", "")

    for retry in range(HEARTBEAT_MAX_RETRIES):
        try:
            resp = get_session(MAX_HEARTBEAT_WORKERS).put(url, params=payload, timeout=HEARTBEAT_TIMEOUT)
            if resp.status_code == 200:
                return True

            body_text = resp.text[:300]

            # 401/403 token 失效 → 强制重新登录后重试
            if resp.status_code in (401, 403) and is_token_invalid(body_text):
                if retry < HEARTBEAT_MAX_RETRIES - 1:
                    logging.warning(
                        "[%s] 心跳时 Token 失效(%s), 重新登录重试: service=%s",
                        _LOG, resp.status_code, service_name,
                    )
                    new_token = nacos_login(
                        _LOG,
                        nacos_cfg.get("username"),
                        nacos_cfg.get("password"),
                        nacos_cfg.get("address", ""),
                        force_refresh=True,
                    )
                    if new_token:
                        payload["accessToken"] = new_token
                    time.sleep(1)
                    continue

            # 503 Distro snapshot load failed → Nacos 服务端还在初始化，退避重试
            if resp.status_code == 503 and "Distro snapshot load failed" in body_text:
                if retry < len(HEARTBEAT_RETRY_BACKOFF):
                    time.sleep(HEARTBEAT_RETRY_BACKOFF[retry])
                continue

            logging.warning(
                "[%s] 心跳失败(%s): service=%s, pod=%s/%s, %s",
                _LOG, resp.status_code, service_name,
                pod_namespace, pod_name, body_text,
            )
            return False
        except Exception as e:
            if retry < HEARTBEAT_MAX_RETRIES - 1:
                delay = HEARTBEAT_RETRY_BACKOFF[min(retry, len(HEARTBEAT_RETRY_BACKOFF) - 1)]
                time.sleep(delay)
                continue
            logging.warning(
                "[%s] 心跳异常(重试耗尽): service=%s, pod=%s/%s, error=%s",
                _LOG, service_name, pod_namespace, pod_name, e,
            )
            return False
    return False


def _get_pods(force: bool = False) -> List[Dict[str, Any]]:
    """获取 Pod 列表，优先使用缓存。force=True 时强制重新发现。

    策略：
    - Pod 列表变化缓慢，心跳频率高（10~30s），无需每轮都 kubectl
    - 默认每 DISCOVERY_INTERVAL 秒重新发现一次
    - 心跳失败过多时也会自动强制重新发现（Pod 可能已被删除）
    """
    global _PODS_CACHE, _PODS_CACHE_TIME
    now = time.time()
    if not force and _PODS_CACHE and (now - _PODS_CACHE_TIME) < DISCOVERY_INTERVAL:
        return _PODS_CACHE

    pods = discover_k8s_containers(_LOG)
    _PODS_CACHE = pods
    _PODS_CACHE_TIME = now

    # 修剪 _BEAT_JSON_CACHE：只保留当前存活 Pod 的条目，避免无限制增长
    valid_keys = set()
    for pod in pods:
        svc_base = resolve_service_name(pod)
        labels = pod.get("labels", {})
        app_label = labels.get("app", "")
        for c in pod.get("containers", []):
            container_name = c.get("name", "")
            image = c.get("image", "")
            slen = svc_base
            if len(pod.get("containers", [])) > 1 and container_name:
                slen = f"{svc_base}-{container_name}"
            for port in (c.get("ports") or []):
                valid_keys.add(
                    f"{pod['namespace']}|{pod.get('pod_name','')}|{pod['pod_ip']}"
                    f"|{slen}|{port}|{container_name}|{image}|{app_label}"
                )
    stale = [k for k in _BEAT_JSON_CACHE if k not in valid_keys]
    for k in stale:
        del _BEAT_JSON_CACHE[k]

    logging.info(
        "[%s] Pod 发现完成: 共 %d 个 Pod%s, beat_cache=%d, 淘汰=%d, 缓存有效期 %d 秒",
        _LOG, len(pods),
        " (强制刷新)" if force else "",
        len(_BEAT_JSON_CACHE), len(stale),
        DISCOVERY_INTERVAL,
    )
    return pods


def send_heartbeats() -> Dict[str, Any]:
    """
    发现 k8s 运行容器并并发向 Nacos 发送心跳。
    预计算阶段（单线程）构建所有任务 → 线程池并发发送。

    Pod 发现采用缓存策略：
    - 默认每 DISCOVERY_INTERVAL 秒重新发现一次 Pod 列表
    - 心跳发送后若失败比例过高，自动触发下一轮强制重新发现

    返回:
        {"success": bool, "total_pods": N, "total_instances": M,
         "successful": X, "failed": Y}
    """
    pods = _get_pods()
    if not pods:
        logging.debug("[%s] 无运行中的 Pod，跳过心跳", _LOG)
        return {
            "success": True,
            "total_pods": 0,
            "total_instances": 0,
            "successful": 0,
            "failed": 0,
        }

    nacos_cfg = _get_nacos_config()
    nacos_address = nacos_cfg.get("address", "")
    if not nacos_address:
        logging.warning("[%s] Nacos 地址未配置，跳过心跳", _LOG)
        return {"success": False, "total_pods": 0, "total_instances": 0,
                "successful": 0, "failed": 0}

    heartbeat_url = f"{nacos_address.rstrip('/')}{nacos_cfg['heartbeat_api']}"
    group_name = nacos_cfg["group_name"]
    namespace_id = nacos_cfg["namespace_id"]
    username = nacos_cfg.get("username")
    password = nacos_cfg.get("password")

    # Nacos 认证: 先登录获取 accessToken（token 有缓存，仅首次或过期时真正请求）
    access_token = nacos_login(_LOG, username, password, nacos_address)
    if not access_token:
        logging.warning("[%s] 获取 accessToken 失败或为空，心跳请求将不携带认证", _LOG)

    # ── 阶段 1：预计算所有心跳任务（单线程，填充缓存，避免并发写）──
    tasks: List[Dict[str, Any]] = []
    for pod in pods:
        pod_ip = pod["pod_ip"]
        pod_name = pod.get("pod_name", "")
        pod_namespace = pod.get("namespace", "")
        service_name_base = resolve_service_name(pod)
        labels = pod.get("labels", {})

        for container in pod.get("containers", []):
            container_name = container.get("name", "")
            ports = container.get("ports", [])
            image = container.get("image", "")

            if len(pod.get("containers", [])) > 1 and container_name:
                service_name = f"{service_name_base}-{container_name}"
            else:
                service_name = service_name_base

            if not ports:
                continue

            # 构建 metadata（复用缓存，填充 _BEAT_JSON_CACHE）
            for port in ports:
                app_label = labels.get("app", "")
                beat_key = (
                    f"{pod_namespace}|{pod_name}|{pod_ip}|{service_name}|{port}"
                    f"|{container_name}|{image}|{app_label}"
                )
                beat_json = _BEAT_JSON_CACHE.get(beat_key)
                if beat_json is None:
                    metadata = {
                        "namespace": pod_namespace,
                        "pod_name": pod_name,
                        "container_name": container_name,
                        "image": str(image),
                        "source": "k8s-discovery",
                        "app": labels.get("app", ""),
                    }
                    metadata = {k: v for k, v in metadata.items() if v}
                    beat_info = {
                        "ip": pod_ip,
                        "port": int(port),
                        "serviceName": service_name,
                        "cluster": "DEFAULT",
                        "weight": 1.0,
                        "healthy": True,
                        "metadata": metadata,
                        "scheduled": True,
                    }
                    beat_json = json.dumps(beat_info, ensure_ascii=False)
                    _BEAT_JSON_CACHE[beat_key] = beat_json

                payload = {
                    "serviceName": service_name,
                    "groupName": group_name,
                    "clusterName": "DEFAULT",
                    "ephemeral": "true",
                    "beat": beat_json,
                }
                if namespace_id:
                    payload["namespaceId"] = namespace_id
                if access_token:
                    payload["accessToken"] = access_token

                tasks.append({
                    "url": heartbeat_url,
                    "payload": payload,
                    "service_name": service_name,
                    "pod_namespace": pod_namespace,
                    "pod_name": pod_name,
                })

    if not tasks:
        logging.debug("[%s] 无心跳任务", _LOG)
        return {"success": True, "total_pods": len(pods), "total_instances": 0,
                "successful": 0, "failed": 0}

    # ── 阶段 2：线程池并发发送心跳（RateLimiter 防瞬间涌入）──
    total_successful = 0
    total_failed = 0
    limiter = RateLimiter(HEARTBEAT_SUBMIT_RATE)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_HEARTBEAT_WORKERS) as executor:
        future_to_task = {}
        for task in tasks:
            limiter.acquire()
            future_to_task[executor.submit(_do_single_beat, task, nacos_cfg)] = task
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]
            try:
                if future.result():
                    total_successful += 1
                else:
                    total_failed += 1
            except Exception as e:
                logging.warning(
                    "[%s] 心跳线程异常: service=%s, pod=%s/%s, error=%s",
                    _LOG, task["service_name"], task["pod_namespace"], task["pod_name"], e,
                )
                total_failed += 1

    logging.debug(
        "[%s] 心跳完成: pods=%d, 实例=%d, 成功=%d, 失败=%d",
        _LOG, len(pods), total_successful + total_failed, total_successful, total_failed,
    )

    # 心跳失败比例过高 → 可能有不少 Pod 已被删除，下一轮强制重新发现
    total = total_successful + total_failed
    if total > 0 and total_failed / total >= FORCE_DISCOVERY_FAIL_RATIO:
        global _PODS_CACHE_TIME
        _PODS_CACHE_TIME = 0.0  # 使缓存失效，下一轮 _get_pods() 会强制重新发现
        logging.info(
            "[%s] 心跳失败率 %.1f%% 超过阈值 %.0f%%，下一轮将强制重新发现 Pod",
            _LOG, total_failed / total * 100, FORCE_DISCOVERY_FAIL_RATIO * 100,
        )

    return {
        "success": total_failed == 0,
        "total_pods": len(pods),
        "total_instances": total,
        "successful": total_successful,
        "failed": total_failed,
    }


# =============================================================================
# 主循环
# =============================================================================

def k8s_nacos_heartbeat_loop(interval: int = DEFAULT_INTERVAL):
    """
    K8s 容器 Nacos 心跳主循环（后台线程入口）。

    参数:
        interval: 心跳间隔（秒），默认 10 秒
    """
    logging.info("[%s] K8s Nacos 心跳循环启动，间隔 %d 秒", _LOG, interval)
    while True:
        try:
            result = send_heartbeats()
            if result["failed"] > 0:
                logging.warning(
                    "[%s] 部分心跳失败: pods=%d, 成功=%d, 失败=%d",
                    _LOG, result["total_pods"], result["successful"], result["failed"],
                )
        except Exception as e:
            logging.error("[%s] 心跳循环异常: %s", _LOG, e, exc_info=True)
        time.sleep(interval)


# =============================================================================
# 自测入口
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = send_heartbeats()
    print(f"\n{'=' * 60}")
    print(f"  K8s 容器 Nacos 心跳结果")
    print(f"{'=' * 60}")
    print(f"  Pod 总数   : {result['total_pods']}")
    print(f"  实例总数   : {result['total_instances']}")
    print(f"  心跳成功   : {result['successful']}")
    print(f"  心跳失败   : {result['failed']}")
    print(f"{'=' * 60}")
