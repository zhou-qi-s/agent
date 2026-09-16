"""
进程信息采集模块

遍历 download 目录下所有组件，读取 version → {version}/runtime/pid，
根据 PID 查询进程详情并返回。
"""

import json
import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import psutil

from utils.config_loader import load_config
from utils.util import get_ip

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")

# 显控台服务的一级目录名：{download}/displayConsole/{服务名}/{版本}
_XKT_SERVICE_ROOT = "displayConsole"

# 插件应用的一级目录名：{download}/plugin/{服务名}/{版本}
_PLUGIN_SERVICE_ROOT = "plugin"


def _make_key(pid: int) -> str:
    """生成格式为 Process/IP/PID 的 key"""
    return f"Process/{get_ip()}/{pid}"


def _collect_ports(proc: psutil.Process) -> list:
    """收集进程的监听端口列表"""
    result = []
    try:
        for conn in proc.connections(kind="inet"):
            if getattr(conn, "status", "") == "LISTEN":
                result.append({
                    "ip": getattr(conn.laddr, "ip", "") or "",
                    "port": getattr(conn.laddr, "port", 0) or 0,
                    "type": "tcp" if getattr(conn, "type", 0) == socket.SOCK_STREAM else "udp",
                })
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        pass
    return result


def _get_process_info_linux(pid: int) -> Dict[str, str]:
    """通过 psutil（统一采集）：CPU 瞬时值、内存瞬时值、IO 瞬时速率"""
    info = {"pid": str(pid), "name": "", "user": "", "io_read_rate": "0", "io_write_rate": "0", "cpu": "0", "mem": "0", "cmd": "", "ports": ""}
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            info["name"] = proc.name() or ""
            info["user"] = proc.username() or ""
            info["cpu"] = f"{proc.cpu_percent(interval=0.1):.1f}"
            info["mem"] = f"{proc.memory_info().rss / (1024 * 1024):.1f}"
            info["cmd"] = " ".join(proc.cmdline() or [])
            ports = _collect_ports(proc)
            if ports:
                info["ports"] = json.dumps(ports)

        # IO 瞬时速率：两次采样差值 / 间隔（MB/s）
        io1 = proc.io_counters()
        time.sleep(0.1)
        io2 = proc.io_counters()
        info["io_read_rate"] = f"{(io2.read_bytes - io1.read_bytes) / (1024 * 1024) / 0.1:.1f}"
        info["io_write_rate"] = f"{(io2.write_bytes - io1.write_bytes) / (1024 * 1024) / 0.1:.1f}"
    except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
        pass
    return info


def _process_exists(pid: int) -> bool:
    """检查进程是否存在"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_process_info(pid: int) -> Optional[Dict[str, str]]:
    """根据 PID 查询进程信息"""
    return _get_process_info_linux(pid)


def _get_children_pids(pid: int) -> List[int]:
    """获取指定 PID 的所有子进程 PID 列表（递归）"""
    try:
        proc = psutil.Process(pid)
        return [child.pid for child in proc.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def _build_process_entry(pid: int, service_name: str, version: str,
                         timestamp: int) -> Dict[str, Any]:
    """构建单个进程的采集条目"""
    running = _process_exists(pid)
    proc_info = get_process_info(pid) if running else {}
    entry: Dict[str, Any] = {
        "key": _make_key(pid),
        "service_name": service_name,
        "version": version,
        "pid": str(pid),
        "running": running,
        "process_info": proc_info,
        "timestamp": timestamp,
    }
    # 补充 psutil 丰富信息
    if running:
        try:
            p = psutil.Process(pid)
            entry["uptime_seconds"] = int(time.time() - p.create_time())
            entry["cmdline"] = p.cmdline() or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return entry


def _collect_service_processes(
        service_dir: str, entry: str, timestamp: int
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    采集单个服务的进程信息（供线程池并行调用）。

    参数:
        service_dir: 服务目录路径
        entry:      服务名称（目录名）
        timestamp:  采集时间戳

    返回:
        (entry, [进程条目列表])
    """
    processes: List[Dict[str, Any]] = []

    version_file = os.path.join(service_dir, "version")
    if not os.path.isfile(version_file):
        return (entry, processes)

    try:
        with open(version_file, "r", encoding="utf-8") as vf:
            version = vf.read().strip()
    except Exception as e:
        logging.warning("[collect_service] 读取 version 失败: %s -> %s", version_file, e)
        return (entry, processes)

    if not version:
        return (entry, processes)

    pid_file = os.path.join(service_dir, version, "runtime", "pid")
    if not os.path.isfile(pid_file):
        return (entry, processes)

    try:
        with open(pid_file, "r", encoding="utf-8") as pf:
            pid_str = pf.read().strip()
    except Exception:
        return (entry, processes)

    if not pid_str:
        return (entry, processes)

    seen: set = set()
    for line in pid_str.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_int = int(line)
        except ValueError:
            continue
        if pid_int in seen:
            continue
        seen.add(pid_int)

        # 采集父进程
        processes.append(_build_process_entry(pid_int, entry, version, timestamp))

        # 采集所有子进程
        for child_pid in _get_children_pids(pid_int):
            if child_pid not in seen:
                seen.add(child_pid)
                processes.append(_build_process_entry(child_pid, entry, version, timestamp))

    return (entry, processes)


def _iter_service_dirs() -> List[Tuple[str, str]]:
    """
    收集所有需要采集的服务目录。

    支持三种目录结构：
        {download}/{服务名}/version                    ← 虚拟机 / 通用服务
        {download}/displayConsole/{服务名}/version     ← 显控台服务
        {download}/plugin/{服务名}/version             ← 插件服务

    返回:
        [(service_dir, service_name), ...]
    """
    tasks: List[Tuple[str, str]] = []

    if not _DOWNLOAD_BASE or not os.path.isdir(_DOWNLOAD_BASE):
        return tasks

    scan_roots: List[str] = [_DOWNLOAD_BASE]
    for sub_root in (_XKT_SERVICE_ROOT, _PLUGIN_SERVICE_ROOT):
        root = os.path.join(_DOWNLOAD_BASE, sub_root)
        if os.path.isdir(root):
            scan_roots.append(root)

    for root in scan_roots:
        try:
            entries = os.listdir(root)
        except Exception as e:
            logging.warning("[服务扫描] 读取目录失败: %s -> %s", root, e)
            continue

        for entry in entries:
            service_dir = os.path.join(root, entry)
            if not os.path.isdir(service_dir):
                continue
            # 只有带 version 文件的目录才算有效服务
            if not os.path.isfile(os.path.join(service_dir, "version")):
                continue
            tasks.append((service_dir, entry))

    return tasks


def collect_all_processes(max_workers: int = 8) -> List[Dict[str, Any]]:
    """
    遍历 download 目录，多线程并行采集所有组件的进程信息（含子进程）。

    流程:
        1. 遍历 {download}/ 下所有子目录，过滤出有效服务
        2. 使用线程池并行采集每个服务的进程详情
        3. 所有服务进程独立一条记录，父/子进程同级返回

    参数:
        max_workers: 线程池最大线程数，默认 8（可根据服务数量调整）

    返回:
        [
            {
                "key": "Process/192.168.1.5/12345",
                "service_name": "xxx",
                "version": "1.0.0",
                "pid": "12345",
                "running": true,
                "process_info": {"name": "xxx.exe", "memory": "50,000 K", ...},
                "timestamp": "2026-06-23T10:59:00",
            },
            ...
        ]
    """
    result: List[Dict[str, Any]] = []
    timestamp = int(datetime.now().timestamp() * 1000)

    if not _DOWNLOAD_BASE or not os.path.isdir(_DOWNLOAD_BASE):
        logging.warning("[collect_all_processes] download 目录不存在: %s", _DOWNLOAD_BASE)
        return result

    # Step 1: 收集所有待采集的服务目录（含显控台 displayConsole 下的服务）
    service_tasks: List[Tuple[str, str]] = _iter_service_dirs()  # [(service_dir, entry), ...]

    if not service_tasks:
        return result

    # Step 2: 线程池并行采集
    service_count = len(service_tasks)
    actual_workers = min(max_workers, service_count)
    logging.info(
        "[collect_all_processes] 并行采集 %d 个服务, 线程数=%d",
        service_count, actual_workers
    )

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {
            executor.submit(_collect_service_processes, sd, en, timestamp): en
            for sd, en in service_tasks
        }
        for future in as_completed(futures):
            service_name = futures[future]
            try:
                svc_name, svc_processes = future.result()
                result.extend(svc_processes)
                logging.debug(
                    "[collect_all_processes] 服务 %s 采集完成, %d 个进程",
                    svc_name, len(svc_processes)
                )
            except Exception as e:
                logging.error(
                    "[collect_all_processes] 服务 %s 采集异常: %s",
                    service_name, e
                )

    elapsed = time.time() - t_start
    logging.info(
        "[collect_all_processes] 采集完成: %d 进程, 耗时 %.1fs (服务数=%d, 线程=%d)",
        len(result), elapsed, service_count, actual_workers
    )
    if elapsed > 8:
        logging.warning(
            "[collect_all_processes] ⚠ 采集耗时 %.1fs 接近间隔上限, 建议减少服务或增加线程",
            elapsed
        )

    return result


def upload_to_redis(processes: List[Dict[str, Any]], expire: int = 600) -> Dict[str, Any]:
    """
    将进程信息上传到 Redis。

    参数:
        processes: collect_all_processes() 返回的进程列表
        expire:    Redis key 过期时间（秒），默认 600 秒（10 分钟）

    返回:
        {"success": true, "uploaded": 2, "failed": 0, "errors": []}
    """
    from utils.redis_client import get_redis

    uploaded, failed = 0, 0
    errors: List[str] = []

    try:
        r = get_redis()
    except Exception as e:
        logging.error("[upload_to_redis] Redis 连接失败: %s", e)
        return {"success": False, "uploaded": 0, "failed": len(processes), "error": str(e)}

    for p in processes:
        key = p.get("key", "")
        if not key:
            failed += 1
            errors.append(f"缺少 key: {p.get('service_name', 'unknown')}")
            continue

        try:
            value = json.dumps({
                "service_name": p.get("service_name", ""),
                "version": p.get("version", ""),
                "pid": p.get("pid", ""),
                "running": p.get("running", False),
                "process_info": p.get("process_info", {}),
                "timestamp": p.get("timestamp", ""),
            }, ensure_ascii=False)
            # 使用 List 存储，LPUSH 插入头部，LTRIM 保留最近 15 条，设置 10 分钟过期
            pipe = r.pipeline()
            pipe.lpush(key, value)
            pipe.ltrim(key, 0, 14)
            pipe.expire(key, expire)
            pipe.execute()
            uploaded += 1
            logging.info("[upload_to_redis] 已上传: %s", key)
        except Exception as e:
            failed += 1
            errors.append(f"{key}: {e}")
            logging.warning("[upload_to_redis] 上传失败: %s -> %s", key, e)

    return {
        "success": failed == 0,
        "uploaded": uploaded,
        "failed": failed,
        "errors": errors,
    }


def collect_and_upload(expire: int = 600) -> Dict[str, Any]:
    """
    采集所有组件进程信息并上传到 Redis（一步完成）。

    参数:
        expire: Redis key 过期时间（秒），默认 30 秒

    返回:
        {"success": true, "processes": [...], "upload": {...}}
    """
    processes = collect_all_processes()
    upload_result = upload_to_redis(processes, expire=expire)
    # 上传后立即同步到 resources.txt
    sync_result = sync_redis_to_resources(expire=expire)
    return {
        "success": upload_result["success"] and sync_result["success"],
        "process_count": len(processes),
        "processes": processes,
        "upload": upload_result,
        "sync": sync_result,
    }


def sync_redis_to_resources(expire: int = 600) -> Dict[str, Any]:
    """
    从 Redis 读取进程数据，按服务分组写入各服务的 resources.txt 文件。

    流程:
        1. 遍历 download 目录，收集所有服务名、版本号、PID 信息
        2. 根据 PID 拼接 Redis key，从 Redis 读取 List 数据
        3. 按服务目录分组，写入对应的 {服务目录}/{version}/runtime/resources.txt
           通用/插件: {download}/{服务名}/...
           显控台:     {download}/displayConsole/{服务名}/...

    参数:
        expire: 读取后重新设置 key 的过期时间（秒），默认 600 秒

    返回:
        {"success": true, "synced": 3, "failed": 0, "errors": []}
    """
    from utils.redis_client import get_redis

    synced, failed = 0, 0
    errors: List[str] = []

    try:
        r = get_redis()
    except Exception as e:
        logging.error("[sync_redis_to_resources] Redis 连接失败: %s", e)
        return {"success": False, "synced": 0, "failed": 0, "error": str(e)}

    if not _DOWNLOAD_BASE or not os.path.isdir(_DOWNLOAD_BASE):
        logging.warning("[sync_redis_to_resources] download 目录不存在: %s", _DOWNLOAD_BASE)
        return {"success": False, "synced": 0, "failed": 0, "error": "download 目录不存在"}

    # 收集所有服务 → 版本 → PID 的映射（含显控台 displayConsole 下的服务）
    # service_map: {service_dir: {"service_name": "xxx", "version": "1.0.0", "pids": [123, 456]}}
    service_map: Dict[str, Dict[str, Any]] = {}

    for service_dir, entry in _iter_service_dirs():
        version_file = os.path.join(service_dir, "version")

        try:
            with open(version_file, "r", encoding="utf-8") as vf:
                version = vf.read().strip()
        except Exception:
            continue

        if not version:
            continue

        pid_file = os.path.join(service_dir, version, "runtime", "pid")
        if not os.path.isfile(pid_file):
            continue

        try:
            with open(pid_file, "r", encoding="utf-8") as pf:
                pid_str = pf.read().strip()
        except Exception:
            continue

        if not pid_str:
            continue

        # 支持多行 PID
        pids = []
        seen = set()
        for line in pid_str.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid_int = int(line)
            except ValueError:
                continue
            if pid_int not in seen:
                seen.add(pid_int)
                pids.append(pid_int)
        # 同时收集子进程 PID
        for parent_pid in list(pids):
            for child_pid in _get_children_pids(parent_pid):
                if child_pid not in seen:
                    seen.add(child_pid)
                    pids.append(child_pid)

        service_map[service_dir] = {
            "service_name": entry,
            "version": version,
            "pids": pids,
        }

    # 从 Redis 读取每个 PID 的数据，按服务目录分组
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for service_dir_key, info in service_map.items():
        pids = info["pids"]
        service_processes: List[Dict[str, Any]] = []

        for pid in pids:
            key = _make_key(pid)
            try:
                # 读取 List 中所有数据（最多 15 条）
                raw_list = r.lrange(key, 0, -1)
                for raw in raw_list:
                    try:
                        item = json.loads(raw)
                        service_processes.append(item)
                    except json.JSONDecodeError:
                        pass
                # 刷新 key 过期时间
                r.expire(key, expire)
            except Exception as e:
                errors.append(f"{key}: {e}")
                logging.warning("[sync_redis_to_resources] 读取 Redis 失败: %s -> %s", key, e)

        grouped[service_dir_key] = service_processes

    # 写入各服务的 resources.txt
    for service_dir_key, process_list in grouped.items():
        info = service_map.get(service_dir_key, {})
        version = info.get("version", "")
        service_name = info.get("service_name", service_dir_key)
        if not version:
            failed += 1
            errors.append(f"{service_name}: 缺少版本号")
            continue

        runtime_dir = os.path.join(service_dir_key, version, "runtime")
        os.makedirs(runtime_dir, exist_ok=True)
        resources_file = os.path.join(runtime_dir, "resources.txt")

        try:
            with open(resources_file, "w", encoding="utf-8") as f:
                json.dump(process_list, f, ensure_ascii=False, indent=2)
            synced += 1
            logging.info("[sync_redis_to_resources] 已写入: %s (%d 条)", resources_file, len(process_list))
        except Exception as e:
            failed += 1
            errors.append(f"{resources_file}: {e}")
            logging.warning("[sync_redis_to_resources] 写入失败: %s -> %s", resources_file, e)

    return {
        "success": failed == 0,
        "synced": synced,
        "failed": failed,
        "errors": errors,
    }


# ── 自测入口 ──

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    processes = collect_all_processes()
    print("\n" + "=" * 60)
    print(f"  进程采集结果（共 {len(processes)} 个组件）")
    print("=" * 60)
    for p in processes:
        status = "运行中" if p["running"] else "已停止"
        print(f"\n  [{status}] {p['key']}")
        print(f"    版本:  {p['version']}")
        print(f"    PID:   {p['pid'] or 'N/A'}")
        for k, v in p.get("process_info", {}).items():
            if v:
                print(f"    {k}:    {v}")
    print("\n" + "=" * 60)

    # 上传到 Redis 测试
    upload = upload_to_redis(processes)
    print(f"  Redis 上传结果:"
         f"  成功 {upload['uploaded']} / 失败 {upload['failed']}")
    if upload.get("errors"):
        for err in upload["errors"]:
            print(f"    [错误] {err}")
    print("=" * 60)
