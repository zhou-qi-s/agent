"""
Nacos 服务注册模块（本地进程版）

【缓存区/运行区分离后的服务发现规则】

扫描「运行区」发现存活服务并注册到 Nacos（幂等，重复注册无副作用）：
    1. 遍历 {server.apps}/ 下所有服务目录
    2. 读 state/config.yaml：
         runtime != true          → 跳过（未运行）
         pids 中的进程均不存活     → 跳过
    3. 读 {server.apps}/{服务名}/nacos/config.yaml 提取 Nacos 配置
       → 注册到 Nacos
       → 注册成功后写入 {server.apps}/{服务名}/nacos/registered 标记
         （供心跳模块复用）

变更(v2)：ThreadPool 并发 + 指数退避重试 + token 认证 + Session 复用 + RateLimiter 限速。
变更(v3)：从 runtime/config.yaml 读取 Nacos 配置（不再依赖 application.yml）。
变更(v4)：改为扫运行区 state/config.yaml 判存活；
          Nacos 配置迁到运行区独立目录 {apps}/{服务名}/nacos/config.yaml
          （原为 {download}/{服务名}/{版本}/runtime/config.yaml）。
"""

import concurrent.futures
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import yaml

from core.nacos.k8s_nacos_common import (
    RateLimiter,
    get_nacos_address,
    get_session,
    is_token_invalid,
    nacos_login,
)
from utils.app_path import (
    SUB_DIR_CANDIDATES,
    find_apps_component_dir,
    get_nacos_config_file,
    get_nacos_dir,
    read_state,
    read_state_pids,
    filter_alive_pids,
)
from utils.config_loader import load_config

_CONFIG = load_config()
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

_LOG = "nacos_register"

# ── 默认参数 ──
DEFAULT_INTERVAL = 30
MAX_REGISTER_WORKERS = 10          # 并发注册 worker（本地进程数少）
REGISTER_MAX_RETRIES = 3           # 失败最大重试
REGISTER_RETRY_BACKOFF = (2, 5)    # 指数退避：2s, 5s
REGISTER_SUBMIT_RATE = 20          # 提交限速 /s（注册比心跳重，更保守）
REGISTER_TIMEOUT = (5, 10)         # 连接 5s, 读取 10s

# ── metadata JSON 缓存 ──
_METADATA_JSON_CACHE: Dict[str, str] = {}
_METADATA_CACHE_LOCK = threading.Lock()


# =============================================================================
# 运行区 Nacos 配置：缺失时自动生成
# =============================================================================
# 常见「基础设施端口」：不能当服务端口（踩过：java 应用同时监听 6379/8080/9848 这类，
# 取最小端口会把 Redis / Nacos 的 gRPC 端口注册成服务端口）
_AUX_PORT_DENYLIST = {6379, 3306, 5432, 11211, 7946, 7848, 9848, 9849}


def _listening_ports(pids: List[int]) -> List[int]:
    """取这些进程正在监听的 TCP 端口（升序、去重）。"""
    import psutil  # 局部导入：本文件其余逻辑用不到 psutil

    ports = set()
    for pid in pids or []:
        try:
            proc = psutil.Process(int(pid))
            for conn in proc.net_connections(kind="tcp"):
                if conn.status == psutil.CONN_LISTEN and conn.laddr:
                    ports.add(int(conn.laddr.port))
        except Exception:
            continue
    return sorted(p for p in ports if p > 0)


def _pick_service_port(ports: List[int]) -> int:
    """从监听端口里挑一个最像「服务端口」的：排除已知基础设施端口后取最小；都没有就取最小。"""
    normal = [p for p in (ports or []) if p not in _AUX_PORT_DENYLIST]
    if normal:
        return normal[0]
    return ports[0] if ports else 0


def _auto_create_nacos_config(
        service_name: str, sub_dir: str, alive_pids: List[int], version: str
) -> Optional[str]:
    """
    运行区缺 {apps}/[{sub_dir}/]{服务名}/nacos/config.yaml 时自动生成一份。

    约定（生成一次，之后永不覆盖，人工修正会被保留）：
        · serviceName 默认取「应用目录名」
        · port 优先取应用自带的 config/app.yaml 里的 port；没有就取进程实际监听的端口（最小的那个）
        · groupName / namespace / weight / enabled 等取 Agent 配置里 nacos 段的值

    返回生成后的文件路径；无法生成（没端口等）返回 None。
    """
    if not service_name:
        return None

    component_dir = find_apps_component_dir(service_name, sub_dir)
    app_conf: Dict[str, Any] = {}
    if component_dir:
        app_yaml = os.path.join(component_dir, "config", "app.yaml")
        if os.path.isfile(app_yaml):
            try:
                with open(app_yaml, "r", encoding="utf-8") as f:
                    app_conf = yaml.safe_load(f) or {}
            except Exception as e:
                logging.warning("[%s] 读取 %s 失败: %s", _LOG, app_yaml, e)
    if not isinstance(app_conf, dict):
        app_conf = {}

    ports = _listening_ports(alive_pids)
    try:
        port = int(app_conf.get("port")) if app_conf.get("port") else _pick_service_port(ports)
    except (TypeError, ValueError):
        port = _pick_service_port(ports)
    if not port:
        logging.warning("[%s] 服务=%s/%s 没有可用端口，跳过自动生成 nacos 配置",
                        _LOG, sub_dir, service_name)
        return None

    nacos_cfg = _CONFIG.get("nacos", {}) or {}
    payload = {
        "version": str(version or app_conf.get("version", "") or ""),
        "nacos": {
            "serviceName": str(app_conf.get("serviceName") or service_name),
            "groupName": str(nacos_cfg.get("group_name", "DEFAULT_GROUP") or "DEFAULT_GROUP"),
            "namespace": str(nacos_cfg.get("namespace", "") or ""),
            "clusterName": str(nacos_cfg.get("cluster_name", "DEFAULT") or "DEFAULT"),
            "port": port,
            "weight": nacos_cfg.get("weight", 1.0),
            "healthy": nacos_cfg.get("healthy", True),
            "enabled": nacos_cfg.get("enabled", True),
            "ephemeral": nacos_cfg.get("ephemeral", True),
            "metadata": {
                "version": str(version or ""),
                "autoGenerated": "true",
                "subDir": sub_dir,
            },
        },
    }

    config_path = get_nacos_config_file(service_name, sub_dir)
    if not config_path:
        return None
    try:
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
        logging.warning("[%s] 运行区缺少 nacos 配置，已自动生成: %s（serviceName=%s, port=%s, 监听端口=%s）",
                        _LOG, config_path, payload["nacos"]["serviceName"], port, ports)
    except Exception as e:
        logging.warning("[%s] 自动生成 nacos 配置失败: %s", _LOG, e)
        return None
    return config_path


class NacosRegister:
    """Nacos 服务注册器：发现运行区存活服务并注册到 Nacos"""

    def __init__(self):
        self._apps_base = _APPS_BASE

    # ------------------------------------------------------------------
    # 服务发现（扫运行区 + 读运行状态）
    # ------------------------------------------------------------------

    def discover_services(self) -> List[Dict[str, Any]]:
        """
        扫描运行区，发现进程存活的 Nacos 服务实例。

        判据（三类应用都要覆盖，见 SUB_DIR_CANDIDATES）：
            1. {apps}/[{sub_dir}/]{服务名}/state/config.yaml 的 runtime == true
            2. pids 中至少有一个进程存活
            3. {apps}/[{sub_dir}/]{服务名}/nacos/config.yaml 存在且含 nacos.serviceName
               （缺失时按「服务名 + 监听端口」自动生成，见 _auto_create_nacos_config）

        注意：显控台（displayConsole/）与插件（plugin/）应用比虚拟机应用多一层目录，
        必须按类别层遍历；只遍历顶层会让这两类应用永远扫不到（恒为"服务数=0"）。
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
                # 跳过软链接与普通文件（current/app/bin/config 是链接）
                if os.path.islink(service_dir) or not os.path.isdir(service_dir):
                    continue

                # 读运行状态，判断是否在运行
                # 注：类别层目录本身（displayConsole / plugin）没有 state/config.yaml，会在这里被自然跳过
                state = read_state(entry, sub_dir)
                if not state:
                    continue
                if not state.get("runtime"):
                    logging.debug("[%s] 服务未运行，跳过: %s/%s", _LOG, sub_dir, entry)
                    continue

                # pids 中至少一个存活
                alive_pids = filter_alive_pids(read_state_pids(entry, sub_dir), entry)
                if not alive_pids:
                    logging.info("[%s] 所有 PID 均已退出，跳过: %s/%s", _LOG, sub_dir, entry)
                    continue

                version = str(state.get("version", "") or "").strip()

                nacos_config = self._try_discover_from_config_yaml(entry, sub_dir, alive_pids, version)
                if nacos_config is None:
                    continue

                nacos_dir = get_nacos_dir(entry, sub_dir)
                result.append({
                    "folder_name": entry,
                    "sub_dir": sub_dir,
                    "version": version,
                    "nacos_config": nacos_config,
                    # 注册成功后的标记文件（心跳模块据此复用，无需重复注册）
                    "nacos_file": os.path.join(nacos_dir, "registered") if nacos_dir else "",
                })

        return result

    @staticmethod
    def _try_discover_from_config_yaml(
            service_name: str,
            sub_dir: str = "",
            alive_pids: Optional[List[int]] = None,
            version: str = ""
    ) -> Optional[Dict[str, Any]]:
        """
        从运行区 Nacos 配置提取注册信息。

        读取路径: {server.apps}/{服务名}/nacos/config.yaml
        （原为 {download}/{服务名}/{版本}/runtime/config.yaml）

        格式:
            version: "1.0.0"
            nacos:
              serviceName: xxx
              groupName: xxxx
              ...
        """
        config_path = get_nacos_config_file(service_name, sub_dir)
        if not config_path or not os.path.isfile(config_path):
            # 运行区没有 nacos/config.yaml：按「服务名 = 应用目录名 + 端口 = 应用监听端口」自动生成一份
            # （文件已存在时永不改写，人工修正过的内容会被保留）
            config_path = _auto_create_nacos_config(service_name, sub_dir, alive_pids or [], version)
        if not config_path or not os.path.isfile(config_path):
            logging.info("[%s] nacos 配置不存在且无法自动生成（服务=%s/%s），跳过注册",
                         _LOG, sub_dir, service_name)
            return None

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                runtime_conf = yaml.safe_load(f) or {}
            logging.info("[%s] 读取到 nacos 配置: %s", _LOG, config_path)
        except Exception as e:
            logging.warning("[%s] 读取 nacos 配置失败: %s -> %s", _LOG, config_path, e)
            return None

        if not isinstance(runtime_conf, dict):
            return None

        nacos_section = runtime_conf.get("nacos", {})
        if not isinstance(nacos_section, dict):
            nacos_section = {}

        # Nacos 注册用的服务名（注意不要覆盖入参 service_name）
        nacos_service_name = str(nacos_section.get("serviceName", "") or "").strip()
        if not nacos_service_name:
            logging.warning("[%s] nacos 配置中未找到 nacos.serviceName: %s",
                            _LOG, config_path)
            return None

        # 版本号：优先取 nacos 配置中的 version，缺省用运行状态里的版本
        version = str(runtime_conf.get("version", "") or "").strip()
        if not version:
            version = str(read_state(service_name).get("version", "") or "").strip()

        # IP / 端口优先从主 config 的 nacos 节点读取
        nacos_runtime = _CONFIG.get("nacos", {}) if isinstance(_CONFIG, dict) else {}
        ip = str(nacos_runtime.get("ip", "") or "").strip()
        if not ip:
            from utils.util import get_ip
            ip = get_ip()

        port = int(nacos_runtime.get("port", 0)) if nacos_runtime.get("port") else 0
        if not port:
            logging.warning("[%s] config.yaml 中未配置 nacos.port", _LOG)
            return None

        group_name = str(
            nacos_section.get("groupName", nacos_section.get("group", "DEFAULT_GROUP"))
            or "DEFAULT_GROUP"
        ).strip()
        cluster = str(
            nacos_section.get("clusterName", nacos_section.get("cluster", "DEFAULT")) or "DEFAULT"
        ).strip()
        weight = float(nacos_section.get("weight", 1.0))
        healthy = bool(nacos_section.get("healthy", True))
        enabled = bool(nacos_section.get("enabled", True))
        ephemeral = bool(nacos_section.get("ephemeral", True))

        metadata = {}
        raw_metadata = nacos_section.get("metadata", {})
        if isinstance(raw_metadata, dict):
            metadata = dict(raw_metadata)
        metadata["version"] = version

        return {
            "ip": ip,
            "port": port,
            "serviceName": nacos_service_name,
            "groupName": group_name,
            "cluster": cluster,
            "weight": weight,
            "healthy": healthy,
            "enabled": enabled,
            "ephemeral": ephemeral,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    @staticmethod
    def _get_nacos_cfg() -> Dict[str, Any]:
        """读取 Nacos 全局配置。"""
        nacos_cfg = _CONFIG.get("nacos", {}) if isinstance(_CONFIG, dict) else {}
        return {
            "address": get_nacos_address(),
            "group_name": str(nacos_cfg.get("group_name", "DEFAULT_GROUP") or "DEFAULT_GROUP").strip(),
            "namespace_id": str(nacos_cfg.get("namespace_id", "") or "").strip(),
            "username": nacos_cfg.get("username"),
            "password": nacos_cfg.get("password"),
        }

    # ------------------------------------------------------------------
    # 服务注册（v2: 并发 + 重试 + token + 限速）
    # ------------------------------------------------------------------

    def register_all(self) -> Dict[str, Any]:
        """发现并注册所有存活的进程到 Nacos（幂等）。"""
        services = self.discover_services()
        if not services:
            return {"success": True, "total": 0, "successful": 0, "failed": 0, "errors": []}

        nacos_cfg = self._get_nacos_cfg()
        nacos_address = nacos_cfg.get("address", "")
        if not nacos_address:
            logging.warning("[%s] Nacos 地址未配置，跳过注册", _LOG)
            return {"success": False, "total": len(services), "successful": 0,
                    "failed": len(services), "errors": []}

        register_url = f"{nacos_address.rstrip('/')}/nacos/v1/ns/instance"
        group_name = nacos_cfg["group_name"]
        namespace_id = nacos_cfg["namespace_id"]
        username = nacos_cfg.get("username")
        password = nacos_cfg.get("password")

        access_token = nacos_login(_LOG, username, password, nacos_address)
        if not access_token:
            logging.warning("[%s] 获取 accessToken 失败，注册将不携带认证", _LOG)

        # ── 预计算注册 payload ──
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
            enabled = bool(nc.get("enabled", True))
            ephemeral = bool(nc.get("ephemeral", True))
            metadata = nc.get("metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}

            meta_key = f"{service_name}|{ip}|{port}"
            with _METADATA_CACHE_LOCK:
                metadata_json = _METADATA_JSON_CACHE.get(meta_key)
            if metadata_json is None:
                metadata_json = json.dumps(metadata, ensure_ascii=False)
                with _METADATA_CACHE_LOCK:
                    _METADATA_JSON_CACHE[meta_key] = metadata_json

            params = {
                "serviceName": service_name,
                "groupName": group_name,
                "ip": ip,
                "port": port,
                "clusterName": cluster,
                "weight": weight,
                "healthy": healthy,
                "enabled": enabled,
                "ephemeral": str(ephemeral).lower(),
                "metadata": metadata_json,
            }
            if namespace_id:
                params["namespaceId"] = namespace_id
            if access_token:
                params["accessToken"] = access_token

            tasks.append({
                "url": register_url,
                "params": params,
                "service_name": service_name,
                "folder_name": svc.get("folder_name", ""),
                "nacos_file": svc.get("nacos_file", ""),
                "nacos_config": nc,
            })

        if not tasks:
            return {"success": True, "total": len(services), "successful": 0, "failed": 0, "errors": []}

        # ── 并发 + RateLimiter 注册 ──
        total_successful = 0
        total_failed = 0
        errors: List[Dict[str, Any]] = []
        limiter = RateLimiter(REGISTER_SUBMIT_RATE)

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_REGISTER_WORKERS) as executor:
            future_to_task = {}
            for task in tasks:
                limiter.acquire()
                future_to_task[executor.submit(
                    _do_single_register, task, nacos_cfg, access_token,
                )] = task

            for future in concurrent.futures.as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    ok, new_token = future.result()
                    if new_token:
                        access_token = new_token
                    if ok:
                        total_successful += 1
                        # 注册成功 → 写入 {apps}/{服务名}/nacos/registered 标记
                        if task["nacos_file"]:
                            self._write_nacos_file(task["nacos_file"], task["nacos_config"])
                    else:
                        total_failed += 1
                        errors.append({
                            "folder": task["folder_name"],
                            "service": task["service_name"],
                            "error": "注册失败(重试耗尽)",
                        })
                except Exception as e:
                    total_failed += 1
                    logging.warning("[%s] 注册线程异常: folder=%s, error=%s",
                                    _LOG, task["folder_name"], e)
                    errors.append({
                        "folder": task["folder_name"],
                        "service": task["service_name"],
                        "error": str(e),
                    })

        # 修剪 metadata 缓存
        valid_keys = {f"{t['service_name']}|{t['params'].get('ip','')}|{t['params'].get('port','')}"
                      for t in tasks}
        with _METADATA_CACHE_LOCK:
            stale = [k for k in _METADATA_JSON_CACHE if k not in valid_keys]
            for k in stale:
                del _METADATA_JSON_CACHE[k]

        logging.info(
            "[%s] 注册完成: 服务=%d, 成功=%d, 失败=%d, cache=%d",
            _LOG, len(tasks), total_successful, total_failed, len(_METADATA_JSON_CACHE),
        )

        return {
            "success": total_failed == 0,
            "total": len(services),
            "successful": total_successful,
            "failed": total_failed,
            "errors": errors,
        }

    @staticmethod
    def _write_nacos_file(nacos_file: str, nacos_config: Dict[str, Any]) -> None:
        """注册成功后写入 nacos 标记文件（{apps}/{服务名}/nacos/registered）。"""
        runtime_dir = os.path.dirname(nacos_file)
        os.makedirs(runtime_dir, exist_ok=True)
        try:
            with open(nacos_file, "w", encoding="utf-8") as f:
                json.dump(nacos_config, f, ensure_ascii=False, indent=2)
            logging.info("[%s] nacos 标记文件已写入: %s", _LOG, nacos_file)
        except Exception as e:
            logging.warning("[%s] 写入 nacos 文件失败: %s -> %s", _LOG, nacos_file, e)


# =============================================================================
# 单次注册 worker（模块级函数，供线程池调用）
# =============================================================================

def _do_single_register(task: Dict[str, Any], nacos_cfg: Dict[str, Any],
                        current_token: str) -> tuple:
    """注册单个实例到 Nacos，支持重试和 token 自动刷新。返回 (ok, new_token_or_none)。"""
    url = task["url"]
    params = dict(task["params"])
    service_name = task["service_name"]

    for retry in range(REGISTER_MAX_RETRIES):
        try:
            resp = get_session(MAX_REGISTER_WORKERS).post(url, params=params, timeout=REGISTER_TIMEOUT)
            if resp.status_code == 200:
                return True, None

            body_text = resp.text[:300]

            if resp.status_code in (401, 403) and is_token_invalid(body_text):
                if retry < REGISTER_MAX_RETRIES - 1:
                    logging.warning(
                        "[%s] 注册 Token 失效(%s), 重新登录: service=%s",
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
                        params["accessToken"] = new_token
                        current_token = new_token
                        time.sleep(1)
                        continue

            if resp.status_code == 503 and "Distro snapshot load failed" in body_text:
                if retry < len(REGISTER_RETRY_BACKOFF):
                    time.sleep(REGISTER_RETRY_BACKOFF[retry])
                continue

            logging.warning(
                "[%s] 注册失败(%s): service=%s, %s",
                _LOG, resp.status_code, service_name, body_text,
            )
            return False, None

        except Exception as e:
            if retry < REGISTER_MAX_RETRIES - 1:
                delay = REGISTER_RETRY_BACKOFF[min(retry, len(REGISTER_RETRY_BACKOFF) - 1)]
                time.sleep(delay)
                continue
            logging.warning("[%s] 注册异常(重试耗尽): service=%s, error=%s",
                            _LOG, service_name, e)
            return False, None

    return False, None


# =============================================================================
# 主循环 & 单例
# =============================================================================

_default_register: Optional[NacosRegister] = None


def get_nacos_register() -> NacosRegister:
    """获取全局 NacosRegister 单例"""
    global _default_register
    if _default_register is None:
        _default_register = NacosRegister()
    return _default_register


def nacos_register_loop(interval: int = DEFAULT_INTERVAL):
    """Nacos 注册主循环（后台线程入口）。"""
    logging.info("[%s] Nacos 注册循环启动，间隔 %d 秒", _LOG, interval)
    register = get_nacos_register()
    while True:
        try:
            result = register.register_all()
            if result.get("failed", 0) > 0:
                logging.warning(
                    "[%s] 部分注册失败: 服务=%d, 成功=%d, 失败=%d",
                    _LOG, result.get("total", 0), result["successful"], result["failed"],
                )
        except Exception as e:
            logging.error("[%s] 注册循环异常: %s", _LOG, e, exc_info=True)
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

    register = NacosRegister()

    # 1. 发现服务
    services = register.discover_services()
    print(f"\n发现 {len(services)} 个存活服务:")
    for svc in services:
        print(f"  - {svc['folder_name']} v{svc['version']}: {svc['nacos_config'].get('serviceName')} "
              f"({svc['nacos_config'].get('ip')}:{svc['nacos_config'].get('port')})")

    # 2. 注册到 Nacos
    result = register.register_all()
    print(f"\n{'=' * 60}")
    print(f"  Nacos 注册结果 (v2)")
    print(f"{'=' * 60}")
    print(f"  服务总数 : {result['total']}")
    print(f"  成功     : {result['successful']}")
    print(f"  失败     : {result['failed']}")
    if result["errors"]:
        for err in result["errors"]:
            print(f"  失败: {err['folder']} - {err['error']}")
    print(f"{'=' * 60}")
