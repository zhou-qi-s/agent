"""
Runtime 清理模块

遍历 download 下所有有 version 的文件夹，进入 app/ 遍历所有版本文件夹，
检查 runtime/pid 对应的进程是否存活，进程不存在则清空 runtime 文件夹。
"""

import logging
import os
import shutil
from typing import Any, Dict, List

from core.process.process_info import _process_exists
from utils.config_loader import load_config

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")


def _clear_runtime(runtime_dir: str) -> None:
    """删除并重建 runtime 目录"""
    shutil.rmtree(runtime_dir)
    os.makedirs(runtime_dir, exist_ok=True)


def clean_dead_runtimes() -> Dict[str, Any]:
    """
    遍历 download 下所有有 version 的文件夹，进入 app/ 遍历所有版本文件夹，
    检查 runtime/pid 对应的进程是否存活。进程不存在则清空 runtime 文件夹。

    返回:
        {"success": true, "checked": 5, "cleaned": 2, "errors": []}
    """
    checked, cleaned = 0, 0
    errors: List[str] = []

    if not _DOWNLOAD_BASE or not os.path.isdir(_DOWNLOAD_BASE):
        logging.warning("[RuntimeCleaner] download 目录不存在: %s", _DOWNLOAD_BASE)
        return {"success": False, "checked": 0, "cleaned": 0, "errors": ["download 目录不存在"]}

    for entry in os.listdir(_DOWNLOAD_BASE):
        service_dir = os.path.join(_DOWNLOAD_BASE, entry)
        if not os.path.isdir(service_dir):
            continue

        # 必须有 version 文件才视为有效服务目录
        version_file = os.path.join(service_dir, "version")
        if not os.path.isfile(version_file):
            continue

        # 进入 app/ 遍历所有版本文件夹
        app_dir = os.path.join(service_dir, "app")
        if not os.path.isdir(app_dir):
            continue

        for version_name in os.listdir(app_dir):
            version_dir = os.path.join(app_dir, version_name)
            if not os.path.isdir(version_dir):
                continue

            runtime_dir = os.path.join(version_dir, "runtime")
            if not os.path.isdir(runtime_dir):
                continue

            pid_file = os.path.join(runtime_dir, "pid")
            checked += 1

            should_clean = False
            reason = ""

            # ── 没有 pid 文件 → 清理 ──
            if not os.path.isfile(pid_file):
                should_clean = True
                reason = "无 pid 文件"
            else:
                # ── 读 pid 文件 ──
                try:
                    with open(pid_file, "r", encoding="utf-8") as pf:
                        pid_str = pf.read().strip()
                except Exception as e:
                    logging.warning("[RuntimeCleaner] 读取 pid 失败: %s -> %s", pid_file, e)
                    errors.append(f"{entry}/{version_name}: 读取 pid 失败: {e}")
                    continue

                if not pid_str:
                    should_clean = True
                    reason = "pid 文件为空"
                else:
                    # 支持多行 PID，只要有一个存活就不清理
                    any_alive = False
                    alive_pids = []
                    for line in pid_str.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            pid_int = int(line)
                        except ValueError:
                            continue
                        if _process_exists(pid_int):
                            any_alive = True
                            alive_pids.append(str(pid_int))
                    if any_alive:
                        logging.debug(
                            "[RuntimeCleaner] 进程存活，保留 runtime: folder=%s, version=%s, pid=%s",
                            entry, version_name, ",".join(alive_pids))
                    else:
                        should_clean = True
                        reason = f"所有进程已退出"

            if should_clean:
                logging.info("[RuntimeCleaner] %s，清空 runtime: folder=%s, version=%s",
                             reason, entry, version_name)
                try:
                    _clear_runtime(runtime_dir)
                    cleaned += 1
                    logging.info("[RuntimeCleaner] runtime 已清空: folder=%s, version=%s",
                                 entry, version_name)
                except Exception as e:
                    errors.append(f"{entry}/{version_name}: {e}")
                    logging.warning("[RuntimeCleaner] 清空 runtime 失败: %s/%s -> %s",
                                    entry, version_name, e)

    return {
        "success": len(errors) == 0,
        "checked": checked,
        "cleaned": cleaned,
        "errors": errors,
    }
