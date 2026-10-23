"""
进程信息采集模块

【缓存区 / 运行区分离后的采集规则】

以「运行区」为采集依据，逐个服务执行：
    1. 读 {server.apps}/{服务名}/state/config.yaml
    2. 判断 runtime 字段：
         runtime != true  → 服务未运行，跳过
         runtime == true  → 继续
    3. 读取其中的 pid 字段（**支持多个 pid**，换行分隔）
    4. 过滤出仍存活的 pid：
         全部已死 → 把状态里的 pid 清空、runtime 置 false
         部分存活 → 把状态里的 pid 更新为存活列表
    5. 对存活 pid 采集进程详情（含子进程）
    6. 支持多进程服务：每个存活 pid 各采一条，各自展开子进程

运行区结构：
    {server.apps}/{服务名}/
        ├── state/config.yaml     运行状态（pid / name / runtime / version）
        ├── current -> {download}/{服务名}/{版本}
        ├── app/bin/config -> current/xxx
        └── runtime/              采集结果 resources.txt 落点
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
from utils.app_path import (
    find_apps_component_dir,
    read_state,
    read_state_pids,
    filter_alive_pids,
    refresh_state_pids,
    get_process_names,
)
from utils.util import get_ip

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

# 显控台服务的一级目录名：{apps}/displayConsole/{服务名}
_XKT_SERVICE_ROOT = "displayConsole"

# 插件应用的一级目录名：{apps}/plugin/{服务名}
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


def _resolve_version(service_name: str, sub_dir: str = "") -> str:
    """
    解析服务当前生效的版本号（信任脚本模式的版本判定依据）。

    优先解析运行区的 current 软链：{apps}[/{sub_dir}]/{服务名}/current
        → 指向 {download}[/{sub_dir}]/{服务名}/{版本}
        → 取链接目标 basename 即为版本号。

    为什么不用 state.version：start 任务会把下发的 version 参数写回
    state/config.yaml —— 如果 start 是按旧绑定（旧应用）发起的，
    state.version 会被覆盖回旧值，巡检就会拿旧版本目录的 app.yaml
    去匹配进程名。current 软链只反映 install 真正落地的版本，更可靠。

    解析失败（软链不存在/损坏）时回退 state.version。

    参数:
        service_name: 服务名（运行区目录名）
        sub_dir:      应用类别层（"" / displayConsole / plugin）

    返回:
        版本号字符串（可能为空）
    """
    root = _APPS_BASE
    if sub_dir:
        root = os.path.join(root, sub_dir)
    current_link = os.path.join(root, service_name, "current")
    try:
        if os.path.islink(current_link):
            target = os.readlink(current_link)
            # 相对链接按链接所在目录解析
            if not os.path.isabs(target):
                target = os.path.join(os.path.dirname(current_link), target)
            target = os.path.normpath(target)
            version = os.path.basename(target)
            if version and version != service_name:
                return version
    except OSError as e:
        logging.warning("[版本巡检] 解析 current 软链失败: %s -> %s", current_link, e)

    # 回退：state.version
    state = read_state(service_name, sub_dir)
    if state:
        return str(state.get("version", "") or "").strip()
    return ""


def _read_proc_name(service_name: str, version: str, sub_dir: str = "") -> str:
    """
    读版本目录 config/app.yaml 的 name 字段作为进程名（信任脚本模式下唯一的匹配依据）。

    app.yaml 是应用包自带元数据，跟随版本走；没有 name 字段时回退用服务名
    （目录名）匹配——兜底行为，正常包都应声明 name。

    参数:
        service_name: 服务名（运行区/下载区目录名）
        version:      版本号
        sub_dir:      应用类别层

    返回:
        进程名
    """
    base = _DOWNLOAD_BASE
    if sub_dir:
        base = os.path.join(base, sub_dir)
    yaml_path = os.path.join(base, service_name, version, "config", "app.yaml")
    if os.path.isfile(yaml_path):
        try:
            import yaml
            with open(yaml_path, "r", encoding="utf-8") as f:
                meta = yaml.safe_load(f) or {}
            name = str(meta.get("name", "") or "").strip()
            if name:
                return name
            logging.warning("[name巡检] app.yaml 缺少 name 字段，回退用服务名匹配: %s", yaml_path)
        except Exception as e:
            logging.warning("[name巡检] 读 app.yaml 失败: %s -> %s", yaml_path, e)
    else:
        logging.warning("[name巡检] app.yaml 不存在，回退用服务名匹配: %s", yaml_path)
    return service_name


def _match_processes_by_name(proc_name: str) -> List[int]:
    """
    按进程名精准匹配节点上的进程（可匹配多个——一个应用多进程是正常形态）。

    参数:
        proc_name: app.yaml 声明的进程名

    返回:
        匹配到的 PID 列表（可能为空）
    """
    matched: List[int] = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] == proc_name:
                matched.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return matched


def _build_app_agg_entry(service_name: str, version: str, proc_name: str,
                         matched_pids: List[int], timestamp: int) -> Dict[str, Any]:
    """
    构建应用级聚合条目（平台刷实例状态的依据）。

    写入 Redis 键 App/{ip}/{服务名}（List，保留最近 15 条，与 Process/ 键同模式）。
    running 只看是否有匹配进程，与启动方式无关（平台内外启动一视同仁）。
    """
    return {
        "agg": True,
        "key": f"App/{get_ip()}/{service_name}",
        "service_name": service_name,
        "version": version,
        "proc_name": proc_name,
        "running": bool(matched_pids),
        "pids": [str(p) for p in matched_pids],
        "process_count": len(matched_pids),
        "timestamp": timestamp,
    }


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
        service_dir: str, entry: str, timestamp: int, sub_dir: str = ""
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    采集单个服务的进程信息（供线程池并行调用）。

    采集依据（虚拟机应用，sub_dir=""）：
        **按 app.yaml 的 name 精准匹配节点进程**（信任脚本模式）——
        不管应用是谁启动的（平台 / 用户手动 / systemd），只要进程名对得上就采，
        多进程全收。app.yaml 没有 name 时回退用服务名匹配。
        显控台 / 插件（sub_dir 非空）仍走旧的 state/pid 逻辑，互不影响。

    旧依据（仅显控台 / 插件还在用）：
        运行区 {apps}[/{sub_dir}]/{服务名}/state/config.yaml
            - runtime 必须为 true，否则视为未运行
            - pid 字段支持多个 pid（换行分隔）
            - 死 pid 会被过滤，并顺带回写状态文件

    参数:
        service_dir: 运行区服务目录路径 {apps}[/{sub_dir}]/{entry}
        entry:       服务名称（目录名）
        timestamp:   采集时间戳
        sub_dir:     应用类别层（"" / displayConsole / plugin）

    返回:
        (entry, [进程条目列表])
    """
    processes: List[Dict[str, Any]] = []

    if sub_dir == "":
        # ── 虚拟机应用：name 巡检（信任脚本模式）──
        state = read_state(entry, sub_dir)
        if not state:
            return (entry, processes)
        version = _resolve_version(entry, sub_dir)
        if not version:
            return (entry, processes)
        proc_name = _read_proc_name(entry, version)
        matched = _match_processes_by_name(proc_name)
        seen: set = set()
        for pid in matched:
            processes.append(_build_process_entry(pid, entry, version, timestamp))
            for child_pid in _get_children_pids(pid):
                if child_pid not in seen:
                    seen.add(child_pid)
                    processes.append(_build_process_entry(child_pid, entry, version, timestamp))
        if matched:
            logging.info(
                "[name巡检] %s: 进程名=%s 匹配 %d 个进程 %s",
                entry, proc_name, len(matched), matched,
            )
        else:
            logging.debug("[name巡检] %s: 进程名=%s 无匹配进程", entry, proc_name)
        return (entry, processes)

    # ── 显控台 / 插件：旧 state/pid 逻辑 ──
    # ── 读运行状态 ──
    state = read_state(entry, sub_dir)
    if not state:
        return (entry, processes)

    if not state.get("runtime"):
        return (entry, processes)

    version = str(state.get("version", "") or "").strip()

    # ── 取 pids 数组并过滤存活 ──
    pid_list = read_state_pids(entry, sub_dir)
    if not pid_list:
        return (entry, processes)

    alive_pids = filter_alive_pids(pid_list, entry)

    # 死 pid 清理：全部死 → 清空 + runtime=false；部分死 → 只留存活的
    if len(alive_pids) != len(pid_list):
        refresh_state_pids(entry, alive_pids, get_process_names(alive_pids), sub_dir)

    if not alive_pids:
        return (entry, processes)

    # ── 逐个 pid 采集（含子进程）──
    seen: set = set()
    for pid_int in alive_pids:

        # 采集父进程
        processes.append(_build_process_entry(pid_int, entry, version, timestamp))

        # 采集所有子进程
        for child_pid in _get_children_pids(pid_int):
            if child_pid not in seen:
                seen.add(child_pid)
                processes.append(_build_process_entry(child_pid, entry, version, timestamp))

    return (entry, processes)


def _iter_service_dirs() -> List[Tuple[str, str, str]]:
    """
    收集所有需要采集的服务目录（**扫描运行区**）。

    支持三种目录结构：
        {apps}/{服务名}/state/config.yaml                    ← 虚拟机 / 通用服务
        {apps}/displayConsole/{服务名}/state/config.yaml     ← 显控台服务
        {apps}/plugin/{服务名}/state/config.yaml             ← 插件服务

    有效服务判据：目录下存在 state/config.yaml（install 阶段写入）。
    仅 runtime=true 的会在采集时被真正处理，此处只做目录筛选。

    返回:
        [(service_dir, service_name, sub_dir), ...]
        —— sub_dir 是应用类别层（"" / displayConsole / plugin），
           调用方读取状态文件时必须一并传入，否则会按「一楼」去找而读不到。
    """
    tasks: List[Tuple[str, str, str]] = []

    if not _APPS_BASE or not os.path.isdir(_APPS_BASE):
        return tasks

    scan_roots: List[Tuple[str, str]] = [(_APPS_BASE, "")]
    for sub_root in (_XKT_SERVICE_ROOT, _PLUGIN_SERVICE_ROOT):
        root = os.path.join(_APPS_BASE, sub_root)
        if os.path.isdir(root):
            scan_roots.append((root, sub_root))

    for root, sub_dir in scan_roots:
        try:
            entries = os.listdir(root)
        except Exception as e:
            logging.warning("[服务扫描] 读取目录失败: %s -> %s", root, e)
            continue

        for entry in entries:
            service_dir = os.path.join(root, entry)
            # 跳过软链接（运行区内的 current/app/bin/config 是链接，不是服务目录）
            if os.path.islink(service_dir) or not os.path.isdir(service_dir):
                continue
            # 有效服务判据：存在 state/config.yaml
            state_file = os.path.join(service_dir, "state", "config.yaml")
            if not os.path.isfile(state_file):
                continue
            tasks.append((service_dir, entry, sub_dir))

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

    if not _APPS_BASE or not os.path.isdir(_APPS_BASE):
        logging.warning("[collect_all_processes] 运行区目录不存在: %s", _APPS_BASE)
        return result

    # Step 1: 收集所有待采集的服务目录（扫运行区，含显控台 displayConsole 下的服务）
    service_tasks: List[Tuple[str, str, str]] = _iter_service_dirs()  # [(service_dir, entry, sub_dir), ...]

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
            executor.submit(_collect_service_processes, sd, en, timestamp, sdir): en
            for sd, en, sdir in service_tasks
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


def collect_all_with_agg(max_workers: int = 8) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    进程采集 + 应用级聚合状态（信任脚本模式的主采集入口）。

    在 collect_all_processes 之外，额外为每个虚拟机应用产出一条聚合条目
    （App/{ip}/{服务名}）：running 只取决于 name 精准匹配到的进程数，
    与启动方式无关 —— 平台内外启动一视同仁。

    返回:
        (进程条目列表, 应用聚合条目列表)
    """
    timestamp = int(datetime.now().timestamp() * 1000)
    processes: List[Dict[str, Any]] = []
    aggs: List[Dict[str, Any]] = []

    if not _APPS_BASE or not os.path.isdir(_APPS_BASE):
        logging.warning("[collect_all_with_agg] 运行区目录不存在: %s", _APPS_BASE)
        return processes, aggs

    service_tasks = _iter_service_dirs()
    if not service_tasks:
        return processes, aggs

    actual_workers = min(max_workers, len(service_tasks))
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=actual_workers) as executor:
        futures = {
            executor.submit(_collect_service_with_agg, sd, en, timestamp, sdir): en
            for sd, en, sdir in service_tasks
        }
        for future in as_completed(futures):
            try:
                svc_processes, svc_agg = future.result()
                processes.extend(svc_processes)
                if svc_agg:
                    aggs.append(svc_agg)
            except Exception as e:
                logging.error("[collect_all_with_agg] 采集异常: %s", e)

    elapsed = time.time() - t_start
    logging.info(
        "[collect_all_with_agg] 采集完成: %d 进程 / %d 应用, 耗时 %.1fs",
        len(processes), len(aggs), elapsed,
    )
    return processes, aggs


def _collect_service_with_agg(service_dir: str, entry: str, timestamp: int,
                              sub_dir: str = "") -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    单服务采集（带聚合）。虚拟机应用产出 name 巡检结果 + 聚合条目；
    显控台/插件走旧 state/pid 逻辑，不产出聚合条目。
    """
    if sub_dir != "":
        processes = _collect_service_processes(service_dir, entry, timestamp, sub_dir)[1]
        return processes, None

    # ── 虚拟机应用：name 巡检 ──
    state = read_state(entry, sub_dir)
    if not state:
        return [], None
    version = _resolve_version(entry, sub_dir)
    if not version:
        return [], None

    proc_name = _read_proc_name(entry, version)
    matched = _match_processes_by_name(proc_name)

    processes: List[Dict[str, Any]] = []
    seen: set = set()
    for pid in matched:
        processes.append(_build_process_entry(pid, entry, version, timestamp))
        for child_pid in _get_children_pids(pid):
            if child_pid not in seen:
                seen.add(child_pid)
                processes.append(_build_process_entry(child_pid, entry, version, timestamp))

    if matched:
        logging.info("[name巡检] %s: 进程名=%s 匹配 %d 个进程 %s", entry, proc_name, len(matched), matched)
    else:
        logging.debug("[name巡检] %s: 进程名=%s 无匹配进程", entry, proc_name)

    agg = _build_app_agg_entry(entry, version, proc_name, matched, timestamp)
    return processes, agg


def upload_to_redis(processes: List[Dict[str, Any]], expire: int = 600,
                    aggs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    将进程信息与应用聚合状态上传到 Redis。

    参数:
        processes: collect_all_processes() 返回的进程列表
        expire:    Redis key 过期时间（秒），默认 600 秒（10 分钟）
        aggs:      应用聚合状态列表（App/{ip}/{服务名}），可选

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

    def _push(key: str, value: Dict[str, Any]):
        nonlocal uploaded, failed
        pipe = r.pipeline()
        pipe.lpush(key, json.dumps(value, ensure_ascii=False))
        pipe.ltrim(key, 0, 14)
        pipe.expire(key, expire)
        pipe.execute()
        uploaded += 1
        logging.info("[upload_to_redis] 已上传: %s", key)

    for p in processes:
        key = p.get("key", "")
        if not key:
            failed += 1
            errors.append(f"缺少 key: {p.get('service_name', 'unknown')}")
            continue
        try:
            _push(key, {
                "service_name": p.get("service_name", ""),
                "version": p.get("version", ""),
                "pid": p.get("pid", ""),
                "running": p.get("running", False),
                "process_info": p.get("process_info", {}),
                "timestamp": p.get("timestamp", ""),
            })
        except Exception as e:
            failed += 1
            errors.append(f"{key}: {e}")
            logging.warning("[upload_to_redis] 上传失败: %s -> %s", key, e)

    # 应用聚合状态：平台刷实例状态的依据（运行中/已停止，与启动方式无关）
    for a in (aggs or []):
        key = a.get("key", "")
        if not key:
            continue
        try:
            _push(key, {
                "service_name": a.get("service_name", ""),
                "version": a.get("version", ""),
                "proc_name": a.get("proc_name", ""),
                "running": a.get("running", False),
                "pids": a.get("pids", []),
                "process_count": a.get("process_count", 0),
                "timestamp": a.get("timestamp", ""),
            })
        except Exception as e:
            failed += 1
            errors.append(f"{key}: {e}")
            logging.warning("[upload_to_redis] 聚合状态上传失败: %s -> %s", key, e)

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
    processes, aggs = collect_all_with_agg()
    upload_result = upload_to_redis(processes, expire=expire, aggs=aggs)
    # 上传后立即同步到 resources.txt
    sync_result = sync_redis_to_resources(expire=expire)
    return {
        "success": upload_result["success"] and sync_result["success"],
        "process_count": len(processes),
        "processes": processes,
        "aggs": aggs,
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

    if not _APPS_BASE or not os.path.isdir(_APPS_BASE):
        logging.warning("[sync_redis_to_resources] 运行区目录不存在: %s", _APPS_BASE)
        return {"success": False, "synced": 0, "failed": 0, "error": "运行区目录不存在"}

    # 收集所有服务 → 版本 → PID 的映射（扫运行区，含显控台 displayConsole 下的服务）
    # service_map: {service_dir: {"service_name": "xxx", "version": "1.0.0", "pids": [123, 456]}}
    service_map: Dict[str, Dict[str, Any]] = {}

    for service_dir, entry, sub_dir in _iter_service_dirs():
        # 读运行状态：runtime 必须为 true
        # 注意带 sub_dir：显控台/插件服务在运行区多一层，少了它读不到状态
        state = read_state(entry, sub_dir)
        if not state or not state.get("runtime"):
            continue

        version = str(state.get("version", "") or "").strip()

        # 取 pids 数组，过滤存活
        alive = filter_alive_pids(read_state_pids(entry, sub_dir), entry)
        if not alive:
            continue

        pids = []
        seen = set()
        for pid_int in alive:
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

    # 写入各服务的 resources.txt（落在运行区 {apps}/{服务名}/runtime/）
    for service_dir_key, process_list in grouped.items():
        # 落点直接由运行区组件目录推导，不再依赖版本号
        runtime_dir = os.path.join(service_dir_key, "runtime")
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
