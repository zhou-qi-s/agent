"""
Nacos 服务注册与心跳管理模块

本模块提供统一的 Nacos 基础设施，包括：
1. Nacos 服务注册/注销
2. 心跳线程管理
3. 实例-进程映射维护
4. 配置读取与辅助工具

供 start_task、stop_task 等任务处理器共享使用
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import psutil
import requests

from core.utils import register_to_nacos
from utils.config_loader import load_config


# =============================================================================
# 全局变量
# =============================================================================

# 加载配置文件（供 _get_nacos_runtime_config 使用）
nacos_config = load_config()

# Nacos心跳管理全局锁，用于保护以下全局数据结构
NACOS_REGISTRY_LOCK = threading.Lock()

# Nacos心跳线程字典，存储所有活跃的心跳线程
# 键: 实例唯一标识符（由nacos_address|namespace|group|cluster|service|ip|port组成）
# 值: 包含实例信息、stop_event、thread的字典
NACOS_HEARTBEAT_THREADS: Dict[str, Dict[str, Any]] = {}

# 进程与实例键的映射，用于进程退出时快速找到对应的心跳
# 键: 进程PID
# 值: 该进程关联的实例键集合
NACOS_PROCESS_INSTANCE_KEYS: Dict[int, Set[str]] = {}


# =============================================================================
# 心跳管理器（单线程统一管理所有实例的心跳）
# =============================================================================

class HeartbeatManager:
    """
    Nacos 心跳管理器

    单后台线程管理所有服务实例的心跳发送和进程存活检测：
    - 注册成功时将实例加入管理器
    - 定时检测关联进程是否存活
    - 进程存活则发送心跳，进程消失则自动停止心跳并尝试注销
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._instances: Dict[str, Dict[str, Any]] = {}     # instance_key -> instance_info
        self._pid_to_keys: Dict[int, Set[str]] = {}          # pid -> instance_keys
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._interval: int = 5

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self, interval: int = 5) -> None:
        """
        启动心跳管理器（全局单例，重复调用安全）

        参数:
            interval: 心跳间隔（秒）
        """
        if self._thread and self._thread.is_alive():
            return
        self._interval = max(1, int(interval))
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="nacos-heartbeat-manager"
        )
        self._thread.start()
        logging.info("[HeartbeatManager] 心跳管理器已启动, interval=%ds, thread=%s",
                     self._interval, self._thread.name)

    def stop(self) -> None:
        """停止心跳管理器"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        self._thread = None
        logging.info("[HeartbeatManager] 心跳管理器已停止")

    # ------------------------------------------------------------------
    # 实例管理
    # ------------------------------------------------------------------

    def add_instance(self, instance: Dict[str, Any]) -> str:
        """
        添加实例到心跳管理器

        参数:
            instance: 实例信息，需包含 service_name/ip/port/process_id/nacos_address 等

        返回:
            str: 实例唯一标识键
        """
        key = _build_nacos_instance_key(instance)
        pid = instance.get("process_id")

        with self._lock:
            self._instances[key] = dict(instance)
            self._instances[key]["last_heartbeat"] = time.time()
            self._instances[key]["heartbeat_status"] = "running"
            self._instances[key]["heartbeat_error"] = ""
            if pid is not None:
                pid_int = self._parse_pid(pid)
                if pid_int is not None:
                    self._pid_to_keys.setdefault(pid_int, set()).add(key)

        logging.info(
            "[HeartbeatManager] 添加实例: service=%s, ip=%s, port=%s, pid=%s, key=%s",
            instance.get("service_name"), instance.get("ip"), instance.get("port"), pid, key
        )
        return key

    def remove_instance(self, instance_key: str) -> Optional[Dict[str, Any]]:
        """
        移除单个实例

        参数:
            instance_key: 实例唯一标识键

        返回:
            dict 或 None: 被移除的实例信息
        """
        with self._lock:
            instance = self._instances.pop(instance_key, None)
            if instance:
                pid = instance.get("process_id")
                if pid is not None:
                    pid_int = self._parse_pid(pid)
                    if pid_int is not None:
                        keys = self._pid_to_keys.get(pid_int)
                        if keys:
                            keys.discard(instance_key)
                            if not keys:
                                self._pid_to_keys.pop(pid_int, None)
        if instance:
            logging.info("[HeartbeatManager] 移除实例: service=%s, ip=%s, port=%s, key=%s",
                         instance.get("service_name"), instance.get("ip"), instance.get("port"), instance_key)
        return instance

    def remove_by_pid(self, pid: int) -> List[Dict[str, Any]]:
        """
        根据 PID 移除所有关联实例

        参数:
            pid: 进程PID

        返回:
            list: 被移除的实例列表
        """
        with self._lock:
            keys = list(self._pid_to_keys.pop(pid, set()))
        removed = []
        for key in keys:
            inst = self.remove_instance(key)
            if inst:
                removed.append(inst)
        return removed

    def get_instances_by_pid(self, pid: int) -> List[Dict[str, Any]]:
        """
        根据 PID 获取关联的实例（不修改状态）

        参数:
            pid: 进程PID

        返回:
            list: 实例信息列表
        """
        with self._lock:
            keys = self._pid_to_keys.get(pid, set())
            return [dict(self._instances[k]) for k in keys if k in self._instances]

    def get_statistics(self) -> Dict[str, Any]:
        """获取心跳管理器统计信息"""
        with self._lock:
            return {
                "instance_count": len(self._instances),
                "pid_count": len(self._pid_to_keys),
                "interval": self._interval,
                "running": self._thread is not None and self._thread.is_alive(),
                "instances": [dict(v) for v in self._instances.values()]
            }

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """心跳管理器主循环，在独立线程中运行"""
        runtime_cfg = _get_nacos_runtime_config()

        while not self._stop_event.wait(self._interval):
            dead_keys: List[tuple] = []    # (key, pid, instance)

            with self._lock:
                for key, instance in list(self._instances.items()):
                    pid = instance.get("process_id")
                    if pid is not None:
                        pid_int = self._parse_pid(pid)
                        if pid_int is not None and not self._is_process_alive(pid_int):
                            dead_keys.append((key, pid_int, instance))

            # 移除已死进程的实例
            for key, pid, instance in dead_keys:
                logging.info(
                    "[HeartbeatManager] 进程已退出，移除实例: key=%s, pid=%d, service=%s",
                    key, pid, instance.get("service_name")
                )
                self.remove_instance(key)
                self._try_deregister(instance)

            # 发送心跳
            with self._lock:
                heartbeat_tasks = [(key, dict(inst)) for key, inst in self._instances.items()]

            for key, instance in heartbeat_tasks:
                try:
                    _send_nacos_heartbeat(instance)
                    with self._lock:
                        if key in self._instances:
                            self._instances[key]["last_heartbeat"] = time.time()
                            self._instances[key]["heartbeat_status"] = "running"
                            self._instances[key]["heartbeat_error"] = ""
                except Exception as e:
                    logging.warning("[HeartbeatManager] 心跳失败: key=%s, error=%s", key, e)
                    with self._lock:
                        if key in self._instances:
                            self._instances[key]["heartbeat_status"] = "failed"
                            self._instances[key]["heartbeat_error"] = str(e)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_pid(pid) -> Optional[int]:
        """解析 PID，支持多行字符串，返回第一个有效值"""
        if pid is None:
            return None
        if isinstance(pid, int):
            return pid
        try:
            for line in str(pid).strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    return int(line)
                except ValueError:
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """检测进程是否存活"""
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return False

    def _try_deregister(self, instance: Dict[str, Any]) -> None:
        """尝试从 Nacos 注销已死进程的实例"""
        try:
            deregistered, failed = _deregister_nacos_instances([instance])
            if deregistered:
                logging.info("[HeartbeatManager] 已自动注销: service=%s, ip=%s, port=%s",
                             instance.get("service_name"), instance.get("ip"), instance.get("port"))
            if failed:
                logging.warning("[HeartbeatManager] 自动注销失败: %s", failed)
        except Exception as e:
            logging.warning("[HeartbeatManager] 注销异常: %s", e)


# 全局单例
_heartbeat_manager: Optional[HeartbeatManager] = None


def get_heartbeat_manager() -> HeartbeatManager:
    """获取全局心跳管理器单例"""
    global _heartbeat_manager
    if _heartbeat_manager is None:
        _heartbeat_manager = HeartbeatManager()
    return _heartbeat_manager


# =============================================================================
# Nacos配置与辅助函数
# =============================================================================

def _get_nacos_runtime_config() -> Dict[str, Any]:
    """
    获取Nacos运行时配置

    从配置文件中读取Nacos相关配置，并提供默认值处理。
    支持从多个配置项读取，兼容不同的配置风格。

    返回:
        dict: Nacos配置字典，包含:
            - address: Nacos服务器地址
            - namespace_id: 命名空间ID
            - group_name: 分组名称
            - cluster_name: 集群名称
            - username/password: 认证信息
            - register/deregister/heartbeat: API路径
            - heartbeat_interval: 心跳间隔（秒）
            - request_timeout: 请求超时（秒）
    """
    nacos_cfg = nacos_config.get("nacos", {}) if isinstance(nacos_config, dict) else {}
    agent_cfg = nacos_config.get("agent", {}) if isinstance(nacos_config, dict) else {}
    heartbeat_cfg = agent_cfg.get("heartbeat", {}) if isinstance(agent_cfg, dict) else {}
    api_cfg = nacos_cfg.get("api", {}) if isinstance(nacos_cfg.get("api"), dict) else {}

    # 读取心跳间隔，支持多种配置项名称
    heartbeat_interval_raw = nacos_cfg.get("heartbeat_interval", heartbeat_cfg.get("interval", 5))
    request_timeout_raw = nacos_cfg.get("request_timeout", 10)

    # 转换为整数并确保有效范围
    try:
        heartbeat_interval = max(1, int(heartbeat_interval_raw))
    except (TypeError, ValueError):
        heartbeat_interval = 5

    try:
        request_timeout = max(3, int(request_timeout_raw))
    except (TypeError, ValueError):
        request_timeout = 10

    # 构建Nacos地址
    address = str(nacos_cfg.get("address", "") or "").strip()
    nacos_ip = str(nacos_cfg.get("ip", "") or "").strip()
    nacos_port = str(nacos_cfg.get("port", "") or "").strip()
    if not address and nacos_ip:
        base_address = nacos_ip.rstrip("/")
        if not base_address.startswith(("http://", "https://")):
            base_address = f"http://{base_address}"
        if nacos_port and ":" not in base_address.rsplit("/", 1)[-1]:
            address = f"{base_address}:{nacos_port}"
        else:
            address = base_address

    return {
        "address": address,
        "namespace_id": str(nacos_cfg.get("namespace_id", nacos_cfg.get("namespace", "")) or "").strip(),
        "group_name": str(
            nacos_cfg.get("group_name", nacos_cfg.get("group", "DEFAULT_GROUP")) or "DEFAULT_GROUP").strip(),
        "cluster_name": str(nacos_cfg.get("cluster_name", "DEFAULT") or "DEFAULT").strip(),
        "username": nacos_cfg.get("username"),
        "password": nacos_cfg.get("password"),
        "register": str(api_cfg.get("register", nacos_cfg.get("register",
                                                              "/nacos/v1/ns/instance")) or "/nacos/v1/ns/instance").strip(),
        "deregister": str(api_cfg.get("deregister", nacos_cfg.get("deregister",
                                                                  "/nacos/v1/ns/instance")) or "/nacos/v1/ns/instance").strip(),
        "heartbeat": str(api_cfg.get("heartbeat", nacos_cfg.get("heartbeat_api",
                                                                "/nacos/v1/ns/instance/beat")) or "/nacos/v1/ns/instance/beat").strip(),
        "weight": nacos_cfg.get("weight", 1.0),
        "healthy": nacos_cfg.get("healthy", True),
        "enabled": nacos_cfg.get("enabled", True),
        "ephemeral": nacos_cfg.get("ephemeral", True),
        "metadata": nacos_cfg.get("metadata", {}),
        "heartbeat_interval": heartbeat_interval,
        "request_timeout": request_timeout
    }


def _get_nacos_auth(runtime_cfg: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """
    获取Nacos认证信息

    参数:
        runtime_cfg: Nacos运行时配置

    返回:
        tuple: (username, password) 或 None（如果未配置认证）
    """
    username = runtime_cfg.get("username")
    password = runtime_cfg.get("password")
    if username or password:
        return str(username or ""), str(password or "")
    return None


def _parse_nacos_bool(value: Any, default: bool = False) -> bool:
    """
    解析Nacos布尔值

    Nacos支持多种布尔表示方式：
        - 字符串: "true", "1", "yes", "on" -> True
        - 字符串: "false", "0", "no", "off" -> False

    参数:
        value: 原始值
        default: 默认值

    返回:
        bool: 解析后的布尔值
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)


def _to_nacos_bool(value: Any, default: bool = False) -> str:
    """
    将值转换为Nacos布尔字符串

    参数:
        value: 原始值
        default: 默认值

    返回:
        str: "true" 或 "false"
    """
    return "true" if _parse_nacos_bool(value, default) else "false"


def _serialize_nacos_metadata(metadata: Any) -> str:
    """
    序列化Nacos元数据

    将元数据转换为JSON字符串格式

    参数:
        metadata: 元数据（可以是字符串、字典等）

    返回:
        str: JSON字符串
    """
    if metadata is None:
        return ""
    if isinstance(metadata, str):
        return metadata.strip()
    try:
        return json.dumps(metadata, ensure_ascii=False)
    except TypeError:
        return json.dumps(str(metadata), ensure_ascii=False)


def _build_nacos_instance_key(instance: Dict[str, Any]) -> str:
    """
    构建Nacos实例唯一标识键

    使用多个维度构建唯一键，确保不同实例不会冲突：
        nacos_address|namespace_id|group_name|cluster_name|service_name|ip|port

    参数:
        instance: 实例信息字典

    返回:
        str: 实例唯一标识键
    """
    return "|".join([
        str(instance.get("nacos_address", "") or ""),
        str(instance.get("namespace_id", "") or ""),
        str(instance.get("group_name", "DEFAULT_GROUP") or "DEFAULT_GROUP"),
        str(instance.get("cluster_name", "DEFAULT") or "DEFAULT"),
        str(instance.get("service_name", "") or ""),
        str(instance.get("ip", "") or ""),
        str(instance.get("port", "") or "")
    ])


def _remove_instance_key_from_process_map(instance_key: str) -> None:
    """
    从进程映射中移除实例键

    当心跳停止时，清理进程与实例的关联关系

    参数:
        instance_key: 实例唯一标识键
    """
    empty_pids = []
    for pid, keys in NACOS_PROCESS_INSTANCE_KEYS.items():
        if instance_key in keys:
            keys.discard(instance_key)
        if not keys:
            empty_pids.append(pid)

    # 清理空集合
    for pid in empty_pids:
        NACOS_PROCESS_INSTANCE_KEYS.pop(pid, None)


def _stop_heartbeat_entries(instance_keys: Set[str]) -> List[Dict[str, Any]]:
    """
    停止指定实例的心跳线程

    流程：
        1. 获取心跳条目快照
        2. 设置停止事件
        3. 等待线程结束
        4. 清理全局数据结构

    参数:
        instance_keys: 要停止的实例键集合

    返回:
        list: 已停止的实例信息列表
    """
    snapshot: List[Dict[str, Any]] = []
    heartbeat_entries: List[Tuple[str, Any, Any]] = []

    # 1. 在锁保护下获取心跳条目
    with NACOS_REGISTRY_LOCK:
        for instance_key in instance_keys:
            entry = NACOS_HEARTBEAT_THREADS.get(instance_key)
            if not entry:
                continue
            # 排除不可序列化的对象
            snapshot.append({k: v for k, v in entry.items() if k not in {"stop_event", "thread"}})
            heartbeat_entries.append((instance_key, entry.get("stop_event"), entry.get("thread")))

    # 2. 在锁外停止线程（避免死锁）
    for _, stop_event, thread in heartbeat_entries:
        if stop_event:
            stop_event.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)

    # 3. 清理全局数据结构
    with NACOS_REGISTRY_LOCK:
        for instance_key in instance_keys:
            NACOS_HEARTBEAT_THREADS.pop(instance_key, None)
            _remove_instance_key_from_process_map(instance_key)

    return snapshot


def _collect_instances_by_pids(pids: List[int]) -> List[Dict[str, Any]]:
    """
    根据PID收集关联的Nacos实例

    用于停止任务时，根据进程PID找到需要下线的Nacos实例

    参数:
        pids: 进程ID列表

    返回:
        list: 关联的实例信息列表
    """
    with NACOS_REGISTRY_LOCK:
        instance_keys: Set[str] = set()
        for pid in pids:
            instance_keys.update(NACOS_PROCESS_INSTANCE_KEYS.get(pid, set()))
        return [
            {k: v for k, v in NACOS_HEARTBEAT_THREADS[key].items() if k not in {"stop_event", "thread"}}
            for key in instance_keys
            if key in NACOS_HEARTBEAT_THREADS
        ]


def _deregister_nacos_instances(instances: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    从Nacos注销实例

    发送HTTP DELETE请求到Nacos，注销服务实例

    参数:
        instances: 要注销的实例列表

    返回:
        tuple: (deregistered_list, failed_list)
    """
    runtime_cfg = _get_nacos_runtime_config()
    nacos_address = str(runtime_cfg.get("address", "") or "").strip()
    if not nacos_address:
        return [], [{"error": "未配置 Nacos 地址", "instances": instances}]

    deregister_api = str(runtime_cfg.get("deregister", runtime_cfg.get("register",
                                                                       "/nacos/v1/ns/instance")) or "/nacos/v1/ns/instance").strip()
    deregister_url = f"{nacos_address.rstrip('/')}{deregister_api}"
    auth = _get_nacos_auth(runtime_cfg)
    request_timeout = runtime_cfg.get("request_timeout", 10)
    deregistered: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for instance in instances:
        try:
            payload = {
                "serviceName": instance.get("service_name", ""),
                "ip": instance.get("ip", ""),
                "port": int(instance.get("port", 0)),
                "groupName": instance.get("group_name", runtime_cfg.get("group_name", "DEFAULT_GROUP")),
                "clusterName": instance.get("cluster_name", runtime_cfg.get("cluster_name", "DEFAULT")),
                "ephemeral": _to_nacos_bool(instance.get("ephemeral", runtime_cfg.get("ephemeral", True)), True)
            }
            namespace_id = str(instance.get("namespace_id", runtime_cfg.get("namespace_id", "")) or "").strip()
            if namespace_id:
                payload["namespaceId"] = namespace_id

            response = requests.delete(deregister_url, params=payload, auth=auth, timeout=request_timeout)
            response.raise_for_status()
            deregistered.append({
                "service_name": payload["serviceName"],
                "ip": payload["ip"],
                "port": payload["port"],
                "process_id": instance.get("process_id"),
                "process_name": instance.get("process_name"),
                "response": response.text.strip()
            })
        except Exception as e:
            failed.append({
                "service_name": instance.get("service_name", ""),
                "ip": instance.get("ip", ""),
                "port": instance.get("port"),
                "process_id": instance.get("process_id"),
                "process_name": instance.get("process_name"),
                "error": str(e)
            })

    return deregistered, failed


def _send_nacos_heartbeat(instance: Dict[str, Any]) -> None:
    """
    发送单次Nacos心跳

    发送HTTP PUT请求到Nacos心跳接口，保活服务实例

    参数:
        instance: 实例信息字典

    异常:
        RuntimeError: 未配置Nacos地址
        requests.RequestException: HTTP请求失败
    """
    runtime_cfg = _get_nacos_runtime_config()
    nacos_address = str(runtime_cfg.get("address", "") or "").strip()
    if not nacos_address:
        raise RuntimeError("未配置 Nacos 地址")

    heartbeat_api = str(
        runtime_cfg.get("heartbeat", "/nacos/v1/ns/instance/beat") or "/nacos/v1/ns/instance/beat").strip()
    beat_url = f"{nacos_address.rstrip('/')}{heartbeat_api}"
    auth = _get_nacos_auth(runtime_cfg)
    request_timeout = runtime_cfg.get("request_timeout", 10)
    service_name = str(instance.get("service_name", "") or "")
    ip = str(instance.get("ip", "") or "")
    port = int(instance.get("port", 0))
    cluster_name = str(instance.get("cluster_name", runtime_cfg.get("cluster_name", "DEFAULT")) or "DEFAULT")
    group_name = str(instance.get("group_name", runtime_cfg.get("group_name", "DEFAULT_GROUP")) or "DEFAULT_GROUP")
    namespace_id = str(instance.get("namespace_id", runtime_cfg.get("namespace_id", "")) or "").strip()

    # 构建beat信息JSON
    beat_info = {
        "ip": ip,
        "port": port,
        "serviceName": service_name,
        "cluster": cluster_name,
        "scheduled": True
    }
    payload = {
        "serviceName": service_name,
        "groupName": group_name,
        "clusterName": cluster_name,
        "ephemeral": "true",
        "beat": json.dumps(beat_info, ensure_ascii=False)
    }
    if namespace_id:
        payload["namespaceId"] = namespace_id

    response = requests.put(beat_url, params=payload, auth=auth, timeout=request_timeout)
    response.raise_for_status()


def _nacos_heartbeat_loop(instance_key: str, stop_event: threading.Event) -> None:
    """
    Nacos心跳循环线程函数

    持续发送心跳，直到：
        1. 收到停止信号（stop_event被设置）
        2. 关联进程已退出

    参数:
        instance_key: 实例唯一标识键
        stop_event: 线程停止事件
    """
    while not stop_event.is_set():
        # 获取实例信息
        with NACOS_REGISTRY_LOCK:
            instance = NACOS_HEARTBEAT_THREADS.get(instance_key)
        if not instance:
            return

        # 检查关联进程是否存活
        process_id = instance.get("process_id")
        if process_id and not psutil.pid_exists(int(process_id)):
            logging.info(f"[Nacos心跳] 进程已退出，停止心跳: key={instance_key}, pid={process_id}")
            stop_event.set()
            break

        # 等待心跳间隔或停止信号
        wait_seconds = int(instance.get("heartbeat_interval", 5) or 5)
        if stop_event.wait(wait_seconds):
            break

        # 发送心跳
        try:
            _send_nacos_heartbeat(instance)
            with NACOS_REGISTRY_LOCK:
                if instance_key in NACOS_HEARTBEAT_THREADS:
                    NACOS_HEARTBEAT_THREADS[instance_key]["last_heartbeat_time"] = time.time()
                    NACOS_HEARTBEAT_THREADS[instance_key]["heartbeat_status"] = "running"
                    NACOS_HEARTBEAT_THREADS[instance_key]["heartbeat_error"] = ""
        except Exception as e:
            logging.warning(f"[Nacos心跳] 心跳失败: key={instance_key}, error={e}")
            with NACOS_REGISTRY_LOCK:
                if instance_key in NACOS_HEARTBEAT_THREADS:
                    NACOS_HEARTBEAT_THREADS[instance_key]["heartbeat_status"] = "failed"
                    NACOS_HEARTBEAT_THREADS[instance_key]["heartbeat_error"] = str(e)

    # 清理心跳条目
    _stop_heartbeat_entries({instance_key})


def _start_nacos_heartbeats(instances: List[Dict[str, Any]], heartbeat_interval: int) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    启动Nacos心跳（统一的心跳管理入口）

    将实例注册到 HeartbeatManager，由单一后台线程统一管理所有心跳。

    参数:
        instances: 实例信息列表
        heartbeat_interval: 心跳间隔（秒）

    返回:
        tuple: (started_list, failed_list)
    """
    started: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    mgr = get_heartbeat_manager()
    mgr.start(heartbeat_interval)

    for instance in instances:
        try:
            mgr.add_instance(instance)
            started.append({
                "service_name": instance.get("service_name"),
                "ip": instance.get("ip"),
                "port": instance.get("port"),
                "process_id": instance.get("process_id"),
                "process_name": instance.get("process_name"),
                "heartbeat_interval": heartbeat_interval
            })
        except Exception as e:
            failed.append({
                "service_name": instance.get("service_name"),
                "ip": instance.get("ip"),
                "port": instance.get("port"),
                "process_id": instance.get("process_id"),
                "process_name": instance.get("process_name"),
                "error": str(e)
            })

    return started, failed


# =============================================================================
# 进程端口检测
# =============================================================================

def _get_process_listening_ports(pid: int) -> List[int]:
    """获取指定进程的 TCP 监听端口列表"""
    try:
        proc = psutil.Process(pid)
        ports: Set[int] = set()
        for conn in proc.net_connections(kind='tcp'):
            if conn.status == 'LISTEN' and conn.laddr:
                ports.add(conn.laddr.port)
        return sorted(ports)
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return []


# =============================================================================
# Nacos 注册辅助（best-effort）
# =============================================================================

def _try_register_to_nacos(
    nacos_addr: str,
    service_name: str,
    ip: str,
    ports: List[int],
    group_name: str,
    namespace_id: str,
    cluster_name: str,
    ephemeral: bool,
    heartbeat_interval: int,
    service_pid: Optional[int]
) -> Tuple[List[int], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    尝试注册到 Nacos 并启动心跳（best-effort，失败不回滚）

    返回:
        tuple: (registered_ports, nacos_failed, heartbeat_started, heartbeat_failed)
    """
    registered: List[int] = []
    nacos_failed: List[Dict[str, Any]] = []
    heartbeat_started: List[Dict[str, Any]] = []
    heartbeat_failed: List[Dict[str, Any]] = []

    if not nacos_addr:
        logging.info("[进程监控] 未配置 Nacos 地址，跳过注册")
        return registered, nacos_failed, heartbeat_started, heartbeat_failed

    if not ports:
        logging.info("[进程监控] 无监听端口，跳过 Nacos 注册")
        return registered, nacos_failed, heartbeat_started, heartbeat_failed

    try:
        registered, nacos_failed_list = register_to_nacos(
            nacos_addr=nacos_addr,
            service_name=service_name,
            ip=ip,
            ports=ports,
            group_name=group_name,
            namespace_id=namespace_id,
            cluster_name=cluster_name,
            ephemeral=ephemeral
        )
        for port, err in nacos_failed_list:
            nacos_failed.append({"port": port, "error": err})

        if registered:
            instances = []
            for port in registered:
                instances.append({
                    "service_name": service_name,
                    "ip": ip,
                    "port": port,
                    "process_id": service_pid,
                    "process_name": service_name,
                    "nacos_address": nacos_addr,
                    "group_name": group_name,
                    "cluster_name": cluster_name,
                    "namespace_id": namespace_id,
                    "ephemeral": ephemeral
                })
            heartbeat_started, heartbeat_failed = _start_nacos_heartbeats(instances, heartbeat_interval)
            if heartbeat_failed:
                logging.warning(f"[进程监控] 部分心跳启动失败: {heartbeat_failed}")
        else:
            logging.warning(f"[进程监控] Nacos 注册失败: {nacos_failed}")

    except Exception as e:
        logging.warning(f"[进程监控] Nacos 注册异常(不影响启动): {e}")
        nacos_failed.append({"error": str(e)})

    return registered, nacos_failed, heartbeat_started, heartbeat_failed


# =============================================================================
# 进程监控上报
# =============================================================================

def _report_process_to_server(
    instance_id: str,
    process_name: str,
    pid: int,
    ports: List[int],
    registered_ports: List[int],
    ip: str,
    nacos_addr: str,
) -> None:
    """
    监控到目标进程启动并注册 Nacos 后，调用接口上报给服务端

    参数:
        instance_id: 实例ID
        process_name: 进程名称
        pid: 进程PID
        ports: 检测到的监听端口
        registered_ports: Nacos 注册成功的端口
        ip: 本机IP
        nacos_addr: Nacos 地址
    """
    try:
        cfg = load_config()
        server_ip = ""
        server_port = ""
        server_cfg = cfg.get("server", {}) if isinstance(cfg, dict) else {}
        if isinstance(server_cfg, dict):
            server_ip = str(server_cfg.get("iP", server_cfg.get("ip", "")) or "").strip()
            server_port = str(server_cfg.get("port", "") or "").strip()
        interface_cfg = cfg.get("interface", {}) if isinstance(cfg, dict) else {}
        xkt_report_path = str(interface_cfg.get("xkt_report", "/api/agent/xkt_report") or "").strip()
        report_url = f"{server_ip}:{server_port}{xkt_report_path}"

        if not server_ip:
            logging.warning("[进程监控上报] 未配置 server.ip，跳过上报")
            return

        payload: Dict[str, Any] = {
            "ip": ip,
            "instance_id": instance_id,
            "result": len(registered_ports) > 0,
            "task_type": "process_started",
            "message": f"监控到进程 {process_name} 已启动, Nacos注册端口: {registered_ports}",
            "data": {
                "status": "process_started",
                "process_name": process_name,
                "pid": pid,
                "ports": ports,
                "registered_ports": registered_ports,
                "nacos_address": nacos_addr,
            },
            "timestamp": datetime.now().isoformat(),
        }
        response = requests.post(report_url, json=payload, timeout=10)
        if response.status_code == 200:
            logging.info(
                "[进程监控上报] 上报成功: instance_id=%s, process=%s, pid=%d, ports=%s",
                instance_id, process_name, pid, registered_ports,
            )
        else:
            logging.warning(
                "[进程监控上报] 上报返回非200: status=%d, body=%s",
                response.status_code, response.text[:200],
            )
    except Exception as e:
        logging.warning("[进程监控上报] 上报失败(best-effort): %s", e)


# =============================================================================
# 进程监控线程
# =============================================================================

def _monitor_processes_thread(
    process_names: List[str],
    nacos_addr: str,
    service_name: str,
    ip: str,
    group_name: str,
    namespace_id: str,
    cluster_name: str,
    ephemeral: bool,
    heartbeat_interval: int,
    stop_event: threading.Event,
    instance_id: str = "",
    scan_interval: float = 5.0,
) -> None:
    """
    后台监控线程：持续扫描进程名称列表，发现新进程后注册到 Nacos 并上报

    流程:
        1. 每 scan_interval 秒扫描一次系统进程
        2. 匹配 process_names 中的进程名
        3. 对新发现的进程，检测其监听端口
        4. 有监听端口 → 注册到 Nacos → 加入心跳发送器 → 上报 instance_id
        5. 已注册的 PID 不再重复处理
    """
    registered_pids: Set[int] = set()
    logging.info("[进程监控] 监控线程已启动, process_names=%s, instance_id=%s, scan_interval=%.1fs",
                 process_names, instance_id, scan_interval)

    while not stop_event.is_set():
        for proc_name in process_names:
            try:
                for proc in psutil.process_iter(['pid', 'name']):
                    try:
                        if proc.info['name'] != proc_name:
                            continue
                        if proc.pid in registered_pids:
                            continue

                        ports = _get_process_listening_ports(proc.pid)
                        if not ports:
                            continue

                        logging.info(
                            "[进程监控] 发现新进程: name=%s, pid=%d, ports=%s",
                            proc_name, proc.pid, ports
                        )

                        registered_ports, nacos_failed, hb_started, hb_failed = _try_register_to_nacos(
                            nacos_addr=nacos_addr,
                            service_name=service_name,
                            ip=ip,
                            ports=ports,
                            group_name=group_name,
                            namespace_id=namespace_id,
                            cluster_name=cluster_name,
                            ephemeral=ephemeral,
                            heartbeat_interval=heartbeat_interval,
                            service_pid=proc.pid
                        )

                        if registered_ports:
                            registered_pids.add(proc.pid)
                            logging.info(
                                "[进程监控] 已注册到 Nacos: pid=%d, ports=%s",
                                proc.pid, registered_ports
                            )
                            # 上报给服务端
                            if instance_id:
                                _report_process_to_server(
                                    instance_id=instance_id,
                                    process_name=proc_name,
                                    pid=proc.pid,
                                    ports=ports,
                                    registered_ports=registered_ports,
                                    ip=ip,
                                    nacos_addr=nacos_addr,
                                )
                        if nacos_failed:
                            logging.warning(
                                "[进程监控] 部分注册失败: pid=%d, failed=%s",
                                proc.pid, nacos_failed
                            )

                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
            except Exception as e:
                logging.warning("[进程监控] 扫描异常: %s", e)

        stop_event.wait(scan_interval)

    # 清理：移除已退出进程的注册记录
    if registered_pids:
        alive = {pid for pid in registered_pids if psutil.pid_exists(pid)}
        gone = registered_pids - alive
        if gone:
            logging.info("[进程监控] 监控线程停止，已退出进程: %s", sorted(gone))

    logging.info("[进程监控] 监控线程已停止")
