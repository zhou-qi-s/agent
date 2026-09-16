"""
节点级别资源采集和告警模块

- 节点资源采集（CPU/内存/IO），写入 download/node/runtime/resources.txt
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


_NODE_IO_BASELINE = {}
def _get_download_base() -> str:
    config = load_config()
    return config.get("download", {}).get("base", "/var/cache/agent/download")


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

    # IO delta(MB): current cumulative - previous cumulative
    _io_cnt = psutil.disk_io_counters()
    _cur_bytes = (_io_cnt.read_bytes + _io_cnt.write_bytes) if _io_cnt else 0
    _prev = _NODE_IO_BASELINE.get("total_bytes")
    if _prev is None:
        io_total_mb = 0.0
    else:
        io_total_mb = max(_cur_bytes - _prev, 0) / (1024 * 1024)
    _NODE_IO_BASELINE["total_bytes"] = _cur_bytes

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
        node_dir:  download/node/runtime 目录路径
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
    download_base = _get_download_base()
    node_dir = os.path.join(download_base, "node", "runtime")
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


def _can_report_node_alarm(dimension: str) -> bool:
    """节点告警冷却检查"""
    current_time = time.time()
    ip = get_local_ip()
    key = f"node_{ip}_{dimension}"

    last_time = last_alarm_time.get(key)
    if not last_time:
        last_alarm_time[key] = current_time
        return True

    elapsed = current_time - last_time
    if elapsed > ALARM_COOLDOWN:
        last_alarm_time[key] = current_time
        return True

    logger.info("[NODE-ALARM] 节点告警冷却中: type=%s 距上次 %.0f 秒 (需要 %d 秒)",
                dimension, elapsed, ALARM_COOLDOWN)
    return False


def check_and_report_node_alarms() -> Optional[list]:
    """
    节点告警检测主流程:
    1. 从 Redis alarm:node:{ip} 读阈值
    2. 从 resources.txt 读实际值
    3. 对比并上报告警
    """
    ip = get_local_ip()
    download_base = _get_download_base()
    node_dir = os.path.join(download_base, "node", "runtime")
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
        if _can_report_node_alarm("cpu"):
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
        if _can_report_node_alarm("memory"):
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
        if _can_report_node_alarm("io"):
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
