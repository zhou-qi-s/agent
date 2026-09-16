"""
显控进程资源监控模块

定时遍历 download/displayConsole/{服务名}/{版本}/runtime/pid 读取显控台 PID，
查询进程资源使用情况（CPU/内存/IO），
将采集数据写入 download/xkt/resources/{进程名}。

PID 唯一落点：{版本}/runtime/pid（由 core/xkt/process_check.py 巡检维护）。
资源采集逻辑参考 core/alarm/alarm.py 中的 check_process_resource。
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil
import yaml

from utils.config_loader import load_config


# =============================================================================
# 基线缓存（用于计算 CPU / IO 增量）
# =============================================================================

_cpu_baseline: Dict[int, Dict[str, float]] = {}
_io_baseline: Dict[int, Dict[str, float]] = {}


# =============================================================================
# 工具函数
# =============================================================================

def _get_download_path() -> str:
    """获取 download 目录的绝对路径"""
    cfg = load_config()
    download = cfg.get("server", {}).get("download", "download")
    if not os.path.isabs(download):
        download = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            download
        )
    return download


def _read_xkt_pids(display_console_dir: str) -> Dict[str, List[int]]:
    """
    读取显控台各服务的 PID。

    目录结构:
        download/displayConsole/
            {服务名}/
                version                    ← 当前版本号
                {版本}/
                    runtime/
                        config.yaml        ← name 字段 = 进程名（用于聚合命名）
                        pid                ← 文件内容 = PID（一行一个）

    进程名取 runtime/config.yaml 的 name 字段（取第一个），
    取不到时退化为服务目录名。

    参数:
        display_console_dir: download/displayConsole 目录路径

    返回:
        {进程名: [PID列表]}
    """
    result: Dict[str, List[int]] = {}

    if not os.path.isdir(display_console_dir):
        return result

    for service_name in os.listdir(display_console_dir):
        service_dir = os.path.join(display_console_dir, service_name)
        if not os.path.isdir(service_dir):
            continue

        # ── 读取 version 文件 ──
        version_file = os.path.join(service_dir, "version")
        if not os.path.isfile(version_file):
            continue
        try:
            with open(version_file, "r", encoding="utf-8") as vf:
                version = vf.read().strip()
        except Exception as e:
            logging.warning("[xkt资源监控] 读取 version 失败: %s -> %s", version_file, e)
            continue
        if not version:
            continue

        version_dir = os.path.join(service_dir, version)

        # ── 读取 runtime/pid ──
        pid_file = os.path.join(version_dir, "runtime", "pid")
        if not os.path.isfile(pid_file):
            continue
        try:
            with open(pid_file, "r", encoding="utf-8") as f:
                pid_lines = f.read().strip().splitlines()
        except Exception as e:
            logging.warning("[xkt资源监控] 读取 PID 文件失败: %s -> %s", pid_file, e)
            continue

        pids = [int(line.strip()) for line in pid_lines if line.strip().isdigit()]
        if not pids:
            continue

        result[_read_process_name(version_dir, service_name)] = pids

    return result


def _read_process_name(version_dir: str, fallback: str) -> str:
    """
    从 {version_dir}/runtime/config.yaml 读取进程名（name 字段，取第一个）。

    参数:
        version_dir: 版本目录
        fallback:    读不到时的兜底名称（通常为服务目录名）

    返回:
        进程名
    """
    config_path = os.path.join(version_dir, "runtime", "config.yaml")
    if not os.path.isfile(config_path):
        return fallback
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        names = config.get("name", [])
        if isinstance(names, str):
            names = [names]
        if names:
            return str(names[0])
    except Exception as e:
        logging.warning("[xkt资源监控] 读取 config.yaml 失败: %s -> %s", config_path, e)
    return fallback


# =============================================================================
# 进程资源查询（模仿 core/alarm/alarm.py 的 check_process_resource）
# =============================================================================

def check_process_resource(pid: int) -> Optional[Dict[str, Any]]:
    """
    查询单个进程的 CPU / 内存 / IO 使用情况。

    CPU 采集逻辑：
    - 首次采集固定返回 0（需要基线差值计算）
    - 后续采集基于 cpu_times 差值 / 时间间隔计算平均使用率

    IO 采集逻辑：
    - 基于 io_counters 差值计算间隔内的 IO 增量

    参数:
        pid: 进程 ID

    返回:
        {
            "pid": 12345,
            "name": "java",
            "cpu": 12.5,       # 百分比
            "memory": 512.0,   # MB (RSS 物理内存)
            "io": 0.5,         # MB (间隔增量)
            "timestamp": 1234567890,
        }
        进程不存在返回 None
    """
    try:
        process = psutil.Process(pid)
        current_time = time.time()

        # ---- CPU ----
        cpu_times = process.cpu_times()
        cpu_percent = 0.0

        if pid in _cpu_baseline:
            baseline = _cpu_baseline[pid]
            delta_user = cpu_times.user - baseline["user"]
            delta_system = cpu_times.system - baseline["system"]
            delta_time = current_time - baseline["timestamp"]
            if delta_time > 0:
                cpu_percent = ((delta_user + delta_system) / delta_time) * 100.0

        _cpu_baseline[pid] = {
            "user": cpu_times.user,
            "system": cpu_times.system,
            "timestamp": current_time,
        }

        # ---- 内存（RSS 物理内存） ----
        memory_mb = process.memory_info().rss / 1024 / 1024

        # ---- IO 统计（间隔增量） ----
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
            "timestamp": int(current_time),
        }

    except psutil.NoSuchProcess:
        _cpu_baseline.pop(pid, None)
        _io_baseline.pop(pid, None)
        return None

    except Exception as e:
        logging.warning("[xkt资源监控] 查询进程资源失败 pid=%d: %s", pid, e)
        return None


# =============================================================================
# 主采集逻辑
# =============================================================================

def _collect_one_pid(
        pid: int, process_name: str
) -> Optional[Tuple[str, int, Optional[Dict[str, Any]]]]:
    """采集单个 PID 的资源信息（供线程池并行调用）"""
    resource = check_process_resource(pid)
    return (process_name, pid, resource)


def collect_xkt_resources(max_workers: int = 8) -> Dict[str, Any]:
    """
    读取 displayConsole 各服务的 runtime/pid → 多线程并行查询资源 → 写入 resources 目录。

    流程:
        1. 遍历 download/displayConsole/{服务名}/{版本}/runtime/pid
        2. 使用线程池并行采集每个 PID 的 CPU/内存/IO
        3. 按进程名聚合结果，写入 download/xkt/resources/{进程名}

    参数:
        max_workers: 线程池最大线程数，默认 8

    返回:
        {
            "success": True,
            "collected": 3,
            "failed": 0,
            "details": [...]
        }
    """
    download_path = _get_download_path()
    display_console_dir = os.path.join(download_path, "displayConsole")
    resources_dir = os.path.join(download_path, "xkt", "resources")

    try:
        Path(resources_dir).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.error("[xkt资源监控] 创建 resources 目录失败: %s", e)
        return {"success": False, "collected": 0, "failed": 0, "details": [], "error": str(e)}

    pid_map = _read_xkt_pids(display_console_dir)
    if not pid_map:
        logging.debug("[xkt资源监控] displayConsole 下没有可采集的 PID")
        return {"success": True, "collected": 0, "failed": 0, "details": []}

    # 收集所有 (pid, process_name) 任务
    pid_tasks: List[Tuple[int, str]] = []
    for process_name, pids in pid_map.items():
        for pid in pids:
            pid_tasks.append((pid, process_name))

    if not pid_tasks:
        return {"success": True, "collected": 0, "failed": 0, "details": []}

    actual_workers = min(max_workers, len(pid_tasks))
    logging.info(
        "[xkt资源监控] 并行采集 %d 个 PID, 线程=%d",
        len(pid_tasks), actual_workers
    )

    # 线程池并行采集
    collected = 0
    failed = 0
    process_resources: Dict[str, List[Dict[str, Any]]] = {}

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {
            executor.submit(_collect_one_pid, pid, pname): (pid, pname)
            for pid, pname in pid_tasks
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result is None:
                    continue
                pname, pid, resource = result
                if resource:
                    if pname not in process_resources:
                        process_resources[pname] = []
                    process_resources[pname].append(resource)
                    collected += 1
                else:
                    failed += 1
                    logging.warning("[xkt资源监控] 进程不存在, PID=%d", pid)
            except Exception as e:
                failed += 1
                logging.error("[xkt资源监控] 采集任务异常: %s", e)

    elapsed = time.time() - t_start
    logging.info(
        "[xkt资源监控] 采集耗时 %.1fs (PID数=%d, 线程=%d, 成功=%d, 失败=%d)",
        elapsed, len(pid_tasks), actual_workers, collected, failed
    )

    # 写入文件
    details: List[Dict[str, Any]] = []
    for process_name, resources in process_resources.items():
        output_data = {
            "process_name": process_name,
            "timestamp": int(time.time()),
            "resources": resources,
        }
        output_file = os.path.join(resources_dir, process_name)
        try:
            temp_file = output_file + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(output_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_file, output_file)
            logging.info("[xkt资源监控] 已写入资源文件: %s (%d 条)", output_file, len(resources))
        except Exception as e:
            logging.error("[xkt资源监控] 写入资源文件失败: %s -> %s", output_file, e)

        details.append({
            "process_name": process_name,
            "pids": pid_map.get(process_name, []),
            "resource_count": len(resources),
        })

    return {
        "success": True,
        "collected": collected,
        "failed": failed,
        "details": details,
    }


# =============================================================================
# 定时任务循环
# =============================================================================

def xkt_resource_monitor_loop(interval: int = 30):
    """
    显控进程资源监控循环（用于后台线程）。

    参数:
        interval: 采集间隔（秒），默认 30 秒
    """
    logging.info("[xkt资源监控] 资源监控线程启动, 间隔=%ds", interval)
    while True:
        t0 = time.time()
        try:
            result = collect_xkt_resources()
            logging.info(
                "[xkt资源监控] 本轮采集完成: 成功 %d, 失败 %d",
                result["collected"], result["failed"]
            )
        except Exception as e:
            logging.error("[xkt资源监控] 采集异常: %s", e)
        elapsed = time.time() - t0
        if elapsed > interval:
            logging.warning(
                "[xkt资源监控] ⚠ 单轮耗时 %.1fs 超过间隔 %ds",
                elapsed, interval
            )
        else:
            time.sleep(max(1, interval - elapsed))


# =============================================================================
# 本地测试入口
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 60)
    print("  显控进程资源监控（单次执行）")
    print("=" * 60)
    result = collect_xkt_resources()
    print(f"\n  采集结果: 成功 {result['collected']} / 失败 {result['failed']}")
    for d in result.get("details", []):
        print(f"    - {d['process_name']}: PID={d['pids']}, 资源条目={d['resource_count']}")
    print("=" * 60)

    # 详细输出
    print(json.dumps(result, indent=2, ensure_ascii=False))
