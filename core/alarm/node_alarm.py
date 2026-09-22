"""
节点级别资源采集和告警模块

- 节点资源采集（CPU/内存/IO），写入 {server.apps}/node/runtime/resources.txt
  （改造后从「缓存区」移到「运行区」，与各应用服务的产物落点保持一致；
    缓存区可被清理，节点运行数据不应放在那里）
- 节点告警检测，从 Redis alarm:node:{ip} 读取阈值并对比上报
"""

import json
import os
import time
from typing import Any, Dict, Optional

import psutil

from core.alarm.alarm_common import (
    ALARM_COOLDOWN,
    last_alarm_time,
    get_local_ip,
    report_alarm,
)

# 节点级别告警统一使用 alarmType=1
NODE_ALARM_TYPE = 1
from utils.config_loader import load_config
from utils.logger import logger


# =========================================================
# 配置
# =========================================================

# 节点资源数据的伪服务名（在运行区下表现为一个虚拟服务目录）
_NODE_SERVICE_NAME = "node"


def _get_apps_base() -> str:
    """
    获取运行区根目录（config.yaml 的 server.apps）。

    注意：原实现读的是顶层 `download.base` 键（与项目其它模块的
    `server.download` 不一致），改造后统一从 server.apps 取。
    """
    config = load_config()
    return config.get("server", {}).get("apps", "/var/cache/agent/apps")


def _get_node_dir() -> str:
    """节点资源目录：{server.apps}/node/runtime"""
    return os.path.join(_get_apps_base(), _NODE_SERVICE_NAME, "runtime")


# =========================================================
# 节点资源采集
# =========================================================

def collect_node_resources() -> Dict[str, Any]:
    """
    采集当前节点的 CPU/内存/IO 资源。

    Returns:
        {"pid": "node", "name": "node", "cpu": 45.2, "mem": 3200.5, "io": 1024.3}
    """
    cpu_percent = psutil.cpu_percent(interval=0.1)

    mem = psutil.virtual_memory()
    mem_used_mb = mem.used / (1024 * 1024)

    io_counters = psutil.disk_io_counters()
    io_total_mb = (io_counters.read_bytes + io_counters.write_bytes) / (1024 * 1024)

    return {
        "pid": "node",
        "name": "node",
        "cpu": round(cpu_percent, 1),
        "mem": round(mem_used_mb, 1),
        "io": round(io_total_mb, 1),
    }


def write_node_resources_file(node_dir: str, resources: Dict[str, Any]) -> None:
    """
    将节点资源写入 resources.txt，格式与应用级一致。

    Args:
        node_dir:  {server.apps}/node/runtime 目录路径
        resources: collect_node_resources() 的返回值
    """
    os.makedirs(node_dir, exist_ok=True)
    filepath = os.path.join(node_dir, "resources.txt")

    record = {
        "pid": "node",
        "process_info": {
            "pid": "node",
            "name": "node",
            "cpu": str(resources["cpu"]),
            "mem": str(resources["mem"]),
            "io": str(resources["io"]),
        },
        "timestamp": int(time.time() * 1000),
    }

    try:
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = []
        else:
            data = []

        data.append(record)
        if len(data) > 60:
            data = data[-60:]

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("[NODE-RESOURCE] 写入 resources.txt 失败: %s", e)


def read_node_resources(filepath: str) -> Optional[Dict[str, float]]:
    """
    从 resources.txt 读取最新一条节点资源数据。

    Returns:
        {"cpu": 45.2, "mem": 3200.5, "io": 1024.3} 或 None
    """
    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not data:
            return None

        latest = data[-1]
        proc_info = latest.get("process_info", latest)

        return {
            "cpu": float(proc_info.get("cpu", 0)),
            "mem": float(proc_info.get("mem", 0)),
            "io": float(proc_info.get("io", 0)),
        }
    except Exception as e:
        logger.warning("[NODE-RESOURCE] 读取 resources.txt 失败: %s", e)
        return None


def collect_and_write_node_resources() -> None:
    """采集节点资源并写入文件（对外接口）"""
    node_dir = _get_node_dir()
    resources = collect_node_resources()
    write_node_resources_file(node_dir, resources)
    logger.info("[NODE-RESOURCE] 采集完成: cpu=%.1f mem=%.1f io=%.1f",
                 resources["cpu"], resources["mem"], resources["io"])


# =========================================================
# 节点告警检测
# =========================================================

def read_node_alarm_from_redis() -> Dict[str, Any]:
    """
    从 Redis 读取节点告警阈值。
    Key: alarm:node:{ip}
    """
    from utils.redis_client import get_redis

    ip = get_local_ip()
    key = f"alarm:node:{ip}"

    try:
        r = get_redis()
        raw_data = r.get(key)
        if raw_data is None:
            logger.warning("[NODE-ALARM] key=%s 不存在，节点告警规则未配置", key)
            return {}

        logger.info("[NODE-ALARM] key=%s raw=%s", key, raw_data)
        result = json.loads(raw_data)

        # 兼容双重 JSON 编码
        if isinstance(result, str):
            logger.warning("[NODE-ALARM] 检测到双重JSON编码，正在二次解析: key=%s", key)
            result = json.loads(result)

        if not isinstance(result, dict):
            logger.warning("[NODE-ALARM] Redis 值不是 dict: key=%s type=%s", key, type(result).__name__)
            return {}

        return result
    except Exception as e:
        logger.warning("[NODE-ALARM] 从 Redis 读取失败: %s -> %s", key, e)
        return {}


def _can_report_node_alarm(alarm_type: int) -> bool:
    """节点告警冷却检查"""
    current_time = time.time()
    ip = get_local_ip()
    key = f"node_{ip}_{alarm_type}"

    last_time = last_alarm_time.get(key)
    if not last_time:
        last_alarm_time[key] = current_time
        return True

    elapsed = current_time - last_time
    if elapsed > ALARM_COOLDOWN:
        last_alarm_time[key] = current_time
        return True

    logger.info("[NODE-ALARM] 节点告警冷却中: type=%s 距上次 %.0f 秒 (需要 %d 秒)",
                alarm_type, elapsed, ALARM_COOLDOWN)
    return False


def check_and_report_node_alarms() -> Optional[list]:
    """
    节点告警检测主流程:
    1. 从 Redis alarm:node:{ip} 读阈值
    2. 从 resources.txt 读实际值
    3. 对比并上报告警
    """
    ip = get_local_ip()
    node_dir = _get_node_dir()
    filepath = os.path.join(node_dir, "resources.txt")

    # 读取阈值
    rules = read_node_alarm_from_redis()
    cpu_threshold = rules.get("cpu")
    memory_threshold = rules.get("memory")
    io_threshold = rules.get("io")

    if cpu_threshold is None or memory_threshold is None or io_threshold is None:
        logger.warning("[NODE-ALARM] 节点告警阈值不完整，跳过: cpu=%s mem=%s io=%s",
                     cpu_threshold, memory_threshold, io_threshold)
        return None

    try:
        cpu_threshold = float(cpu_threshold)
        memory_threshold = float(memory_threshold)
        io_threshold = float(io_threshold)
    except (ValueError, TypeError):
        logger.warning("[NODE-ALARM] 节点告警阈值格式错误: cpu=%s mem=%s io=%s",
                       cpu_threshold, memory_threshold, io_threshold)
        return None

    # 读取实际资源
    resources = read_node_resources(filepath)
    if resources is None:
        logger.warning("[NODE-ALARM] 节点 resources.txt 无数据，跳过")
        return None

    cpu_val = resources["cpu"]
    mem_val = resources["mem"]
    io_val = resources["io"]

    logger.info("[NODE-ALARM] 节点=%s 实际值: cpu=%.1f mem=%.1f io=%.1f | 阈值: cpu=%.1f mem=%.1f io=%.1f",
                ip, cpu_val, mem_val, io_val, cpu_threshold, memory_threshold, io_threshold)

    reported = []

    # ---- CPU 告警 ----
    if cpu_threshold >= 0 and cpu_val > cpu_threshold:
        logger.info("[NODE-ALARM] CPU告警触发: %.1f > %.1f", cpu_val, cpu_threshold)
        if _can_report_node_alarm(NODE_ALARM_TYPE):
            report_alarm(
                alarm_type=NODE_ALARM_TYPE,
                service_name=f"node:{ip}",
                alarm_name=f"节点 {ip} CPU告警",
                content=f"节点 {ip} CPU 使用 {cpu_val}%，超过阈值 {cpu_threshold}%",
            )
            reported.append({"type": "cpu", "value": cpu_val, "threshold": cpu_threshold})
        else:
            logger.info("[NODE-ALARM] CPU告警命中冷却，跳过")

    # ---- 内存告警 ----
    if memory_threshold >= 0 and mem_val > memory_threshold:
        logger.info("[NODE-ALARM] 内存告警触发: %.1f > %.1f", mem_val, memory_threshold)
        if _can_report_node_alarm(NODE_ALARM_TYPE):
            report_alarm(
                alarm_type=NODE_ALARM_TYPE,
                service_name=f"node:{ip}",
                alarm_name=f"节点 {ip} 内存告警",
                content=f"节点 {ip} 内存使用 {mem_val:.1f}MB，超过阈值 {memory_threshold:.1f}MB",
            )
            reported.append({"type": "memory", "value": mem_val, "threshold": memory_threshold})
        else:
            logger.info("[NODE-ALARM] 内存告警命中冷却，跳过")

    # ---- IO 告警 ----
    if io_threshold >= 0 and io_val > io_threshold:
        logger.info("[NODE-ALARM] IO告警触发: %.1f > %.1f", io_val, io_threshold)
        if _can_report_node_alarm(NODE_ALARM_TYPE):
            report_alarm(
                alarm_type=NODE_ALARM_TYPE,
                service_name=f"node:{ip}",
                alarm_name=f"节点 {ip} IO告警",
                content=f"节点 {ip} IO 使用 {io_val:.1f}MB，超过阈值 {io_threshold:.1f}MB",
            )
            reported.append({"type": "io", "value": io_val, "threshold": io_threshold})
        else:
            logger.info("[NODE-ALARM] IO告警命中冷却，跳过")

    logger.info("[NODE-ALARM] 本轮检测完成，触发 %d 条告警", len(reported))
    return reported
