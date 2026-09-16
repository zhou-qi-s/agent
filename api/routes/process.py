"""
进程查询相关的 API 路由
"""
import logging
import os
import socket
import time
from datetime import datetime
from typing import Any, Dict

import psutil
import yaml
from fastapi import APIRouter, HTTPException, Query

from core.process.process_info import _process_exists
from utils.config_loader import load_config
from utils.util import get_ip

router = APIRouter()

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")


def _read_version(service_dir: str) -> str:
    """读取服务目录下的 version 文件"""
    version_file = os.path.join(service_dir, "version")
    if not os.path.isfile(version_file):
        raise HTTPException(status_code=404, detail=f"version 文件不存在: {version_file}")

    try:
        with open(version_file, "r", encoding="utf-8") as f:
            version = f.read().strip()
    except Exception:
        raise HTTPException(status_code=500, detail="读取 version 文件失败")

    if not version:
        raise HTTPException(status_code=404, detail="version 内容为空")

    return version


def _read_pids(runtime_dir: str) -> list:
    """读取 runtime/pid 文件，支持多行 PID，返回有效的 PID 整数列表"""
    pid_file = os.path.join(runtime_dir, "pid")
    if not os.path.isfile(pid_file):
        return []

    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
    except Exception:
        return []

    pids = []
    for line in lines:
        try:
            pids.append(int(line))
        except ValueError:
            logging.warning("[_read_pids] 非法 PID 行，已忽略: %s", line)
    return pids


def _read_allowed_names(runtime_dir: str) -> list:
    """读取 runtime/config.yaml 中的 name 字段，返回允许的进程名列表"""
    config_path = os.path.join(runtime_dir, "config.yaml")
    if not os.path.isfile(config_path):
        return []
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        return []
    if not isinstance(cfg, dict):
        return []
    names = cfg.get("name", [])
    if isinstance(names, str):
        return [names]
    return names if isinstance(names, list) else []


def _safe(func, default=None):
    """psutil 安全调用，捕获进程相关异常"""
    try:
        return func()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return default


def _build_process_item(proc: psutil.Process,
                        io1_map: dict = None,
                        io2_map: dict = None) -> Dict[str, Any]:
    """
    构建单个进程的信息字典（含端口、命令行、状态等丰富信息）。

    io1_map / io2_map: 批量 IO 采样结果 {pid: io_counters}，
        避免每个进程单独 sleep(0.1)，大幅减少耗时。
    """
    pid = proc.pid

    with proc.oneshot():
        # cpu_percent(interval=None) 非阻塞，使用上次缓存值
        name = _safe(proc.name, "")
        cpu_percent = _safe(lambda: proc.cpu_percent(interval=None), 0.0)
        memory_info = _safe(proc.memory_info)
        memory_percent = _safe(proc.memory_percent, 0.0)
        num_threads = _safe(proc.num_threads, 0)
        create_time = _safe(proc.create_time)
        status = _safe(proc.status, "") or ""
        username = _safe(proc.username, "") or ""
        cmdline = _safe(proc.cmdline, []) or []
        cwd = _safe(proc.cwd, "") or ""

    item: Dict[str, Any] = {
        "pid": pid,
        "process_name": name,
        "cpu_percent": cpu_percent,
        "memory_rss_bytes": getattr(memory_info, "rss", 0) if memory_info else 0,
        "memory_vms_bytes": getattr(memory_info, "vms", 0) if memory_info else 0,
        "memory_percent": memory_percent,
        "num_threads": num_threads,
        "create_time": datetime.fromtimestamp(create_time).isoformat() if create_time else None,
        "status": status,
        "username": username,
        "cmdline": cmdline,
        "cwd": cwd,
        "ports": [],
        "io_read_bytes_per_sec": 0.0,
        "io_write_bytes_per_sec": 0.0,
        "uptime_seconds": 0,
    }

    # 运行时长
    ct = _safe(proc.create_time)
    if ct:
        item["uptime_seconds"] = int(time.time() - ct)

    # 监听端口
    ports: list = []
    for conn in _safe(lambda: proc.connections(kind="inet"), []) or []:
        if getattr(conn, "status", "") == "LISTEN":
            ports.append({
                "ip": getattr(conn.laddr, "ip", "") or "",
                "port": getattr(conn.laddr, "port", 0) or 0,
                "type": "tcp" if getattr(conn, "type", 0) == socket.SOCK_STREAM else "udp",
            })
    item["ports"] = ports

    # IO 瞬时速率：从批量采样结果中计算，避免单独 sleep
    if io1_map and io2_map and pid in io1_map and pid in io2_map:
        try:
            io1 = io1_map[pid]
            io2 = io2_map[pid]
            item["io_read_bytes_per_sec"] = (getattr(io2, "read_bytes", 0) - getattr(io1, "read_bytes", 0)) / 0.1
            item["io_write_bytes_per_sec"] = (getattr(io2, "write_bytes", 0) - getattr(io1, "write_bytes", 0)) / 0.1
        except Exception:
            pass

    return item


def _collect_all_pids(pids: list) -> list:
    """展开所有根 PID 及其子进程 PID，去重后返回"""
    all_pids = []
    seen = set()
    for pid in pids:
        if pid not in seen and _process_exists(pid):
            seen.add(pid)
            all_pids.append(pid)
            try:
                proc = psutil.Process(pid)
                for child in proc.children(recursive=True):
                    if child.pid not in seen:
                        seen.add(child.pid)
                        all_pids.append(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    return all_pids


def _batch_io_sample(all_pids: list):
    """
    批量 IO 采样：一次 sleep(0.1) 获取所有进程的 IO 速率。
    返回 (io1_map, io2_map)，均为 {pid: io_counters}。
    """
    io1_map = {}
    for pid in all_pids:
        try:
            proc = psutil.Process(pid)
            io1_map[pid] = proc.io_counters()
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            pass

    time.sleep(0.1)

    io2_map = {}
    for pid in all_pids:
        if pid in io1_map:
            try:
                proc = psutil.Process(pid)
                io2_map[pid] = proc.io_counters()
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                pass

    return io1_map, io2_map


def _get_process_tree(pid: int, io1_map: dict = None, io2_map: dict = None) -> Dict[str, Any]:
    """获取父进程和所有子进程信息，返回 { "parent": {...}, "children": [...] }"""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return {"parent": None, "children": []}
    except psutil.AccessDenied:
        return {"parent": None, "children": []}

    # 父进程信息
    parent_info = _build_process_item(proc, io1_map, io2_map)

    # 子进程信息
    children_list = []
    try:
        for child in proc.children(recursive=True):
            try:
                children_list.append(_build_process_item(child, io1_map, io2_map))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    return {"parent": parent_info, "children": children_list}


@router.get("/query")
async def query_process_by_service(
        service_name: str = Query(..., description="服务名称"),
        type: int = Query(1, description="服务类型: 1=普通服务, 3=显控台(路径加displayConsole)"),
):
    """
    根据服务名称查询进程信息。

    流程:
        1. 在 download 目录下定位服务文件夹
        2. 读取 version 文件获取版本号
        3. 读取 {version}/runtime/pid 获取 PID（支持多行多 PID）
        4. 检查各 PID 是否存活
        5. 获取进程名、CPU、内存、IO、端口等信息，自动去重

    type 参数:
        type=1（默认）: 普通服务，路径 download/{service_name}
        type=3: 显控台服务，路径 download/displayConsole/{service_name}

    返回:
        进程名、PID、是否存活、CPU、内存、IO、端口等信息
    """
    if not _DOWNLOAD_BASE:
        raise HTTPException(status_code=500, detail="config.yaml 中未配置 server.download")

    base_path = os.path.join(_DOWNLOAD_BASE, "displayConsole") if type == 3 else _DOWNLOAD_BASE
    service_dir = os.path.join(base_path, service_name)
    if not os.path.isdir(service_dir):
        raise HTTPException(status_code=404, detail=f"服务目录不存在: {service_dir}")

    # 读取版本号
    version = _read_version(service_dir)

    # 定位 runtime 目录
    runtime_dir = os.path.join(service_dir, version, "runtime")
    if not os.path.isdir(runtime_dir):
        raise HTTPException(status_code=404, detail=f"runtime 目录不存在: {runtime_dir}")

    # 读取 PID（支持多行多 PID）
    pids = _read_pids(runtime_dir)

    # Step 1: 展开所有进程 PID（根 + 子进程），用于批量 IO 采样
    all_pids = _collect_all_pids(pids)

    # Step 2: 批量 IO 采样（只 sleep 一次 100ms，而不是每个进程 sleep）
    io1_map, io2_map = _batch_io_sample(all_pids)

    # Step 3: 构建进程树，传入批量 IO 结果
    processes: list = []
    seen_pids: set = set()

    for pid in pids:
        if pid not in seen_pids and _process_exists(pid):
            seen_pids.add(pid)
            tree = _get_process_tree(pid, io1_map, io2_map)
            if tree["parent"]:
                processes.append(tree["parent"])
            for child in tree["children"]:
                if child["pid"] not in seen_pids:
                    seen_pids.add(child["pid"])
                    processes.append(child)

    # Step 4: 按 config.yaml 中的 name 字段过滤，只保留匹配的进程
    allowed_names = _read_allowed_names(runtime_dir)
    if allowed_names:
        processes = [
            p for p in processes
            if any(an.lower() in (p.get("process_name") or "").lower() for an in allowed_names)
        ]

    return {
        "code": 200,
        "message": "查询成功",
        "ip": get_ip(),
        "data": processes,
        "summary": {
            "total_processes": len(processes),
            "total_cpu_percent": round(sum(p.get("cpu_percent", 0) or 0 for p in processes), 2),
            "total_memory_rss_bytes": sum(p.get("memory_rss_bytes", 0) or 0 for p in processes),
            "total_memory_vms_bytes": sum(p.get("memory_vms_bytes", 0) or 0 for p in processes),
            "total_memory_percent": round(sum(p.get("memory_percent", 0) or 0 for p in processes), 2),
            "total_threads": sum(p.get("num_threads", 0) or 0 for p in processes),
            "total_io_read_bytes_per_sec": round(sum(p.get("io_read_bytes_per_sec", 0) or 0 for p in processes), 2),
            "total_io_write_bytes_per_sec": round(sum(p.get("io_write_bytes_per_sec", 0) or 0 for p in processes), 2),
        },
        "service_name": service_name,
        "version": version,
        "query_time": datetime.now().isoformat(),
    }
