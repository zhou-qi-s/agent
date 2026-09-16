"""
Nacos 服务注册模块（本地进程版）

遍历 download 目录，发现存活的进程并注册到 Nacos（幂等，重复注册无副作用）：
    1. 遍历 {download}/ 下所有子目录
    2. 检查 version 文件 → 进入 {version}/runtime/
    3. runtime/pid 存在 → 读 PID → _process_exists() 确认进程存活
       → 读取 runtime/config.yaml 提取配置 → 注册到 Nacos
       → 注册成功后写入 runtime/nacos 缓存（供心跳模块使用）

变更(v2)：ThreadPool 并发 + 指数退避重试 + token 认证 + Session 复用 + RateLimiter 限速。
变更(v3)：从 runtime/config.yaml 读取 Nacos 配置（不再依赖 application.yml）。
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
from core.process.process_info import _process_exists
from utils.config_loader import load_config

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")

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


class NacosRegister:
    """Nacos 服务注册器：发现本地进程并注册到 Nacos（v2 升级版）"""

    def __init__(self):
        self._download_base = _DOWNLOAD_BASE

    # ------------------------------------------------------------------
    # 服务发现（本地进程，文件系统方式，保持不变）
    # ------------------------------------------------------------------

    def discover_services(self) -> List[Dict[str, Any]]:
        """遍历 download 目录，发现进程存活的 Nacos 服务实例。"""
        result: List[Dict[str, Any]] = []

        if not self._download_base or not os.path.isdir(self._download_base):
            logging.warning("[%s] download 目录不存在: %s", _LOG, self._download_base)
            return result

        for entry in os.listdir(self._download_base):
            service_dir = os.path.join(self._download_base, entry)
            if not os.path.isdir(service_dir):
                continue

            version_file = os.path.join(service_dir, "version")
            if not os.path.isfile(version_file):
                continue

            try:
                with open(version_file, "r", encoding="utf-8") as vf:
                    version = vf.read().strip()
            except Exception as e:
                logging.warning("[%s] 读取 version 失败: %s -> %s", _LOG, version_file, e)
                continue

            if not version:
                continue

            version_dir = os.path.join(service_dir, version)
            runtime_dir = os.path.join(version_dir, "runtime")

            pid_file = os.path.join(runtime_dir, "pid")
            if not os.path.isfile(pid_file):
                continue

            try:
                with open(pid_file, "r", encoding="utf-8") as pf:
                    content = pf.read().strip()
            except Exception:
                logging.debug("[%s] 读取 pid 文件失败，跳过: folder=%s", _LOG, entry)
                continue

            if not content:
                continue

            # 支持多行 PID，只要有一个存活即可
            alive = False
            for line in content.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pid_int = int(line)
                except ValueError:
                    continue
                if _process_exists(pid_int):
                    alive = True
                    break

            if not alive:
                logging.info("[%s] 所有 PID 均已退出，跳过: folder=%s", _LOG, entry)
                continue

            nacos_config = self._try_discover_from_config_yaml(service_dir, version)
            if nacos_config is None:
                continue

            result.append({
                "folder_name": entry,
                "version": version,
                "nacos_config": nacos_config,
                "nacos_file": os.path.join(runtime_dir, "nacos"),
            })

        return result

    @staticmethod
    def _try_discover_from_config_yaml(service_dir: str, version: str) -> Optional[Dict[str, Any]]:
        """
        从 runtime/config.yaml 提取 Nacos 注册配置。

        读取路径: {service_dir}/runtime/config.yaml
        格式:
            version: "1.0.0"
            nacos:
              serviceName: xxx
              groupName: xxxx
              ...
        """
        config_path = os.path.join(service_dir, "runtime", "config.yaml")
        if not os.path.isfile(config_path):
            logging.info("[%s] runtime/config.yaml 不存在（service_dir=%s），跳过注册", _LOG, service_dir)
            return None

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                runtime_conf = yaml.safe_load(f) or {}
            logging.info("[%s] 读取到 runtime/config.yaml: %s", _LOG, config_path)
        except Exception as e:
            logging.warning("[%s] 读取 runtime/config.yaml 失败: %s -> %s", _LOG, config_path, e)
            return None

        if not isinstance(runtime_conf, dict):
            return None

        nacos_section = runtime_conf.get("nacos", {})
        if not isinstance(nacos_section, dict):
            nacos_section = {}

        service_name = str(nacos_section.get("serviceName", "") or "").strip()
        if not service_name:
            logging.warning("[%s] runtime/config.yaml 中未找到 nacos.serviceName: %s",
                            _LOG, config_path)
            return None

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
            "serviceName": service_name,
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
                        # 注册成功 → 写入 runtime/nacos 缓存
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
        """注册成功后写入 runtime/nacos 缓存文件。"""
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
