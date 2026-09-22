"""
运行状态兜底清理模块

【定位：兜底手段】

主链路由 `process_info.py` 负责采集进程信息，并在采集过程中顺带清理死 pid。
但若 process_info 因异常 / 卡住 / 未启动而没能跑，`state/config.yaml` 中可能
长期残留 `runtime: true`（但进程其实早已退出）的错误状态。

本模块作为**兜底**，独立于 process_info 运行：
    遍历运行区所有服务 → 读 state/config.yaml
      · runtime != true  → 跳过（本就没在跑）
      · runtime == true  → 校验 pids 里的进程是否真的存活
           全部已死 → 把 pids/processes 清空、runtime 置 false（并记录日志）
           部分存活 → 仅保留存活项，runtime 维持 true

与 process_info 的区别：
    process_info  → 采集数据 + 顺带清理（主链路）
    runtime_cleaner → 只做状态纠偏（兜底，不采集数据）

运行区结构：
    {server.apps}/{服务名}/
        ├── state/config.yaml     运行状态（pids / processes / name / runtime / version）
        ├── current -> {download}/{服务名}/{版本}
        ├── app/bin/config -> current/xxx
        └── runtime/              服务运行日志等产物
"""

import logging
import os
from typing import Any, Dict, List, Tuple

from utils.app_path import (
    read_state,
    read_state_pids,
    filter_alive_pids,
    refresh_state_pids,
    get_process_names,
)
from utils.config_loader import load_config

_CONFIG = load_config()
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

# 显控台 / 插件应用的子目录名（运行区下）
_XKT_SERVICE_ROOT = "displayConsole"
_PLUGIN_SERVICE_ROOT = "plugin"


def _iter_services() -> List[Tuple[str, str]]:
    """
    收集运行区下的所有服务（服务名 + 所属类别层）。

    扫描 {apps}/、{apps}/displayConsole/、{apps}/plugin/ 三个位置，
    以「存在 state/config.yaml」为有效服务判据。

    返回:
        [(服务名, sub_dir), ...]
        —— sub_dir 是类别层（"" / displayConsole / plugin），
           读取/回写状态文件时必须带上，否则显控台/插件服务会读不到（历史缺陷）。
    """
    services: List[Tuple[str, str]] = []

    if not _APPS_BASE or not os.path.isdir(_APPS_BASE):
        return services

    scan_roots: List[Tuple[str, str]] = [(_APPS_BASE, "")]
    for sub_root in (_XKT_SERVICE_ROOT, _PLUGIN_SERVICE_ROOT):
        root = os.path.join(_APPS_BASE, sub_root)
        if os.path.isdir(root):
            scan_roots.append((root, sub_root))

    for root, sub_dir in scan_roots:
        try:
            entries = os.listdir(root)
        except OSError as e:
            logging.warning("[RuntimeCleaner] 读取目录失败: %s -> %s", root, e)
            continue

        for entry in entries:
            service_dir = os.path.join(root, entry)
            # 跳过软链接（current/app/bin/config）与普通文件
            if os.path.islink(service_dir) or not os.path.isdir(service_dir):
                continue
            # 有效服务判据：存在 state/config.yaml
            if not os.path.isfile(os.path.join(service_dir, "state", "config.yaml")):
                continue
            if (entry, sub_dir) not in services:
                services.append((entry, sub_dir))

    return services


def clean_dead_runtimes() -> Dict[str, Any]:
    """
    兜底校验：检查所有 runtime=true 的服务，进程是否真的还活着。

    对所有标记为运行中、但进程实际已全部退出的服务，
    把状态文件纠正为 pids=[] / processes=[] / runtime=false。

    本函数不采集进程数据（那是 process_info 的职责），
    仅在 process_info 未能正常工作时兜底纠偏。

    返回:
        {
            "success":    是否无错误,
            "checked":    检查的服务数（runtime=true 的）,
            "corrected":  纠正的服务数,
            "skipped":    跳过的服务数（runtime != true）,
            "errors":     [...]
        }
    """
    checked, corrected, skipped = 0, 0, 0
    errors: List[str] = []

    if not _APPS_BASE or not os.path.isdir(_APPS_BASE):
        logging.warning("[RuntimeCleaner] 运行区目录不存在: %s", _APPS_BASE)
        return {"success": False, "checked": 0, "corrected": 0,
                "skipped": 0, "errors": ["运行区目录不存在"]}

    for service_name, sub_dir in _iter_services():
        try:
            state = read_state(service_name, sub_dir)
            if not state:
                continue

            # 未标记运行中的 → 不在本模块职责内
            if not state.get("runtime"):
                skipped += 1
                continue

            checked += 1
            pid_list = read_state_pids(service_name, sub_dir)

            # runtime=true 但 pids 为空 → 状态自相矛盾，直接纠正
            if not pid_list:
                logging.warning(
                    "[RuntimeCleaner] 服务 %s 标记 runtime=true 但 pids 为空，纠正为未运行",
                    service_name)
                refresh_state_pids(service_name, [], [], sub_dir)
                corrected += 1
                continue

            alive = filter_alive_pids(pid_list, service_name)

            if len(alive) == len(pid_list):
                logging.debug("[RuntimeCleaner] 服务 %s 进程均存活: pids=%s",
                              service_name, alive)
                continue

            # 进程有变化 → 纠正状态（全死则清空 + runtime=false）
            logging.warning(
                "[RuntimeCleaner] 服务 %s 状态与实际不符: 记录 pids=%s, 实际存活=%s，执行纠正",
                service_name, pid_list, alive)
            refresh_state_pids(service_name, alive, get_process_names(alive), sub_dir)
            corrected += 1

        except Exception as e:
            errors.append(f"{service_name}: {e}")
            logging.warning("[RuntimeCleaner] 处理服务 %s 异常: %s", service_name, e)

    if corrected:
        logging.info("[RuntimeCleaner] 兜底纠正完成: 检查 %d 个, 纠正 %d 个",
                     checked, corrected)
    else:
        logging.debug("[RuntimeCleaner] 兜底检查完成: 检查 %d 个, 无异常",
                      checked)

    return {
        "success": len(errors) == 0,
        "checked": checked,
        "corrected": corrected,
        "skipped": skipped,
        "errors": errors,
    }


# ── 兼容旧调用名 ──

def clean_runtimes() -> Dict[str, Any]:
    """兼容旧入口名，等价于 clean_dead_runtimes()"""
    return clean_dead_runtimes()


# ── 自测入口 ──

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = clean_dead_runtimes()
    print("\n" + "=" * 60)
    print("  运行状态兜底检查结果")
    print("=" * 60)
    print(f"  检查服务数: {result.get('checked')}")
    print(f"  纠正服务数: {result.get('corrected')}")
    print(f"  跳过服务数: {result.get('skipped')}")
    print(f"  是否成功  : {result.get('success')}")
    if result.get("errors"):
        print("  错误:")
        for e in result["errors"]:
            print(f"    - {e}")
    print("=" * 60)
