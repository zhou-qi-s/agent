"""
Nacos 心跳发送模块（本地进程版）

【缓存区/运行区分离后的服务发现规则】

定时向 Nacos 发送服务心跳，独立于 NacosRegister 工作：
    1. 遍历 {server.apps}/ 下所有服务目录
    2. 读 state/config.yaml 判存活（runtime == true 且 pids 中有存活进程）
    3. 读 {server.apps}/{服务名}/nacos/registered 获取注册时缓存的 Nacos 参数
    4. 并发调用 Nacos 心跳接口

变更(v2)：ThreadPool 并发 + 指数退避重试 + token 认证 + Session 复用 + RateLimiter 限速。
变更(v3)：改为扫运行区 state/config.yaml 判存活；
          心跳参数缓存迁到 {apps}/{服务名}/nacos/registered
          （原为 {download}/{服务名}/{版本}/runtime/nacos）。
"""

import concurrent.futures
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from core.nacos.k8s_nacos_common import (
    RateLimiter,
    get_nacos_address,
    get_session,
    is_token_invalid,
    nacos_login,
)
from utils.app_path import (
    SUB_DIR_CANDIDATES,
    get_nacos_dir,
    read_state,
    read_state_pids,
    filter_alive_pids,
)
from utils.config_loader import load_config

_CONFIG = load_config()
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

# 注册标记文件名（NacosRegister 写入，心跳读取复用）
_REGISTERED_FILE = "registered"

_LOG = "nacos_heartbeat"

# ── 默认参数 ──
DEFAULT_INTERVAL = 10
DISCOVERY_INTERVAL = 300             # 发现缓存有效期（秒）
MAX_HEARTBEAT_WORKERS = 10           # 并发心跳 worker（本地进程数少，10 足够）
HEARTBEAT_MAX_RETRIES = 3            # 失败最大重试
HEARTBEAT_RETRY_BACKOFF = (1, 3)     # 指数退避：1s, 3s
HEARTBEAT_SUBMIT_RATE = 50           # 提交限速 /s（本地场景比 k8s 更低）
HEARTBEAT_TIMEOUT = (3, 5)           # 连接 3s, 读取 5s

# ── beat JSON 缓存（避免每轮对不变的数据重复 json.dumps）──
# key: service_name|ip|port，value: 序列化后的 JSON 字符串
_BEAT_JSON_CACHE: Dict[str, str] = {}


class NacosHeartbeat:
    """Nacos 心跳发送器：扫运行区发现存活服务，并发发送心跳"""

    def __init__(self):
        self._apps_base = _APPS_BASE

    # ------------------------------------------------------------------
    # 服务发现（扫运行区 + 读运行状态）
    # ------------------------------------------------------------------

    def _discover_for_heartbeat(self) -> List[Dict[str, Any]]:
        """
        扫描运行区，发现需要发心跳的服务。

        判据（三类应用都要覆盖，见 SUB_DIR_CANDIDATES）：
            1. {apps}/[{sub_dir}/]{服务名}/state/config.yaml 的 runtime == true
            2. pids 中至少一个进程存活
            3. {apps}/[{sub_dir}/]{服务名}/nacos/registered 存在（说明已注册成功）

        注意：显控台（displayConsole/）与插件（plugin/）应用多一层目录，
        必须按类别层遍历，否则这两类应用的心跳永远发不出去（实例会被 Nacos 剔除）。
        """
        result: List[Dict[str, Any]] = []

        if not self._apps_base or not os.path.isdir(self._apps_base):
            logging.warning("[%s] 运行区目录不存在: %s", _LOG, self._apps_base)
            return result

        for sub_dir in SUB_DIR_CANDIDATES:
            root = os.path.join(self._apps_base, sub_dir) if sub_dir else self._apps_base
            if not os.path.isdir(root):
                continue
            for entry in os.listdir(root):
                service_dir = os.path.join(root, entry)
                if os.path.islink(service_dir) or not os.path.isdir(service_dir):
                    continue

                # 类别层目录本身没有 state/config.yaml，会在这里被自然跳过
                state = read_state(entry, sub_dir)
                if not state or not state.get("runtime"):
                    continue

                if not filter_alive_pids(read_state_pids(entry, sub_dir), entry):
                    logging.info("[%s] 所有 PID 均已退出: service=%s/%s", _LOG, sub_dir, entry)
                    continue

                nacos_dir = get_nacos_dir(entry, sub_dir)
                registered_file = os.path.join(nacos_dir, _REGISTERED_FILE) if nacos_dir else ""
                if not registered_file or not os.path.isfile(registered_file):
                    logging.debug("[%s] 尚未注册，跳过: service=%s/%s", _LOG, sub_dir, entry)
                    continue

                nacos_config = self._read_nacos_file(registered_file, entry)
                if nacos_config is None:
                    continue

                result.append({
                    "folder_name": entry,
                    "sub_dir": sub_dir,
                    "version": str(state.get("version", "") or "").strip(),
                    "nacos_config": nacos_config,
                })

        return result

    @staticmethod
    def _read_nacos_file(nacos_file: str, folder_name: str) -> Optional[Dict[str, Any]]:
        try:
            with open(nacos_file, "r", encoding="utf-8") as f:
                nacos_config = json.load(f)
        except Exception as e:
            logging.warning("[%s] 读取 nacos 文件失败: %s -> %s", _LOG, nacos_file, e)
            return None

        if not isinstance(nacos_config, dict) or not nacos_config:
            logging.warning("[%s] nacos 文件内容为空: folder=%s", _LOG, folder_name)
            return None

        service_name = nacos_config.get("serviceName", "")
        if not service_name:
            logging.warning("[%s] nacos 文件缺少 serviceName: folder=%s", _LOG, folder_name)
            return None

        return nacos_config

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    @staticmethod
    def _get_nacos_cfg_for_beat() -> Dict[str, Any]:
        """读取 Nacos 全局配置（心跳/认证相关）。"""
        nacos_cfg = _CONFIG.get("nacos", {}) if isinstance(_CONFIG, dict) else {}
        api_cfg = nacos_cfg.get("api", {}) if isinstance(nacos_cfg.get("api"), dict) else {}

        return {
            "address": get_nacos_address(),
            "heartbeat_api": str(
                api_cfg.get("heartbeat", "/nacos/v1/ns/instance/beat") or
                "/nacos/v1/ns/instance/beat"
            ).strip(),
            "group_name": str(nacos_cfg.get("group_name", "DEFAULT_GROUP") or "DEFAULT_GROUP").strip(),
            "namespace_id": str(nacos_cfg.get("namespace_id", "") or "").strip(),
            "username": nacos_cfg.get("username"),
            "password": nacos_cfg.get("password"),
        }

    # ------------------------------------------------------------------
    # 心跳发送（v2: 并发 + 重试 + token + 限速）
    # ------------------------------------------------------------------

    def send_heartbeats(self):
        """发现服务并发送心跳。调用失败返回 None。"""
        try:
            services = self._discover_for_heartbeat()
            return self._send_heartbeats_for_services(services)
        except Exception as e:
            logging.error("[%s] send_heartbeats 调用失败: %s", _LOG, e, exc_info=True)
            return None

    def discover_and_heartbeat(self):
        """发现服务并发送心跳（兼容旧接口）。调用失败返回 None。"""
        try:
            services = self._discover_for_heartbeat()
            heartbeat_result = self._send_heartbeats_for_services(services)
            return {
                "success": heartbeat_result["success"],
                "service_count": len(services),
                "services": services,
                "heartbeat": heartbeat_result,
            }
        except Exception as e:
            logging.error("[%s] discover_and_heartbeat 调用失败: %s", _LOG, e, exc_info=True)
            return None

    def _send_heartbeats_for_services(self, services: List[Dict[str, Any]]) -> Dict[str, Any]:
        """对服务列表并发发送心跳。"""
        if not services:
            return {"success": True, "total": 0, "successful": 0, "failed": 0, "skipped": 0, "errors": []}

        nacos_cfg = self._get_nacos_cfg_for_beat()
        nacos_address = nacos_cfg.get("address", "")
        if not nacos_address:
            logging.warning("[%s] Nacos 地址未配置，跳过心跳", _LOG)
            return {"success": False, "total": len(services), "successful": 0, "failed": len(services),
                    "skipped": 0, "errors": []}

        heartbeat_url = f"{nacos_address.rstrip('/')}{nacos_cfg['heartbeat_api']}"
        group_name = nacos_cfg["group_name"]
        namespace_id = nacos_cfg["namespace_id"]
        username = nacos_cfg.get("username")
        password = nacos_cfg.get("password")

        access_token = nacos_login(_LOG, username, password, nacos_address)
        if not access_token:
            logging.warning("[%s] 获取 accessToken 失败，心跳将不携带认证", _LOG)

        # ── 阶段 1：预计算所有心跳 payload（单线程，填充缓存）──
        tasks: List[Dict[str, Any]] = []
        for svc in services:
            nc = svc.get("nacos_config", {})
            service_name = str(nc.get("serviceName", "") or "").strip()
            if not service_name:
                continue

            ip = str(nc.get("ip", "") or "").strip()
            port = int(nc.get("port", 0))
            cluster = str(nc.get("cluster", "DEFAULT") or "DEFAULT").strip()
            weight = float(nc.get("weight", 1.0))
            healthy = bool(nc.get("healthy", True))
            metadata = nc.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}

            beat_key = f"{service_name}|{ip}|{port}"
            beat_json = _BEAT_JSON_CACHE.get(beat_key)
            if beat_json is None:
                beat_info = {
                    "ip": ip,
                    "port": port,
                    "serviceName": service_name,
                    "cluster": cluster,
                    "weight": weight,
                    "healthy": healthy,
                    "metadata": metadata,
                    "scheduled": True,
                }
                beat_json = json.dumps(beat_info, ensure_ascii=False)
                _BEAT_JSON_CACHE[beat_key] = beat_json

            payload = {
                "serviceName": service_name,
                "groupName": group_name,
                "clusterName": cluster,
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
                "folder_name": svc.get("folder_name", ""),
            })

        if not tasks:
            return {"success": True, "total": len(services), "successful": 0, "failed": 0,
                    "skipped": 0, "errors": []}

        # ── 阶段 2：并发 + RateLimiter 发送心跳 ──
        total_successful = 0
        total_failed = 0
        errors: List[Dict[str, Any]] = []
        limiter = RateLimiter(HEARTBEAT_SUBMIT_RATE)

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_HEARTBEAT_WORKERS) as executor:
            future_to_task = {}
            for task in tasks:
                limiter.acquire()
                future_to_task[executor.submit(
                    _do_single_beat, task, nacos_cfg, access_token,
                )] = task

            for future in concurrent.futures.as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    ok, new_token = future.result()
                    if new_token:
                        access_token = new_token
                    if ok:
                        total_successful += 1
                    else:
                        total_failed += 1
                        errors.append({
                            "folder": task["folder_name"],
                            "service": task["service_name"],
                            "error": "心跳失败(重试耗尽)",
                        })
                except Exception as e:
                    total_failed += 1
                    logging.warning("[%s] 心跳线程异常: folder=%s, error=%s",
                                    _LOG, task["folder_name"], e)
                    errors.append({
                        "folder": task["folder_name"],
                        "service": task["service_name"],
                        "error": str(e),
                    })

        # 修剪缓存：移除已不存在的服务
        valid_keys = {f"{t['service_name']}|{t['payload'].get('ip','')}|{t['payload'].get('port','')}"
                      for t in tasks}
        stale = [k for k in _BEAT_JSON_CACHE if k not in valid_keys]
        for k in stale:
            del _BEAT_JSON_CACHE[k]

        logging.debug(
            "[%s] 心跳完成: 服务=%d, 成功=%d, 失败=%d, cache=%d",
            _LOG, len(tasks), total_successful, total_failed, len(_BEAT_JSON_CACHE),
        )

        return {
            "success": total_failed == 0,
            "total": len(services),
            "successful": total_successful,
            "failed": total_failed,
            "skipped": 0,
            "errors": errors,
        }


# =============================================================================
# 单次心跳 worker（模块级函数，供线程池调用）
# =============================================================================

def _do_single_beat(task: Dict[str, Any], nacos_cfg: Dict[str, Any],
                    current_token: str) -> tuple:
    """发送单个心跳，支持重试和 token 自动刷新。返回 (ok, new_token_or_none)。"""
    url = task["url"]
    payload = dict(task["payload"])
    service_name = task["service_name"]

    for retry in range(HEARTBEAT_MAX_RETRIES):
        try:
            resp = get_session(MAX_HEARTBEAT_WORKERS).put(url, params=payload, timeout=HEARTBEAT_TIMEOUT)
            if resp.status_code == 200:
                return True, None

            body_text = resp.text[:300]

            if resp.status_code in (401, 403) and is_token_invalid(body_text):
                if retry < HEARTBEAT_MAX_RETRIES - 1:
                    logging.warning(
                        "[%s] 心跳 Token 失效(%s), 重新登录: service=%s",
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
                        current_token = new_token
                        time.sleep(1)
                        continue

            if resp.status_code == 503 and "Distro snapshot load failed" in body_text:
                if retry < len(HEARTBEAT_RETRY_BACKOFF):
                    time.sleep(HEARTBEAT_RETRY_BACKOFF[retry])
                continue

            logging.warning(
                "[%s] 心跳失败(%s): service=%s, %s",
                _LOG, resp.status_code, service_name, body_text,
            )
            return False, None

        except Exception as e:
            if retry < HEARTBEAT_MAX_RETRIES - 1:
                delay = HEARTBEAT_RETRY_BACKOFF[min(retry, len(HEARTBEAT_RETRY_BACKOFF) - 1)]
                time.sleep(delay)
                continue
            logging.warning("[%s] 心跳异常(重试耗尽): service=%s, error=%s",
                            _LOG, service_name, e)
            return False, None

    return False, None


# =============================================================================
# 主循环 & 单例
# =============================================================================

_default_heartbeat: Optional[NacosHeartbeat] = None


def get_nacos_heartbeat() -> NacosHeartbeat:
    """获取全局 NacosHeartbeat 单例"""
    global _default_heartbeat
    if _default_heartbeat is None:
        _default_heartbeat = NacosHeartbeat()
    return _default_heartbeat


def nacos_heartbeat_loop(interval: int = DEFAULT_INTERVAL):
    """Nacos 心跳主循环（后台线程入口）。"""
    logging.info("[%s] Nacos 心跳循环启动，间隔 %d 秒", _LOG, interval)
    heartbeat = get_nacos_heartbeat()
    while True:
        try:
            result = heartbeat.send_heartbeats()
            if result is None:
                logging.warning("[%s] 本轮心跳调用失败，返回 null", _LOG)
            elif result.get("failed", 0) > 0:
                logging.warning(
                    "[%s] 部分心跳失败: 服务=%d, 成功=%d, 失败=%d",
                    _LOG, result["total"], result["successful"], result["failed"],
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
    result = NacosHeartbeat().send_heartbeats()
    print(f"\n{'=' * 60}")
    print(f"  Nacos 心跳结果 (v2)")
    print(f"{'=' * 60}")
    print(f"  服务总数 : {result['total']}")
    print(f"  成功     : {result['successful']}")
    print(f"  失败     : {result['failed']}")
    print(f"{'=' * 60}")
