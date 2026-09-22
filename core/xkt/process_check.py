"""
显控台 / 插件 进程巡检模块

【缓存区/运行区分离后的巡检规则】

扫描「运行区」各服务的目录，读取 state/config.yaml 中的进程名（processes），
查找真实在跑的进程并回写 PID 到运行状态。

    1. 遍历 {server.apps}/[{sub_dir}/] 下所有服务目录
    2. 读 state/config.yaml：
         runtime != true            → 跳过（未运行）
         无 processes 字段          → 跳过
    3. 按进程名查找真实进程（用于处理升级/重启后 PID 变化）
    4. 有存活进程 → 回写 pids/processes；无 → 清空并置 runtime=false

原实现遍历 {download}/displayConsole/ 并读写 {version}/runtime/{config.yaml,pid}，
依赖已废弃的 version 文件，故一并改造。
"""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
import yaml

from utils.app_path import (
    SUB_DIR_PLUGIN,
    SUB_DIR_XKT,
    find_apps_component_dir,
    read_state,
    write_state,
    refresh_state_pids,
    get_process_names,
)
from utils.config_loader import get_apps_dir, load_config


# =============================================================================
# 配置
# =============================================================================

# 显控台 / 插件 服务的类别子目录（运行区下）
_XKT_SERVICE_ROOT = SUB_DIR_XKT
_PLUGIN_SERVICE_ROOT = SUB_DIR_PLUGIN


def _get_apps_path() -> str:
    """获取运行区（server.apps）目录路径"""
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


# =============================================================================
# 进程查询
# =============================================================================

def _is_under_dir(target_path: str, parent_dir: str) -> bool:
    """判断 target_path 是否在 parent_dir 目录树下（规范化路径后比较前缀）"""
    try:
        normalized_target = os.path.normpath(os.path.realpath(target_path))
        normalized_parent = os.path.normpath(os.path.realpath(parent_dir))
    except Exception:
        return False
    # 确保是比较目录前缀，而不是字符串前缀（避免 /app 匹配 /app2）
    if os.name == "nt":
        return normalized_target.lower().startswith(normalized_parent.lower() + os.sep)
    return normalized_target.startswith(normalized_parent + os.sep)


def _find_process_by_name(process_name: str) -> List[Dict[str, Any]]:
    """
    按进程名称查找正在运行的进程（跨平台）

    参数:
        process_name: 进程名称，如 'java'、'python'、'yyxx.exe'

    返回:
        匹配的进程信息列表 [{"pid": 123, "name": "java", "cmdline": "..."}, ...]
    """
    found: List[Dict[str, Any]] = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                info = proc.info or {}
                pname = info.get('name', '') or ''
                cmdline = ' '.join(info.get('cmdline', []) or [])
                # 匹配进程名或命令行中包含目标名称
                if process_name.lower() in pname.lower() or process_name.lower() in cmdline.lower():
                    found.append({
                        "pid": info.get('pid'),
                        "name": pname,
                        "cmdline": cmdline,
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        logging.error("[xkt巡检] 遍历进程失败: %s", e)

    return found


# 是否按"进程工作目录(cwd)"二次过滤：
#   True  = 只认 cwd 在服务目录下的进程（可避免同名进程误伤，但服务若从其他目录启动会漏判）
#   False = 只要进程名/命令行匹配即认可（当前取值；显控台服务可能运行在其他目录，先不过滤）
FILTER_BY_CWD = False


def collect_service_pids(
    service_dir: str,
    process_names: Optional[List[str]] = None,
) -> List[int]:
    """
    查找服务当前"真实在跑"的进程 PID（升级/重启后即为新进程的 PID）。

    匹配规则:
        1. 按传入的 process_names 找候选进程
           （改造后由调用方从运行区 state/config.yaml 的 processes 字段读取，
             不再读 {版本}/runtime/config.yaml）
        2. （可选）再按 cwd 过滤，只保留工作目录在该服务目录下的进程
           —— 由 FILTER_BY_CWD 控制，默认关闭。

    参数:
        service_dir:   运行区服务目录，如 {apps}/displayConsole/{服务名}（用于 cwd 过滤）
        process_names: 进程名列表

    返回:
        存活的 PID 列表（已去重），查不到返回空列表
    """
    # 兼容 name 为单字符串的情况
    if isinstance(process_names, str):
        process_names = [process_names]

    if not process_names:
        logging.debug("[xkt巡检] %s 未提供进程名，无法扫描进程", service_dir)
        return []

    service_name = os.path.basename(os.path.normpath(service_dir))
    all_pids: List[int] = []

    for pname in process_names:
        matched_pids: List[int] = []
        for p in _find_process_by_name(pname):
            pid = p["pid"]

            # ── 按工作目录精确过滤（FILTER_BY_CWD=True 时生效）──
            if FILTER_BY_CWD:
                try:
                    proc_cwd = psutil.Process(pid).cwd()
                except Exception:
                    continue
                if not _is_under_dir(proc_cwd, service_dir):
                    logging.debug(
                        "[xkt巡检] 跳过无关进程: PID=%s cwd=%s（不在 %s 下）",
                        pid, proc_cwd, service_dir
                    )
                    continue

            matched_pids.append(pid)

        if matched_pids:
            logging.info(
                "[xkt巡检] 服务=%s, 进程名=%s, 运行中, PID=%s",
                service_name, pname, matched_pids
            )
            all_pids.extend(matched_pids)
        else:
            logging.info(
                "[xkt巡检] 服务=%s, 进程名=%s, 未运行",
                service_name, pname
            )

    # 去重（保持顺序）
    return list(dict.fromkeys(all_pids))


# 注：原 _clear_pid_file() / _write_pid_file() / _read_runtime_config() 已删除。
#     它们维护 {版本}/runtime/{pid,config.yaml} 这套旧落点：
#       · pid 现统一由运行状态 {apps}/[{sub_dir}/]{服务}/state/config.yaml 的 pids 数组承载
#       • 进程名移到 state 的 processes 字段
#       · 资源监控也改为读 state
#     故三个函数均无调用方，一并移除。


# =============================================================================
# 巡检主逻辑
# =============================================================================

def check_service_group(root_dir: str, tag: str = "xkt巡检",
                        sub_dir: str = "") -> List[Dict[str, Any]]:
    """
    通用服务巡检（**扫运行区**）：

    流程:
        1. 遍历 {root_dir} 下所有服务子目录
        2. 读 state/config.yaml：
             runtime != true  → 跳过
             无 processes     → 跳过
        3. 按进程名查找真实在跑的进程（升级/重启后 PID 会变）
        4. 有存活 → 回写 pids/processes；无 → 清空并置 runtime=false

    参数:
        root_dir: 类别服务根目录，如 {apps}/displayConsole 或 {apps}/plugin
        tag:      日志前缀，便于区分显控台 / 插件
        sub_dir:  类别子目录（用于回写状态；留空则从 root_dir 末尾推断）

    返回:
        巡检结果列表，每项包含 service_name、version、process_names、running、pids
    """
    results: List[Dict[str, Any]] = []

    if not os.path.isdir(root_dir):
        logging.debug("[%s] 目录不存在: %s", tag, root_dir)
        return results

    for entry in os.listdir(root_dir):
        service_dir = os.path.join(root_dir, entry)
        # 跳过软链接（current/app/bin/config）与普通文件
        if os.path.islink(service_dir) or not os.path.isdir(service_dir):
            continue

        service_name = entry

        # ── 读运行状态 ──
        state = read_state(service_name, sub_dir)
        if not state:
            logging.debug("[%s] 服务 %s 无运行状态文件，跳过", tag, service_name)
            continue

        if not state.get("runtime"):
            logging.debug("[%s] 服务 %s 未运行，跳过", tag, service_name)
            continue

        version = str(state.get("version", "") or "").strip()

        # ── 读取进程名列表（来自 state 的 processes 字段）──
        process_names = state.get("processes", [])
        if not process_names:
            logging.warning("[%s] 服务 %s 的状态中未记录进程名，跳过", tag, service_name)
            continue
        if isinstance(process_names, str):
            process_names = [process_names]

        # ── 查找真实在跑的进程（升级/重启后即为新进程 PID）──
        all_pids = collect_service_pids(service_dir, process_names)

        # ── 同步 PID 记录到运行状态 ──
        if all_pids:
            refresh_state_pids(service_name, all_pids, get_process_names(all_pids), sub_dir)
        else:
            logging.info("[%s] 服务 %s 无存活进程, 清空运行状态", tag, service_name)
            refresh_state_pids(service_name, [], [], sub_dir)

        results.append({
            "service_name": service_name,
            "version": version,
            "process_names": process_names,
            "running": len(all_pids) > 0,
            "pids": all_pids,
        })

    return results


def check_all_xkt_services() -> List[Dict[str, Any]]:
    """
    显控台进程巡检：遍历运行区 {apps}/displayConsole/ 子目录。
    具体逻辑见 check_service_group()。
    """
    apps_path = _get_apps_path()
    return check_service_group(
        os.path.join(apps_path, _XKT_SERVICE_ROOT), "xkt巡检", SUB_DIR_XKT)


def check_all_plugin_services() -> List[Dict[str, Any]]:
    """
    插件进程巡检：遍历运行区 {apps}/plugin/ 子目录。
    具体逻辑同 check_service_group()。
    """
    apps_path = _get_apps_path()
    return check_service_group(
        os.path.join(apps_path, _PLUGIN_SERVICE_ROOT), "plugin巡检", SUB_DIR_PLUGIN)


# =============================================================================
# 定时巡检
# =============================================================================

def xkt_check_loop(interval: int = 10):
    """
    显控台 / 插件进程巡检循环（用于后台线程）

    每轮依次巡检两类服务，均按运行状态 state/config.yaml 记录的进程名
    查找真实在跑进程，并把 PID 写入 {版本}/runtime/pid。

    参数:
        interval: 巡检间隔（秒），默认 10 秒
    """
    logging.info("[xkt巡检] 显控台/插件进程巡检线程启动, 间隔=%ds", interval)
    while True:
        t0 = time.time()

        for tag, checker, root_name in (
            ("xkt巡检", check_all_xkt_services, _XKT_SERVICE_ROOT),
            ("plugin巡检", check_all_plugin_services, _PLUGIN_SERVICE_ROOT),
        ):
            try:
                results = checker()
                if results:
                    running_count = sum(1 for r in results if r["running"])
                    logging.info(
                        "[%s] 本轮巡检完成: 共扫描 %d 个 %s 服务, 运行中 %d 个",
                        tag, len(results), root_name, running_count
                    )
                else:
                    logging.debug("[%s] 本轮未发现 %s 服务", tag, root_name)
            except Exception as e:
                logging.error("[%s] 巡检异常: %s", tag, e)

        elapsed = time.time() - t0
        if elapsed > interval:
            logging.warning(
                "[xkt巡检] 单轮耗时 %.1fs 超过间隔 %ds",
                elapsed, interval
            )
        else:
            time.sleep(max(1, interval - elapsed))


# =============================================================================
# 自测入口
# =============================================================================

def main():
    """显控台进程巡检手动测试入口"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print("  显控台进程巡检（单次执行）")
    print("=" * 60)
    results = check_all_xkt_services()
    for r in results:
        status = "运行中" if r["running"] else "已停止"
        print(f"  [{status}] {r['service_name']} (v{r.get('version', 'N/A')})")
        print(f"    进程名: {r.get('process_names', [])}")
        print(f"    PID:    {r.get('pids', [])}")
    print("=" * 60)


if __name__ == "__main__":
    main()
