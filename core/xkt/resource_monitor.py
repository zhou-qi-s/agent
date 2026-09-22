"""
显控进程资源监控模块

【缓存区/运行区分离后的采集规则】

定时扫描「运行区」显控台/插件服务，读取运行状态中的 PID，
查询进程资源使用情况（CPU/内存/IO），将采集数据写入
{server.apps}/{服务}/runtime/resources.txt。

    1. 遍历 {apps}/displayConsole/ 与 {apps}/plugin/ 下各服务
    2. 读 state/config.yaml 的 pids（不读已废弃的 {版本}/runtime/pid）
    3. 按进程名聚合（processes 字段，取第一个；缺省用服务名）

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

from utils.app_path import (
    SUB_DIR_PLUGIN,
    SUB_DIR_XKT,
    read_state,
    read_state_pids,
)
from utils.config_loader import get_apps_dir, load_config


# =============================================================================
# 基线缓存（用于计算 CPU / IO 增量）
# =============================================================================

_cpu_baseline: Dict[int, Dict[str, float]] = {}
_io_baseline: Dict[int, Dict[str, float]] = {}


# =============================================================================
# 工具函数
# =============================================================================

def _get_apps_path() -> str:
    """获取运行区（server.apps）目录的绝对路径"""
    apps = get_apps_dir()
    if apps:
        return apps
    cfg = load_config()
    fallback = cfg.get("server", {}).get("apps", "apps")
    if not os.path.isabs(fallback):
        fallback = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            fallback
        )
    return fallback


def _read_xkt_pids(services_root: str, sub_dir: str = "") -> Dict[str, List[int]]:
    """
    读取指定类别下各服务的 PID（**扫运行区**）。

    目录结构:
        {apps}/[{sub_dir}/]
            {服务名}/
                state/config.yaml      ← 运行状态（pids / processes）

    进程名取 state 的 processes 字段（取第一个），取不到时退化为服务目录名。

    参数:
        services_root: 类别根目录，如 {apps}/displayConsole 或 {apps}/plugin
        sub_dir:       类别子目录（用于读状态；留空则不区分）

    返回:
        {进程名: [PID列表]}
    """
    result: Dict[str, List[int]] = {}

    if not os.path.isdir(services_root):
        return result

    for service_name in os.listdir(services_root):
        service_dir = os.path.join(services_root, service_name)
        # 跳过软链接（current/app/bin/config）与普通文件
        if os.path.islink(service_dir) or not os.path.isdir(service_dir):
            continue

        state = read_state(service_name, sub_dir)
        if not state or not state.get("runtime"):
            continue

        pids = read_state_pids(service_name, sub_dir)
        if not pids:
            continue

        names = state.get("processes") or []
        if isinstance(names, str):
            names = [names]
        pname = names[0] if names else service_name

        result[pname] = pids

    return result


# 注：原 _read_process_name() 已删除。
#     进程名现直接从运行状态 state/config.yaml 的 processes 字段读取
#     （见 _read_xkt_pids），不再读 {版本}/runtime/config.yaml。


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
    读取运行区显控台/插件服务的 state pids → 多线程并行查询资源 → 写入各服务 runtime/。

    流程:
        1. 遍历 {apps}/displayConsole/ 与 {apps}/plugin/ 下各服务，读 state/config.yaml 的 pids
        2. 使用线程池并行采集每个 PID 的 CPU/内存/IO
        3. 按进程名聚合结果，写入 {apps}/[{sub_dir}/]{服务}/runtime/resources.txt

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
    apps_path = _get_apps_path()

    # 收集两类服务：{进程名: (pids, 服务目录, 子类别)}
    pid_map: Dict[str, List[int]] = {}
    service_roots: List[Tuple[str, str]] = []   # (服务目录, sub_dir)
    for sub_dir in (SUB_DIR_XKT, SUB_DIR_PLUGIN):
        root = os.path.join(apps_path, sub_dir)
        if not os.path.isdir(root):
            continue
        for service_name in os.listdir(root):
            service_dir = os.path.join(root, service_name)
            if os.path.islink(service_dir) or not os.path.isdir(service_dir):
                continue
            service_roots.append((service_dir, sub_dir))

    for service_dir, sub_dir in service_roots:
        service_name = os.path.basename(os.path.normpath(service_dir))
        state = read_state(service_name, sub_dir)
        if not state or not state.get("runtime"):
            continue
        pids = read_state_pids(service_name, sub_dir)
        if not pids:
            continue
        names = state.get("processes") or []
        if isinstance(names, str):
            names = [names]
        pname = names[0] if names else service_name
        pid_map[pname] = pids

    if not pid_map:
        logging.debug("[xkt资源监控] 运行区下没有可采集的 PID")
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

    # 写入各服务的 runtime/resources.txt（落在运行区）
    details: List[Dict[str, Any]] = []
    for process_name, resources in process_resources.items():
        output_data = {
            "process_name": process_name,
            "timestamp": int(time.time()),
            "resources": resources,
        }

        # 找到该进程名对应的服务目录，写入其 runtime/
        target_dir = None
        for service_dir, sub_dir in service_roots:
            sname = os.path.basename(os.path.normpath(service_dir))
            st = read_state(sname, sub_dir)
            names = st.get("processes") or []
            if isinstance(names, str):
                names = [names]
            if (names[0] if names else sname) == process_name:
                target_dir = service_dir
                break

        if not target_dir:
            logging.warning("[xkt资源监控] 未找到进程名 %s 对应的服务，跳过写文件", process_name)
            continue

        runtime_dir = os.path.join(target_dir, "runtime")
        try:
            Path(runtime_dir).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.error("[xkt资源监控] 创建 runtime 目录失败: %s -> %s", runtime_dir, e)
            continue

        output_file = os.path.join(runtime_dir, "resources.txt")
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
