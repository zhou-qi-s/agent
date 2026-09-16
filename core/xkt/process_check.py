"""
显控台进程巡检模块

遍历 download/displayConsole/ 目录，读取 {version}/runtime/config.yaml 中的进程名称，
查找进程并将 PID 写入 {version}/runtime/pid 文件。
"""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
import yaml

from utils.config_loader import load_config


# =============================================================================
# 配置
# =============================================================================

# 显控台服务的根目录名：{download}/displayConsole/{服务名}/{版本}
_XKT_SERVICE_ROOT = "displayConsole"

# 插件服务的根目录名：{download}/plugin/{服务名}/{版本}
_PLUGIN_SERVICE_ROOT = "plugin"


def _get_download_path() -> str:
    """获取 download 目录路径"""
    cfg = load_config()
    download = cfg.get("server", {}).get("download", "download")
    if not os.path.isabs(download):
        download = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            download
        )
    return download


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
    version_dir: str,
    process_names: Optional[List[str]] = None,
) -> List[int]:
    """
    查找服务当前"真实在跑"的进程 PID（升级/重启后即为新进程的 PID）。

    匹配规则:
        1. 按 {version_dir}/runtime/config.yaml 的 name 字段找候选进程
        2. （可选）再按 cwd 过滤，只保留工作目录在该服务目录下的进程
           —— 由 FILTER_BY_CWD 控制，默认关闭：进程可能运行在其他目录，
              强过滤会把真实进程漏掉；需要防同名误伤时再打开。

    参数:
        service_dir:   服务目录，如 download/displayConsole/{服务名}（用于 cwd 过滤）
        version_dir:   版本目录，如 {服务目录}/{版本}（用于读取 runtime/config.yaml）
        process_names: 进程名列表，缺省时从 runtime/config.yaml 读取

    返回:
        存活的 PID 列表（已去重），查不到返回空列表
    """
    if process_names is None:
        runtime_config = _read_runtime_config(version_dir)
        process_names = runtime_config.get("name", [])

    # 兼容 name 为单字符串的情况
    if isinstance(process_names, str):
        process_names = [process_names]

    if not process_names:
        logging.debug("[xkt巡检] %s 未配置进程名，无法扫描进程", version_dir)
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


def _clear_pid_file(version_dir: str):
    """
    删除 {version_dir}/runtime/pid

    服务已无存活进程时清理，避免陈旧 PID 被后续任务误读为"服务在运行"。
    """
    pid_file = os.path.join(version_dir, "runtime", "pid")
    try:
        if os.path.isfile(pid_file):
            os.remove(pid_file)
            logging.info("[xkt巡检] 已清理 PID 文件: %s", pid_file)
    except Exception as e:
        logging.warning("[xkt巡检] 清理 PID 文件失败: %s -> %s", pid_file, e)


# =============================================================================
# PID 文件写入（唯一落点：{version_dir}/runtime/pid）
#
# 说明：原先还额外维护一份 download/xkt/{service_name} 供资源监控读取，
# 现已废弃 —— 显控台 PID 只写 {版本}/runtime/pid，资源监控也改为读该文件。
# =============================================================================

def _write_pid_file(version_dir: str, pid_list: List[int]):
    """
    在 {version_dir}/runtime/pid 文件中写入进程 PID

    参数:
        version_dir: 版本目录路径
        pid_list: PID 列表
    """
    runtime_dir = os.path.join(version_dir, "runtime")
    try:
        Path(runtime_dir).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logging.error("[xkt巡检] 创建 runtime 目录失败: %s", e)
        return

    pid_file = os.path.join(runtime_dir, "pid")
    content = "\n".join(str(pid) for pid in pid_list)

    try:
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(content)
        logging.info("[xkt巡检] 已写入 PID 文件: %s, PIDs=%s", pid_file, pid_list)
    except Exception as e:
        logging.error("[xkt巡检] 写入 PID 文件失败: %s -> %s", pid_file, e)


# =============================================================================
# 读取 runtime/config.yaml
# =============================================================================

def _read_runtime_config(version_dir: str) -> Dict[str, Any]:
    """
    读取 {version_dir}/runtime/config.yaml

    返回:
        解析后的配置字典，失败返回空 dict
    """
    config_path = os.path.join(version_dir, "runtime", "config.yaml")
    if not os.path.isfile(config_path):
        logging.warning("[xkt巡检] runtime/config.yaml 不存在: %s", config_path)
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        return config if isinstance(config, dict) else {}
    except Exception as e:
        logging.error("[xkt巡检] 读取 runtime/config.yaml 失败: %s -> %s", config_path, e)
        return {}


# =============================================================================
# 巡检主逻辑
# =============================================================================

def check_service_group(root_dir: str, tag: str = "xkt巡检") -> List[Dict[str, Any]]:
    """
    通用服务巡检：遍历 {root_dir}/{服务名}/ 目录，读取 runtime/config.yaml 中的进程名称，
    查找进程并将 PID 写入 {version}/runtime/pid 文件。

    流程:
        1. 遍历 {root_dir} 下所有子目录
        2. 读取 version 文件获取版本号
        3. 读取 {version}/runtime/config.yaml 获取 name 字段（进程名列表）
        4. 按进程名查找进程
        5. 存活则写入 {version}/runtime/pid，无存活进程则删除该文件

    参数:
        root_dir: 服务根目录，如 {download}/displayConsole 或 {download}/plugin
        tag:      日志前缀，便于区分显控台 / 插件

    返回:
        巡检结果列表，每项包含 service_name、version、process_names、running、pids
    """
    results: List[Dict[str, Any]] = []

    if not os.path.isdir(root_dir):
        logging.debug("[%s] 目录不存在: %s", tag, root_dir)
        return results

    for entry in os.listdir(root_dir):
        service_dir = os.path.join(root_dir, entry)
        if not os.path.isdir(service_dir):
            continue

        service_name = entry

        # ── 读取 version 文件 ──
        version_file = os.path.join(service_dir, "version")
        version = ""
        if os.path.isfile(version_file):
            try:
                with open(version_file, "r", encoding="utf-8") as vf:
                    version = vf.read().strip()
            except Exception as e:
                logging.warning("[%s] 读取 version 失败: %s -> %s", tag, version_file, e)

        if not version:
            logging.warning("[%s] 服务 %s 未找到 version 文件，跳过", tag, service_name)
            continue

        # ── 定位版本目录并读取 runtime/config.yaml ──
        version_dir = os.path.join(service_dir, version)
        if not os.path.isdir(version_dir):
            logging.warning("[%s] 版本目录不存在: %s，跳过", tag, version_dir)
            continue

        runtime_config = _read_runtime_config(version_dir)
        if not runtime_config:
            logging.warning("[%s] 服务 %s 无 runtime/config.yaml，跳过", tag, service_name)
            continue

        # ── 读取进程名列表 ──
        process_names = runtime_config.get("name", [])
        if not process_names:
            logging.warning("[%s] 服务 %s 的 config.yaml 中未配置 name 字段，跳过", tag, service_name)
            continue

        # 兼容 name 为单字符串的情况
        if isinstance(process_names, str):
            process_names = [process_names]

        # ── 查找真实在跑的进程（升级/重启后即为新进程 PID）──
        all_pids = collect_service_pids(service_dir, version_dir, process_names)

        # ── 同步 PID 记录（唯一落点：{version_dir}/runtime/pid）──
        if all_pids:
            _write_pid_file(version_dir, all_pids)
        else:
            logging.info("[%s] 服务 %s 无存活进程, 清理 PID 记录", tag, service_name)
            _clear_pid_file(version_dir)

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
    显控台进程巡检：遍历 download/displayConsole/ 子目录。
    具体逻辑见 check_service_group()。
    """
    download_path = _get_download_path()
    return check_service_group(os.path.join(download_path, _XKT_SERVICE_ROOT), "xkt巡检")


def check_all_plugin_services() -> List[Dict[str, Any]]:
    """
    插件进程巡检：遍历 download/plugin/ 子目录。
    具体逻辑见 check_service_group()。
    """
    download_path = _get_download_path()
    return check_service_group(os.path.join(download_path, "plugin"), "plugin巡检")


# =============================================================================
# 定时巡检
# =============================================================================

def xkt_check_loop(interval: int = 10):
    """
    显控台 / 插件进程巡检循环（用于后台线程）

    每轮依次巡检两类服务，均按 runtime/config.yaml 声明的进程名
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
