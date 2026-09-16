"""
进程资源告警模块

功能：
1. 遍历 download 目录，发现所有运行中的服务
2. 从 Redis alarm:{ip}:{service_name} 读取告警阈值
3. 查询进程 CPU / 内存 / IO 使用情况
4. 判断是否超出阈值
5. 上报告警到服务端
6. 将采集数据写入 runtime/resources.txt

目录结构：
{download_base}/
  my-service/
    version          ← 版本号
    app/
      {version}/
        runtime/
          pid              ← 进程 PID
          resources.txt    ← 采集数据 JSON

Redis 告警阈值键格式：
Key:   alarm:{ip}:{service_name}
Value: {"service_name":"my-service","cpu":"80","memory":"512","io":"100"}

字段说明：
cpu     -> CPU 阈值 (%)
memory  -> 内存阈值 (MB)
io      -> IO阈值 (MB)

CPU 采集说明：
方法每 30 秒调用一次，CPU 使用率为两次采集间隔内的平均值。
首次采集返回 0，后续基于 cpu_times 差值计算。
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import psutil

from utils.config_loader import load_config
from utils.logger import logger
from utils.redis_client import get_redis

# ---- 复用 alarm_common.py 的常量、缓存和上报能力 ----
from core.alarm.alarm_common import (
    ALARM_COOLDOWN,
    ALARM_TYPE_CPU,
    ALARM_TYPE_IO,
    ALARM_TYPE_MEMORY,
    ALARM_TYPE_PROCESS_NOT_FOUND,
    get_local_ip,
    last_alarm_time,
    report_alarm,
)

# =========================================================
# 全局配置
# =========================================================
_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")


# =========================================================
# CPU 基线缓存
#
# 用于计算两次采集间隔内的 CPU 使用率。
# 格式: {pid: {"user": float, "system": float, "timestamp": float}}
# =========================================================
_cpu_baseline: Dict[int, Dict[str, float]] = {}

# =========================================================
# IO 基线缓存
#
# 用于计算两次采集间隔内的 IO 增量。
# 格式: {pid: {"read_bytes": float, "write_bytes": float, "timestamp": float}}
# =========================================================
_io_baseline: Dict[int, Dict[str, float]] = {}


def _get_cpu_first_call(pid: int) -> float:
    """
    首次采集：返回 0，等待下次调用时基于 cpu_times 基线计算。

    Args:
        pid: 进程 ID

    Returns:
        float: CPU 百分比，首次调用固定返回 0
    """
    return 0.0


def _get_cpu_subsequent(pid: int, process: psutil.Process) -> float:
    """
    后续采集：基于 cpu_times 差值计算间隔内的平均 CPU 使用率。

    Args:
        pid:     进程 ID
        process: psutil.Process 对象

    Returns:
        float: CPU 百分比
    """
    try:
        cpu_times = process.cpu_times()
        current_user = cpu_times.user
        current_system = cpu_times.system
    except Exception:
        # 读取失败，重置基线
        _cpu_baseline.pop(pid, None)
        return 0.0

    baseline = _cpu_baseline.get(pid)
    if not baseline:
        return 0.0

    delta_user = current_user - baseline["user"]
    delta_system = current_system - baseline["system"]
    delta_time = time.time() - baseline["timestamp"]

    if delta_time <= 0:
        return 0.0

    cpu_percent = ((delta_user + delta_system) / delta_time) * 100.0 / psutil.cpu_count()
    return round(cpu_percent, 2)


def check_process_resource(pid: int) -> Optional[Dict]:
    """
    查询进程资源使用情况。

    CPU 采集逻辑：
    - 首次采集该 PID → 返回 0（无基线无法计算）
    - 后续采集 → 基于 cpu_times 差值 / 时间间隔计算

    Args:
        pid: 进程 ID

    Returns:
        {
            "pid": 12345,
            "name": "python",
            "cpu": 95.5,
            "memory": 2048.0,
            "io": 800.0
        }

        进程不存在返回 None
    """
    try:
        process = psutil.Process(pid)
        current_time = time.time()

        # ---- CPU ----
        cpu_times = process.cpu_times()

        if pid in _cpu_baseline:
            # 后续采集：用差值计算
            cpu_percent = _get_cpu_subsequent(pid, process)
        else:
            # 首次采集：用 ps -o pcpu
            cpu_percent = _get_cpu_first_call(pid)

        # 更新基线（供下次调用使用）
        _cpu_baseline[pid] = {
            "user": cpu_times.user,
            "system": cpu_times.system,
            "timestamp": current_time,
        }

        # ---- 内存（RSS物理内存） ----
        memory_mb = process.memory_info().rss / 1024 / 1024

        # ---- IO统计（间隔增量，非累计） ----
        io_mb = 0.0
        try:
            io_counter = process.io_counters()
            current_read = io_counter.read_bytes
            current_write = io_counter.write_bytes

            io_baseline = _io_baseline.get(pid)
            if io_baseline:
                delta_read = current_read - io_baseline["read_bytes"]
                delta_write = current_write - io_baseline["write_bytes"]
                io_mb = (delta_read + delta_write) / 1024 / 1024
                if io_mb < 0:
                    io_mb = 0.0

            # 更新 IO 基线
            _io_baseline[pid] = {
                "read_bytes": current_read,
                "write_bytes": current_write,
            }
        except Exception:
            pass

        return {
            "pid": pid,
            "name": process.name(),
            "cpu": round(cpu_percent, 2),
            "memory": round(memory_mb, 2),
            "io": round(io_mb, 2),
        }

    except psutil.NoSuchProcess:
        # 进程已退出，清除基线
        _cpu_baseline.pop(pid, None)
        _io_baseline.pop(pid, None)
        return None

    except Exception as e:
        print(f"[ALARM] 查询进程失败 pid={pid}: {e}")
        return None


def can_report_alarm(pid: int, alarm_type: int) -> bool:
    """
    是否允许上报告警（冷却机制）
    """
    current_time = time.time()
    key = f"{pid}_{alarm_type}"
    last_time = last_alarm_time.get(key)

    if not last_time:
        last_alarm_time[key] = current_time
        return True

    elapsed = current_time - last_time
    if elapsed > ALARM_COOLDOWN:
        last_alarm_time[key] = current_time
        return True

    logger.info("[ALARM-COOLDOWN] PID=%s alarmType=%s 冷却中 (距上次 %.0f 秒, 需要 %d 秒)",
                 pid, alarm_type, elapsed, ALARM_COOLDOWN)
    return False


def _get_alarm_type_name(alarm_type: int) -> str:
    """获取告警类型名称"""
    mapping = {
        ALARM_TYPE_CPU: "CPU告警",
        ALARM_TYPE_MEMORY: "内存告警",
        ALARM_TYPE_IO: "IO告警",
        ALARM_TYPE_PROCESS_NOT_FOUND: "进程不存在",
    }
    return mapping.get(alarm_type, f"未知告警({alarm_type})")


def read_alarm_from_redis(ip: str, service_name: str) -> Dict[str, Any]:
    """
    从 Redis 读取告警阈值。

    Key: alarm:{ip}:{service_name}
    Value: {"service_name":"my-service","cpu":"80","memory":"512","io":"100"}

    Args:
        ip:           本机 IP
        service_name: 服务名称

    Returns:
        阈值字典 {cpu, memory, io, service_name} 或空字典
    """
    key = f"alarm:{ip}:{service_name}"
    raw_data = None
    try:
        r = get_redis()
        raw_data = r.get(key)
        if raw_data is None:
            logger.info("[ALARM-REDIS] key=%s 不存在", key)
            return {}
        logger.info("[ALARM-REDIS] key=%s raw=%s type=%s", key, raw_data, type(raw_data).__name__)
        result = json.loads(raw_data)
        # 兼容双重 JSON 编码: raw 是 "{\"cpu\":\"80\",...}" → json.loads 返回字符串 → 再解一层
        if isinstance(result, str):
            logger.warning("[ALARM-REDIS] 检测到双重JSON编码，正在二次解析: key=%s", key)
            result = json.loads(result)
        if not isinstance(result, dict):
            logger.warning("[ALARM-REDIS] json.loads 返回了 %s 而非 dict, key=%s, value=%s",
                           type(result).__name__, key, result)
            return {}
        return result
    except json.JSONDecodeError as e:
        logger.warning("[ALARM-REDIS] JSON 反序列化失败: key=%s value=%s error=%s", key, raw_data, e)
        return {}
    except Exception as e:
        logger.warning("[ALARM] 从 Redis 读取告警阈值失败: %s -> %s", key, e)
        return {}


def read_version(service_dir: str) -> str:
    """读取服务目录下的 version 文件"""
    version_file = os.path.join(service_dir, "version")
    if not os.path.isfile(version_file):
        return ""
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def read_pid(runtime_dir: str) -> Optional[int]:
    """读取 runtime 目录下的 pid 文件，支持多行，返回第一个有效 PID"""
    pid_file = os.path.join(runtime_dir, "pid")
    if not os.path.isfile(pid_file):
        return None
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                return int(line)
            except ValueError:
                continue
        return None
    except Exception:
        return None


def discover_running_services() -> List[Dict]:
    """
    从 Redis 扫描所有 alarm:{ip}:* 的 key, 以此为驱动发现运行中的服务。

    Returns:
        [
            {
                "service_name": "my-service",
                "pid": 12345,
                "runtime_dir": "/data/download/my-service/app/1.0.0/runtime",
                "cpu_threshold": 90,
                "memory_threshold_mb": 1024.0,
                "io_threshold": 500,
            }
        ]
    """
    if not _DOWNLOAD_BASE or not os.path.isdir(_DOWNLOAD_BASE):
        logger.warning("[ALARM] _DOWNLOAD_BASE 不存在或不是目录: %s", _DOWNLOAD_BASE)
        return []

    result: List[Dict] = []
    local_ip = get_local_ip()
    logger.info("[ALARM-SCAN] 本机IP=%s, 开始扫描 Redis alarm:%s:*", local_ip, local_ip)

    # ---- 第1步：从 Redis 扫描所有 alarm:{ip}:* 的 key ----
    try:
        r = get_redis()
        alarm_keys = list(r.scan_iter(match=f"alarm:{local_ip}:*", count=100))
        logger.info("[ALARM-SCAN] Redis SCAN 完成, 找到 %d 个 key: %s", len(alarm_keys), alarm_keys)
    except Exception as e:
        logger.warning("[ALARM] Redis scan 失败: %s", e)
        return []

    if not alarm_keys:
        logger.warning("[ALARM] Redis 中无 alarm:%s:* 规则，告警检测跳过", local_ip)
        return []

    # ---- 第2步：逐个 key 提取 service_name, 匹配本地服务 ----
    logger.info("[ALARM-SCAN] 开始逐个匹配服务, _DOWNLOAD_BASE=%s", _DOWNLOAD_BASE)
    for key in alarm_keys:
        key_str = key if isinstance(key, str) else key.decode("utf-8")
        # 从 alarm:192.168.0.4:RuoYi-springboot2 提取 service_name
        prefix = f"alarm:{local_ip}:"
        if not key_str.startswith(prefix):
            logger.warning("[ALARM-SCAN] key 前缀不匹配，跳过: %s (期望前缀: %s)", key_str, prefix)
            continue
        service_name = key_str[len(prefix):]
        logger.info("[ALARM-SCAN] 匹配到 service_name=%s", service_name)

        # 检查服务目录
        service_dir = os.path.join(_DOWNLOAD_BASE, service_name)
        if not os.path.isdir(service_dir):
            logger.warning("[ALARM-SCAN] 服务目录不存在: %s, 跳过", service_dir)
            continue

        # 必须有 version 文件
        version = read_version(service_dir)
        if not version:
            logger.warning("[ALARM] version 文件缺失，跳过: %s", service_name)
            continue

        # 从 Redis 读告警规则
        alarm_rules = read_alarm_from_redis(local_ip, service_name)
        cpu_threshold = alarm_rules.get("cpu")
        memory_threshold = alarm_rules.get("memory")
        io_threshold = alarm_rules.get("io")
        logger.info("[ALARM-SCAN] 服务=%s 阈值: cpu=%s mem=%s io=%s", service_name, cpu_threshold, memory_threshold, io_threshold)

        if cpu_threshold is None or memory_threshold is None or io_threshold is None:
            logger.warning("[ALARM] Redis 告警阈值不完整，跳过: %s (cpu=%s mem=%s io=%s)", service_name, cpu_threshold, memory_threshold, io_threshold)
            continue

        # 进入 {version}/runtime/
        runtime_dir = os.path.join(service_dir, version, "runtime")
        if not os.path.isdir(runtime_dir):
            logger.warning("[ALARM] runtime 目录不存在: %s, 跳过", runtime_dir)
            continue

        pid = read_pid(runtime_dir)
        if pid is None:
            logger.warning("[ALARM] PID 文件缺失，跳过: %s/%s", service_name, version)
            continue

        # 确保阈值为数值类型（Redis 返回的 JSON 值为字符串）
        try:
            cpu_threshold = float(cpu_threshold)
            memory_threshold = float(memory_threshold)
            io_threshold = float(io_threshold)
        except (TypeError, ValueError) as e:
            logger.warning("[ALARM] 告警阈值格式错误: %s -> %s", service_name, e)
            continue

        result.append({
            "service_name": service_name,
            "pid": pid,
            "runtime_dir": runtime_dir,
            "cpu_threshold": cpu_threshold,
            "memory_threshold_mb": memory_threshold,
            "io_threshold": io_threshold,
        })

    logger.info("[ALARM-SCAN] 扫描完成, 共匹配到 %d 个有效服务: %s",
                len(result), [s["service_name"] for s in result])
    return result


def _read_all_resources(runtime_dir: str) -> Optional[Dict[str, float]]:
    """
    读取 runtime/resources.txt，汇总该服务所有进程（主进程+子进程）的最新资源数据。

    resources.txt 格式为数组，每个元素结构:
    {
        "service_name": "...",
        "pid": "20488",
        "process_info": {
            "pid": "20488",
            "name": "PING.EXE",
            "cpu": "0.0",
            "mem": "1.4",
            "io": "0.0"
        },
        "timestamp": 1782203893902
    }

    返回: {"cpu": 累加值, "mem": 累加值, "io": 累加值} 或 None
    """
    resources_file = os.path.join(runtime_dir, "resources.txt")
    if not os.path.isfile(resources_file):
        return None
    try:
        with open(resources_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("[ALARM] 读取 resources.txt 失败: %s -> %s", resources_file, e)
        return None

    if not isinstance(data, list) or len(data) == 0:
        return None

    # 按 PID 分组，每个 PID 取最新一条（timestamp 最大的）
    latest_by_pid: Dict[int, Dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        item_pid = item.get("pid")
        try:
            item_pid = int(item_pid)
        except (TypeError, ValueError):
            continue
        ts = item.get("timestamp", 0)
        if item_pid not in latest_by_pid or ts > latest_by_pid[item_pid].get("timestamp", 0):
            latest_by_pid[item_pid] = item

    if not latest_by_pid:
        return None

    # 累加所有进程（主进程 + 子进程）的资源
    total_cpu = 0.0
    total_mem = 0.0
    total_io = 0.0

    for item in latest_by_pid.values():
        proc_info = item.get("process_info", item)
        try:
            total_cpu += float(proc_info.get("cpu", 0))
        except (TypeError, ValueError):
            pass
        try:
            total_mem += float(proc_info.get("mem", 0))
        except (TypeError, ValueError):
            pass
        try:
            total_io += float(proc_info.get("io", 0))
        except (TypeError, ValueError):
            pass

    return {"cpu": total_cpu, "mem": total_mem, "io": total_io}


def check_and_report_alarms() -> List[Dict]:
    """
    遍历 download 目录下的所有服务，读取 runtime/resources.txt 获取该服务所有进程
    （主进程+子进程）的最新 CPU / 内存 / IO 数据，累加后与 Redis 阈值对比，
    超出则上报告警。

    resources.txt 由 process_info.py 的 sync_redis_to_resources() 写入。

    Returns:
        [{type, pid, value, service_name}, ...]
    """
    services = discover_running_services()

    if not services:
        logger.info("[ALARM-CHECK] 没有需要检测的服务（Redis 中无规则或无匹配本地服务）")
        return []

    logger.info("[ALARM-CHECK] 共发现 %d 个服务需要检测告警", len(services))
    reported: List[Dict] = []

    for svc in services:
        service_name = svc["service_name"]
        pid = svc["pid"]
        runtime_dir = svc["runtime_dir"]
        cpu_threshold = svc["cpu_threshold"]
        memory_threshold_mb = svc["memory_threshold_mb"]
        io_threshold = svc["io_threshold"]

        # ---- 从 resources.txt 读取所有进程资源数据（主进程+子进程累加） ----
        resources = _read_all_resources(runtime_dir)

        if resources is None:
            # resources.txt 不存在或无数据
            logger.warning("[ALARM-CHECK] 服务=%s PID=%s, resources.txt 无数据, 尝试上报进程异常", service_name, pid)
            if can_report_alarm(pid, ALARM_TYPE_PROCESS_NOT_FOUND):
                report_alarm(
                    alarm_type=ALARM_TYPE_PROCESS_NOT_FOUND,
                    service_name=service_name,
                    alarm_name=f"{service_name} 进程异常",
                    content=f"服务 {service_name}(PID:{pid}) 进程可能未启动，resources.txt 无数据",
                )
                reported.append({
                    "type": "process_not_found",
                    "pid": pid,
                    "value": 0,
                    "service_name": service_name,
                })
            else:
                logger.info("[ALARM-CHECK] 服务=%s 进程异常告警命中冷却，跳过", service_name)
            continue

        cpu_val = resources["cpu"]
        mem_val = resources["mem"]
        io_val = resources["io"]
        logger.info("[ALARM-CHECK] 服务=%s PID=%s 实际值: cpu=%.1f mem=%.1f io=%.1f | 阈值: cpu=%.1f mem=%.1f io=%.1f",
                    service_name, pid, cpu_val, mem_val, io_val,
                    cpu_threshold, memory_threshold_mb, io_threshold)

        # ---- CPU 告警 ----
        if cpu_threshold >= 0 and cpu_val > cpu_threshold:
            logger.info("[ALARM-CHECK] 服务=%s CPU告警触发: %.1f > %.1f", service_name, cpu_val, cpu_threshold)
            if can_report_alarm(pid, ALARM_TYPE_CPU):
                report_alarm(
                    alarm_type=ALARM_TYPE_CPU,
                    service_name=service_name,
                    alarm_name=f"{service_name} CPU告警",
                    content=f"服务 {service_name}(PID:{pid}) CPU 使用 {cpu_val}%，超过阈值 {cpu_threshold}%",
                )
                reported.append({
                    "type": "cpu",
                    "pid": pid,
                    "value": cpu_val,
                    "threshold": cpu_threshold,
                    "service_name": service_name,
                })
            else:
                logger.info("[ALARM-CHECK] 服务=%s CPU告警命中冷却，跳过", service_name)

        # ---- 内存告警 ----
        if memory_threshold_mb >= 0 and mem_val > memory_threshold_mb:
            logger.info("[ALARM-CHECK] 服务=%s 内存告警触发: %.1f > %.1f", service_name, mem_val, memory_threshold_mb)
            if can_report_alarm(pid, ALARM_TYPE_MEMORY):
                report_alarm(
                    alarm_type=ALARM_TYPE_MEMORY,
                    service_name=service_name,
                    alarm_name=f"{service_name} 内存告警",
                    content=f"服务 {service_name}(PID:{pid}) 内存使用 {mem_val}MB，超过阈值 {memory_threshold_mb}MB",
                )
                reported.append({
                    "type": "memory",
                    "pid": pid,
                    "value": mem_val,
                    "threshold": memory_threshold_mb,
                    "service_name": service_name,
                })
            else:
                logger.info("[ALARM-CHECK] 服务=%s 内存告警命中冷却，跳过", service_name)

        # ---- IO 告警 ----
        if io_threshold >= 0 and io_val > io_threshold:
            logger.info("[ALARM-CHECK] 服务=%s IO告警触发: %.1f > %.1f", service_name, io_val, io_threshold)
            if can_report_alarm(pid, ALARM_TYPE_IO):
                report_alarm(
                    alarm_type=ALARM_TYPE_IO,
                    service_name=service_name,
                    alarm_name=f"{service_name} IO告警",
                    content=f"服务 {service_name}(PID:{pid}) IO 使用 {io_val}MB，超过阈值 {io_threshold}MB",
                )
                reported.append({
                    "type": "io",
                    "pid": pid,
                    "value": io_val,
                    "threshold": io_threshold,
                    "service_name": service_name,
                })
            else:
                logger.info("[ALARM-CHECK] 服务=%s IO告警命中冷却，跳过", service_name)

    logger.info("[ALARM-CHECK] 本轮检测完成，实际触发上报 %d 条告警", len(reported))
    return reported


if __name__ == "__main__":
    # import sys
    # import os
    # sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    # from utils.logger import setup_logger
    # setup_logger()
    #
    # print("=" * 60)
    # print("  手动运行告警检测")
    # print("=" * 60)
    result = check_and_report_alarms()
    # if result:
    #     print(f"\n触发 {len(result)} 条告警:")
    #     for r in result:
    #         print(f"  type={r['type']}, pid={r['pid']}, value={r['value']}, service={r['service_name']}")
    # else:
    #     print("\n无告警触发")
    # print("\n完成")



