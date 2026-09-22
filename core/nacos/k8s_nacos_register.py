"""
K8s 容器发现与 Nacos 注册模块

轮询本机 k8s 所有命名空间中的运行容器，自动注册到 Nacos。
Nacos 已部署在 k8s 集群内，通过 Service 地址访问。
"""

import concurrent.futures
import json
import logging
import threading
import time
from typing import Any, Dict, List

from core.nacos.k8s_nacos_common import (
    _CONFIG,
    RateLimiter,
    discover_k8s_containers,
    get_nacos_address,
    get_session,
    is_token_invalid,
    nacos_login,
    resolve_service_name,
)

_LOG = "k8s_nacos_register"

DEFAULT_INTERVAL = 30
MAX_REGISTER_WORKERS = 10       # 并发注册 worker 数
REGISTER_MAX_RETRIES = 3       # 注册失败最大重试次数
# 指数退避: retry_1=2s, retry_2=5s → 总延迟 7s（之前固定5s×2=10s）
REGISTER_RETRY_BACKOFF = (2, 5)

REGISTER_TIMEOUT = (5, 10)     # 注册：5s 连接, 10s 读取
DEREGISTER_TIMEOUT = (3, 5)    # 注销：3s 连接, 5s 读取
# 提交限速：注册比心跳重（POST + 更长 timeout），用更低速率
REGISTER_SUBMIT_RATE = 50      # 每秒最多提交数

# ── 已注册实例缓存（避免每轮全量 POST，支持注销）──
# key 格式: "namespace|pod_name|pod_ip|service_name|port"
# value: {"service_name": str, "pod_ip": str, "port": int,
#          "group_name": str, "namespace_id": str}
_REGISTERED_CACHE: Dict[str, Dict[str, Any]] = {}

# ── Nacos 注销 API 路径 ──
_NACOS_DEREGISTER_API = "/nacos/v1/ns/instance"

# ── metadata JSON 缓存（避免每轮对不变的数据重复 json.dumps）──
# key: 实例 key，value: 序列化后的 JSON 字符串
# 线程安全：由 ThreadPool 多个 worker 并发访问，_METADATA_CACHE_LOCK 保护
_METADATA_JSON_CACHE: Dict[str, str] = {}
_METADATA_CACHE_LOCK = threading.Lock()


# =============================================================================
# Nacos 注册
# =============================================================================

def register_to_nacos(pod_info: Dict[str, Any]) -> Dict[str, int]:
    """
    将一个 Pod 的所有容器端口注册到 Nacos。

    通过 POST /nacos/v1/ns/instance 逐个注册容器端口。

    参数:
        pod_info: discover_k8s_containers() 返回的单个 Pod 信息

    返回:
        {"successful": N, "failed": N}
    """
    nacos_address = get_nacos_address()
    if not nacos_address:
        logging.warning("[%s] Nacos 地址未配置", _LOG)
        return {"successful": 0, "failed": 0}

    nacos_cfg = _CONFIG.get("nacos", {})
    if not isinstance(nacos_cfg, dict):
        nacos_cfg = {}

    register_api = str(
        nacos_cfg.get("api", {}).get("register", "/nacos/v1/ns/instance")
        if isinstance(nacos_cfg.get("api"), dict)
        else "/nacos/v1/ns/instance"
    )
    register_url = f"{nacos_address.rstrip('/')}{register_api}"

    group_name = str(nacos_cfg.get("group_name", "DEFAULT_GROUP") or "DEFAULT_GROUP").strip()
    namespace_id = str(nacos_cfg.get("namespace_id", "") or "").strip()
    username = nacos_cfg.get("username")
    password = nacos_cfg.get("password")

    # Nacos 认证: 先登录获取 accessToken
    access_token = nacos_login(_LOG, username, password, nacos_address)
    if not access_token:
        logging.warning("[%s] 获取 accessToken 失败或为空，注册请求将不携带认证", _LOG)

    pod_ip = pod_info["pod_ip"]
    pod_name = pod_info.get("pod_name", "")
    pod_namespace = pod_info.get("namespace", "")
    service_name_base = resolve_service_name(pod_info)
    labels = pod_info.get("labels", {})

    session = get_session(MAX_REGISTER_WORKERS)

    successful = 0
    failed = 0

    for container in pod_info.get("containers", []):
        container_name = container.get("name", "")
        ports = container.get("ports", [])
        image = container.get("image", "")

        # 多容器时用 <service>-<container> 区分
        if len(pod_info.get("containers", [])) > 1 and container_name:
            service_name = f"{service_name_base}-{container_name}"
        else:
            service_name = service_name_base

        if not ports:
            logging.debug(
                "[%s] 容器 %s/%s 无端口声明，跳过",
                _LOG, pod_name, container_name,
            )
            continue

        for port in ports:
            # 缓存 key: namespace|pod_name|container_name（同一实例 metadata 不变）
            meta_key = f"{pod_namespace}|{pod_name}|{container_name}"

            with _METADATA_CACHE_LOCK:
                metadata_json = _METADATA_JSON_CACHE.get(meta_key)

            if metadata_json is None:
                metadata = {
                    "namespace": pod_namespace,
                    "pod_name": pod_name,
                    "container_name": container_name,
                    "image": str(image),
                    "source": "k8s-discovery",
                    "app": labels.get("app", ""),
                }
                metadata = {k: v for k, v in metadata.items() if v}
                metadata_json = json.dumps(metadata, ensure_ascii=False)
                with _METADATA_CACHE_LOCK:
                    _METADATA_JSON_CACHE[meta_key] = metadata_json

            params = {
                "serviceName": service_name,
                "groupName": group_name,
                "ip": pod_ip,
                "port": int(port),
                "clusterName": "DEFAULT",
                "weight": 1.0,
                "healthy": True,
                "enabled": True,
                "ephemeral": "true",
                "metadata": metadata_json,
            }
            if namespace_id:
                params["namespaceId"] = namespace_id
            if access_token:
                params["accessToken"] = access_token

            reg_ok = False
            for retry in range(REGISTER_MAX_RETRIES):
                try:
                    resp = session.post(register_url, params=params, timeout=REGISTER_TIMEOUT)
                    if resp.status_code == 200:
                        reg_ok = True
                        break

                    body_text = resp.text[:300]

                    # 401/403 token 失效 → 强制重新登录后重试
                    if resp.status_code in (401, 403):
                        if is_token_invalid(body_text) and retry < REGISTER_MAX_RETRIES - 1:
                            logging.warning(
                                "[%s] Token 失效(%s), 重新登录并重试(%d/%d): service=%s, %s",
                                _LOG, resp.status_code, retry + 2, REGISTER_MAX_RETRIES,
                                service_name, body_text[:120],
                            )
                            new_token = nacos_login(
                                _LOG, username, password, nacos_address, force_refresh=True,
                            )
                            if new_token:
                                params["accessToken"] = new_token
                                access_token = new_token
                            time.sleep(1)
                            continue

                    # 503 Distro snapshot load failed → Nacos 服务端还在初始化，退避重试
                    if resp.status_code == 503 and "Distro snapshot load failed" in body_text:
                        logging.debug(
                            "[%s] Nacos 命名模块初始化中, 第%d次重试: service=%s, pod=%s/%s",
                            _LOG, retry + 1, service_name, pod_namespace, pod_name,
                        )
                        if retry < len(REGISTER_RETRY_BACKOFF):
                            time.sleep(REGISTER_RETRY_BACKOFF[retry])
                        continue

                    logging.warning(
                        "[%s]  注册失败(%s): service=%s, pod=%s/%s, %s",
                        _LOG, resp.status_code, service_name,
                        pod_namespace, pod_name, body_text,
                    )
                    break
                except Exception as e:
                    logging.debug(
                        "[%s] 注册网络异常, 第%d次重试: service=%s, error=%s",
                        _LOG, retry + 1, service_name, e,
                    )
                    if retry < REGISTER_MAX_RETRIES - 1:
                        delay = REGISTER_RETRY_BACKOFF[min(retry, len(REGISTER_RETRY_BACKOFF) - 1)]
                        time.sleep(delay)
                    else:
                        logging.warning(
                            "[%s]  注册异常(重试%d次后仍失败): service=%s, pod=%s/%s, error=%s",
                            _LOG, REGISTER_MAX_RETRIES, service_name,
                            pod_namespace, pod_name, e,
                        )

            if reg_ok:
                logging.info(
                    "[%s]  注册成功: service=%s, ip=%s:%s, pod=%s/%s",
                    _LOG, service_name, pod_ip, port, pod_namespace, pod_name,
                )
                successful += 1
            else:
                failed += 1

    return {"successful": successful, "failed": failed}


# =============================================================================
# Nacos 注销
# =============================================================================

def _deregister_instances(removed_keys: set, removed_info: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """
    批量注销已下线的实例。

    参数:
        removed_keys: 需要注销的实例 key 集合
        removed_info: {key: {service_name, pod_ip, port, group_name, namespace_id}, ...}

    返回:
        {"successful": N, "failed": N}
    """
    if not removed_keys:
        return {"successful": 0, "failed": 0}

    nacos_address = get_nacos_address()
    if not nacos_address:
        logging.warning("[%s] Nacos 地址未配置，跳过注销", _LOG)
        return {"successful": 0, "failed": 0}

    nacos_cfg = _CONFIG.get("nacos", {})
    username = nacos_cfg.get("username") if isinstance(nacos_cfg, dict) else None
    password = nacos_cfg.get("password") if isinstance(nacos_cfg, dict) else None

    deregister_url = f"{nacos_address.rstrip('/')}{_NACOS_DEREGISTER_API}"
    access_token = nacos_login(_LOG, username, password, nacos_address)

    session = get_session(MAX_REGISTER_WORKERS)
    successful = 0
    failed = 0

    for key in removed_keys:
        info = removed_info.get(key, {})
        if not info:
            failed += 1
            continue

        service_name = info.get("service_name", "")
        pod_ip = info.get("pod_ip", "")
        port = info.get("port", 0)
        group_name = info.get("group_name", "DEFAULT_GROUP")
        namespace_id = info.get("namespace_id", "")

        params = {
            "serviceName": service_name,
            "ip": pod_ip,
            "port": int(port),
            "groupName": group_name,
            "ephemeral": "true",
        }
        if namespace_id:
            params["namespaceId"] = namespace_id
        if access_token:
            params["accessToken"] = access_token

        dereg_ok = False
        for retry in range(REGISTER_MAX_RETRIES):
            try:
                resp = session.delete(deregister_url, params=params, timeout=DEREGISTER_TIMEOUT)
                if resp.status_code == 200:
                    dereg_ok = True
                    break

                body_text = resp.text[:300]

                # 401/403 token 失效 → 强制重新登录后重试
                if resp.status_code in (401, 403) and is_token_invalid(body_text):
                    if retry < REGISTER_MAX_RETRIES - 1:
                        logging.warning(
                            "[%s] 注销时 Token 失效(%s), 重新登录重试: service=%s",
                            _LOG, resp.status_code, service_name,
                        )
                        new_token = nacos_login(
                            _LOG, username, password, nacos_address, force_refresh=True,
                        )
                        if new_token:
                            params["accessToken"] = new_token
                            access_token = new_token
                        time.sleep(1)
                        continue

                logging.warning(
                    "[%s]  注销失败(%s): service=%s, ip=%s:%s, %s",
                    _LOG, resp.status_code, service_name, pod_ip, port,
                    body_text,
                )
                break
            except Exception as e:
                if retry < REGISTER_MAX_RETRIES - 1:
                    logging.debug(
                        "[%s] 注销网络异常, 第%d次重试: service=%s, error=%s",
                        _LOG, retry + 1, service_name, e,
                    )
                    delay = REGISTER_RETRY_BACKOFF[min(retry, len(REGISTER_RETRY_BACKOFF) - 1)]
                    time.sleep(delay)
                    continue
                logging.warning(
                    "[%s]  注销异常: service=%s, ip=%s:%s, error=%s",
                    _LOG, service_name, pod_ip, port, e,
                )
                break

        if dereg_ok:
            logging.info(
                "[%s]  注销成功: service=%s, ip=%s:%s",
                _LOG, service_name, pod_ip, port,
            )
            successful += 1
        else:
            failed += 1

    return {"successful": successful, "failed": failed}


# =============================================================================
# 统一入口
# =============================================================================

def _build_instance_key(namespace: str, pod_name: str, pod_ip: str,
                        service_name: str, port: int) -> str:
    """构建实例唯一标识键，用于缓存比对"""
    return f"{namespace}|{pod_name}|{pod_ip}|{service_name}|{port}"


def _get_nacos_group_and_namespace() -> tuple:
    """获取 Nacos 注册用的 groupName 和 namespaceId"""
    nacos_cfg = _CONFIG.get("nacos", {})
    if not isinstance(nacos_cfg, dict):
        nacos_cfg = {}
    group_name = str(nacos_cfg.get("group_name", "DEFAULT_GROUP") or "DEFAULT_GROUP").strip()
    namespace_id = str(nacos_cfg.get("namespace_id", "") or "").strip()
    return group_name, namespace_id


def discover_and_register() -> Dict[str, Any]:
    """
    发现 k8s 运行容器并注册到 Nacos（通过缓存避免重复注册）。

    只对新增/变更的实例发起 Nacos 注册请求，未变化的实例直接跳过。
    已下线的实例会主动调用 Nacos 注销 API 移除。

    返回:
        {
            "success": bool,
            "total_pods": N,
            "total_instances": M,
            "successful": X,      # 注册成功数
            "failed": Y,          # 注册失败数
            "dereg_ok": A,        # 注销成功数
            "dereg_fail": B,      # 注销失败数
            "skipped": Z,         # 未变化的缓存数
            "removed": W,         # 本轮的注销总数
        }
    """
    global _REGISTERED_CACHE

    group_name, namespace_id = _get_nacos_group_and_namespace()

    pods = discover_k8s_containers(_LOG, skip_host_network=True)
    if not pods:
        # 所有 Pod 都没了 → 注销全部已缓存实例
        removed = len(_REGISTERED_CACHE)
        if removed > 0:
            removed_info = dict(_REGISTERED_CACHE)
            logging.info("[%s] 无运行中的 Pod，注销全部 %d 个缓存实例", _LOG, removed)
            dereg_result = _deregister_instances(
                set(removed_info.keys()), removed_info,
            )
            _REGISTERED_CACHE.clear()
        else:
            dereg_result = {"successful": 0, "failed": 0}
        return {
            "success": True,
            "total_pods": 0,
            "total_instances": 0,
            "successful": 0,
            "failed": 0,
            "dereg_ok": dereg_result["successful"],
            "dereg_fail": dereg_result["failed"],
            "skipped": 0,
            "removed": removed,
        }

    # ── 1. 构建当前所有实例 key → Pod 映射 ──
    current_keys: Dict[str, Dict[str, Any]] = {}
    for pod in pods:
        pod_namespace = pod.get("namespace", "")
        pod_name = pod.get("pod_name", "")
        pod_ip = pod.get("pod_ip", "")
        service_name_base = resolve_service_name(pod)
        containers = pod.get("containers", [])

        for container in containers:
            container_name = container.get("name", "")
            ports = container.get("ports", [])

            if len(containers) > 1 and container_name:
                service_name = f"{service_name_base}-{container_name}"
            else:
                service_name = service_name_base

            for port in ports:
                key = _build_instance_key(
                    pod_namespace, pod_name, pod_ip, service_name, port,
                )
                current_keys[key] = pod

    # ── 2. 计算差异 ──
    new_keys = set(current_keys.keys()) - set(_REGISTERED_CACHE.keys())
    removed_keys = set(_REGISTERED_CACHE.keys()) - set(current_keys.keys())
    unchanged_count = len(_REGISTERED_CACHE) - len(removed_keys)

    # ── 3. 注销已下线实例 ──
    dereg_ok = 0
    dereg_fail = 0
    if removed_keys:
        removed_info = {k: _REGISTERED_CACHE[k] for k in removed_keys}
        logging.info(
            "[%s] %d 个实例已下线，正在注销: %s",
            _LOG, len(removed_keys),
            ", ".join(sorted(removed_keys)[:5])
            + ("..." if len(removed_keys) > 5 else ""),
        )
        dereg_result = _deregister_instances(removed_keys, removed_info)
        dereg_ok = dereg_result["successful"]
        dereg_fail = dereg_result["failed"]
        for key in removed_keys:
            _REGISTERED_CACHE.pop(key, None)

    # ── 4. 只注册新/变更的实例 ──
    if not new_keys:
        logging.debug(
            "[%s] 无变化, 跳过注册: pods=%d, 已缓存=%d, 注销=%d",
            _LOG, len(pods), len(_REGISTERED_CACHE), len(removed_keys),
        )
        return {
            "success": True,
            "total_pods": len(pods),
            "total_instances": len(current_keys),
            "successful": 0,
            "failed": 0,
            "dereg_ok": dereg_ok,
            "dereg_fail": dereg_fail,
            "skipped": unchanged_count,
            "removed": len(removed_keys),
        }

    # 去重：同一个 Pod 可能产生多个 key，对每个 Pod 只注册一次
    pending_pods: Dict[int, Dict[str, Any]] = {}
    for key in new_keys:
        pod = current_keys[key]
        pod_id = id(pod)
        if pod_id not in pending_pods:
            pending_pods[pod_id] = pod

    logging.info(
        "[%s] 发现 %d 个需注册的 Pod（%d 个新实例），已缓存 %d，已下线 %d",
        _LOG, len(pending_pods), len(new_keys),
        unchanged_count, len(removed_keys),
    )

    # ── 5. 并发注册新实例（RateLimiter 防瞬间涌入）──
    total_successful = 0
    total_failed = 0
    limiter = RateLimiter(REGISTER_SUBMIT_RATE)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_REGISTER_WORKERS) as executor:
        future_to_pod = {}
        for pod in pending_pods.values():
            limiter.acquire()
            future_to_pod[executor.submit(register_to_nacos, pod)] = pod
        for future in concurrent.futures.as_completed(future_to_pod):
            try:
                result = future.result()
                total_successful += result["successful"]
                total_failed += result["failed"]
            except Exception as e:
                logging.warning("[%s] 注册异常(线程): %s", _LOG, e)
                total_failed += 1

    # ── 6. 更新缓存：新实例写入缓存，含注销所需信息 ──
    for key in new_keys:
        pod = current_keys[key]
        pod_namespace = pod.get("namespace", "")
        pod_name = pod.get("pod_name", "")
        pod_ip = pod.get("pod_ip", "")
        service_name_base = resolve_service_name(pod)
        containers = pod.get("containers", [])
        for container in containers:
            container_name = container.get("name", "")
            ports = container.get("ports", [])
            svc = f"{service_name_base}-{container_name}" if len(containers) > 1 and container_name else service_name_base
            for port in ports:
                k = _build_instance_key(pod_namespace, pod_name, pod_ip, svc, port)
                if k == key:
                    _REGISTERED_CACHE[key] = {
                        "service_name": svc,
                        "pod_ip": pod_ip,
                        "port": port,
                        "group_name": group_name,
                        "namespace_id": namespace_id,
                    }
                    break

    logging.info(
        "[%s] 注册完成: 新注册=%d, 成功=%d, 失败=%d, 跳过=%d, 注销=%d",
        _LOG, len(new_keys), total_successful, total_failed,
        unchanged_count, dereg_ok,
    )

    return {
        "success": total_failed == 0 and dereg_fail == 0,
        "total_pods": len(pods),
        "total_instances": len(current_keys),
        "successful": total_successful,
        "failed": total_failed,
        "dereg_ok": dereg_ok,
        "dereg_fail": dereg_fail,
        "skipped": unchanged_count,
        "removed": len(removed_keys),
    }


# =============================================================================
# 主循环
# =============================================================================

def k8s_nacos_register_loop(interval: int = DEFAULT_INTERVAL):
    """
    K8s 容器 Nacos 注册主循环（后台线程入口）。

    参数:
        interval: 轮询间隔（秒），默认 30 秒
    """
    logging.info("[%s] K8s Nacos 注册循环启动，间隔 %d 秒", _LOG, interval)
    while True:
        try:
            result = discover_and_register()
            if result["failed"] > 0 or result.get("dereg_fail", 0) > 0:
                logging.warning(
                    "[%s] 部分操作失败: pods=%d, 新增=%d, 注册成功=%d, 注册失败=%d, "
                    "注销成功=%d, 注销失败=%d, 跳过=%d",
                    _LOG, result["total_pods"], result["successful"] + result["failed"],
                    result["successful"], result["failed"],
                    result.get("dereg_ok", 0), result.get("dereg_fail", 0),
                    result.get("skipped", 0),
                )
            elif result["successful"] > 0 or result.get("dereg_ok", 0) > 0:
                logging.info(
                    "[%s] 变更处理: pods=%d, 注册=%d, 注销=%d, 跳过=%d",
                    _LOG, result["total_pods"], result["successful"],
                    result.get("dereg_ok", 0), result.get("skipped", 0),
                )
            else:
                logging.debug(
                    "[%s] 无变化跳过: pods=%d, 已缓存=%d",
                    _LOG, result["total_pods"], result.get("skipped", 0),
                )
        except Exception as e:
            logging.error("[%s] 循环异常: %s", _LOG, e, exc_info=True)
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

    # 1. 发现
    pods = discover_k8s_containers(_LOG, skip_host_network=True)
    print(f"\n发现 {len(pods)} 个运行 Pod:")
    for p in pods:
        containers_str = ", ".join(
            f"{c['name']}:{c['ports']}" for c in p.get("containers", [])
        )
        print(f"  [{p['namespace']}] {p['pod_name']} ({p['pod_ip']}) -> {containers_str}")

    # 2. 注册
    result = discover_and_register()
    print(f"\n{'=' * 60}")
    print(f"  K8s 容器 Nacos 注册结果")
    print(f"{'=' * 60}")
    print(f"  Pod 总数   : {result['total_pods']}")
    print(f"  实例总数   : {result['total_instances']}")
    print(f"  注册成功   : {result['successful']}")
    print(f"  注册失败   : {result['failed']}")
    print(f"  注销成功   : {result.get('dereg_ok', 0)}")
    print(f"  注销失败   : {result.get('dereg_fail', 0)}")
    print(f"  跳过(缓存) : {result.get('skipped', 0)}")
    print(f"  实例下线   : {result.get('removed', 0)}")
    print(f"{'=' * 60}")
