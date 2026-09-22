"""
进程查询相关的 API 路由

【改造说明】
原实现从「下载区」读取 version 文件与 {version}/runtime/pid。
缓存区/运行区分离后：
    · 版本号 → 运行区 {apps}/{service_name}/state/config.yaml 的 version 字段
    · 进程号 → 同一状态文件的 pids 数组
    · runtime 目录 → {apps}/{service_name}/runtime/
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
from utils.app_path import (
    SUB_DIR_PLUGIN,
    SUB_DIR_XKT,
    resolve_sub_dir,
    read_state,
    read_state_pids,
    get_state_file,
    find_apps_component_dir,
)
from utils.config_loader import load_config
from utils.util import get_ip

router = APIRouter()

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

# 平台下发的 type → 应用类别层：1=普通/虚拟机（无分层）、3=显控台、4=插件
_SUB_DIR_BY_TYPE = {3: SUB_DIR_XKT, 4: SUB_DIR_PLUGIN}


def _read_version(service_name: str, sub_dir: str = "") -> str:
    """
    读取服务当前版本号。

    来源：运行区 {apps}[/{sub_dir}]/{service_name}/state/config.yaml 的 version 字段
    """
    state = read_state(service_name, sub_dir)
    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"运行状态文件不存在: {get_state_file(service_name, sub_dir)}")

    version = str(state.get("version", "") or "").strip()
    if not version:
        raise HTTPException(status_code=404, detail="运行状态中 version 字段为空")

    return version


def _read_pids(service_name: str, sub_dir: str = "") -> list:
    """
    读取服务的进程号列表。

    来源：运行区 {apps}[/{sub_dir}]/{service_name}/state/config.yaml 的 pids 数组
    """
    return read_state_pids(service_name, sub_dir)


def _read_allowed_names(service_name: str, sub_dir: str = "") -> list:
    """
    读取运行状态中的进程名列表（作为进程名白名单）。

    来源：运行区 state/config.yaml 的 processes 数组
    """
    state = read_state(service_name, sub_dir)
    if not state:
        return []
    names = state.get("processes", [])
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
        1. 在运行区定位服务目录 {apps}/{service_name}
        2. 读取 state/config.yaml 获取版本号与 pids 数组
        3. 检查各 PID 是否存活
        4. 获取进程名、CPU、内存、IO、端口等信息，自动去重

    type 参数:
        type=1（默认）: 普通服务（虚拟机），路径 {apps}/{service_name}
        type=3: 显控台服务，路径 {apps}/displayConsole/{service_name}
        type=4: 插件服务，路径   {apps}/plugin/{service_name}

    返回:
        进程名、PID、是否存活、CPU、内存、IO、端口等信息
    """
    if not _APPS_BASE:
        raise HTTPException(status_code=500, detail="config.yaml 中未配置 server.apps")

    # 先按平台下发的 type 判定类别层；type 与目录不符（或未下发）时自动探测，
    # 避免显控台/插件服务被误判成"运行区服务目录不存在"
    sub_dir = _SUB_DIR_BY_TYPE.get(type) or ""
    if sub_dir and not os.path.isdir(os.path.join(_APPS_BASE, sub_dir, service_name)):
        sub_dir = ""
    if not sub_dir:
        sub_dir = resolve_sub_dir(service_name, roots=[_APPS_BASE])

    base_path = os.path.join(_APPS_BASE, sub_dir) if sub_dir else _APPS_BASE
    service_dir = os.path.join(base_path, service_name)
    if not os.path.isdir(service_dir):
        raise HTTPException(status_code=404, detail=f"运行区服务目录不存在: {service_dir}")

    # 读取版本号（来自 state/config.yaml）
    version = _read_version(service_name, sub_dir)

    # runtime 目录（resources.txt 等运行态产物落点）
    runtime_dir = os.path.join(service_dir, "runtime")
    if not os.path.isdir(runtime_dir):
        raise HTTPException(status_code=404, detail=f"runtime 目录不存在: {runtime_dir}")

    # 读取 PID（来自 state/config.yaml 的 pids 数组）
    pids = _read_pids(service_name, sub_dir)

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

    # Step 4: 按运行状态中的 processes 字段过滤，只保留匹配的进程
    allowed_names = _read_allowed_names(service_name, sub_dir)
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
