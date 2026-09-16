"""
插件应用升级任务模块

与 upgrade.py 结构对齐，路径多一层 plugin/，并复用插件专属处理器：
  1. 停止旧版本: plugin_stop_task（执行 {version}/bin/stop.sh）
  2. 下载新版本: plugin_download_task（version 由参数显式传入）
  3. 安装新版本: plugin_install_task（执行 {version}/bin/install.sh）
  4. 启动新版本: plugin_start_task（执行 {version}/bin/start.sh）
  5. 任一步失败自动回滚：删除新版本目录 + 恢复 version 文件 + 重启旧版本

路径结构: download/plugin/{service_name}/{version}/bin/
版本管理: download/plugin/{service_name}/version 文件
"""

import logging
import os
import shutil
import traceback
from typing import Any, Dict, Optional

from utils.config_loader import load_config

from core.task_utils.plugin_download import plugin_download_task
from core.task_utils.plugin_install import plugin_install_task
from core.task_utils.plugin_start import plugin_start_task
from core.task_utils.plugin_stop import plugin_stop_task

# ── 全局配置 ──
_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")


# =============================================================================
# 结果构建
# =============================================================================

def _build_result(
    task_id: str,
    success: bool,
    message: str,
    data: Optional[Dict[str, Any]] = None,
    error_type: str = "",
    error_message: str = "",
    tb: str = "",
) -> Dict[str, Any]:
    return {
        "success": success,
        "task_id": task_id,
        "task_type": "plugin_upgrade",
        "status": 3 if success else 4,
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "PluginUpgradeTaskError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


# =============================================================================
# 版本文件读写
# =============================================================================

def _read_version(component_dir: str) -> str:
    """读取 {component_dir}/version 文件，不存在或为空返回空字符串"""
    version_file = os.path.join(component_dir, "version")
    if not os.path.isfile(version_file):
        return ""
    try:
        with open(version_file, "r", encoding="utf-8") as vf:
            return vf.read().strip()
    except Exception as e:
        logging.error("[plugin_upgrade] 读取 version 失败: %s -> %s", version_file, e)
        return ""


def _write_version(component_dir: str, version: str) -> bool:
    """写入 {component_dir}/version 文件，成功返回 True"""
    version_file = os.path.join(component_dir, "version")
    try:
        with open(version_file, "w", encoding="utf-8") as vf:
            vf.write(version)
        logging.info("[plugin_upgrade] 已写入 version 文件: %s -> %s", version_file, version)
        return True
    except Exception as e:
        logging.error("[plugin_upgrade] 写入 version 失败: %s -> %s", version_file, e)
        return False


# =============================================================================
# 回滚
# =============================================================================

def _rollback(
    task_id: str,
    service_name: str,
    sub_dir: str,
    component_dir: str,
    old_version: str,
    new_version: str,
    reason: str,
) -> Dict[str, Any]:
    """
    升级失败回滚：删除新版本目录 → 恢复 version 文件 → 重启旧版本。

    返回:
        回滚成功返回 None 语义的占位（由调用方决定最终结果），
        回滚失败返回错误结果字典。
    """
    logging.warning("[plugin_upgrade] 开始回滚: %s", reason)

    # 1. 删除新版本目录
    new_version_dir = os.path.join(component_dir, new_version)
    if os.path.isdir(new_version_dir):
        try:
            shutil.rmtree(new_version_dir)
            logging.info("[plugin_upgrade] 已删除新版本目录: %s", new_version_dir)
        except Exception as e:
            logging.error("[plugin_upgrade] 删除新版本目录失败: %s -> %s", new_version_dir, e)

    # 2. 恢复 version 文件
    if old_version:
        if not _write_version(component_dir, old_version):
            return _build_result(
                task_id, False,
                f"升级失败({reason})，且回滚时恢复 version 文件失败",
                data={"service_name": service_name, "old_version": old_version,
                      "new_version": new_version, "rollback": "failed"},
                error_type="RollbackVersionWriteError",
                error_message="回滚时无法恢复 version 文件",
            )

    # 3. 重启旧版本
    start_result = plugin_start_task({
        "task_id": task_id,
        "service_name": service_name,
        "sub_dir": sub_dir,
    })
    if not start_result.get("success"):
        return _build_result(
            task_id, False,
            f"升级失败({reason})，且回滚后启动旧版本失败: {start_result.get('message', '')}",
            data={
                "service_name": service_name,
                "old_version": old_version,
                "new_version": new_version,
                "rollback": "start_failed",
                "rollback_start_result": start_result,
            },
            error_type="RollbackStartError",
            error_message=start_result.get("message", ""),
        )

    return _build_result(
        task_id, False,
        f"升级失败({reason})，已回滚到旧版本 {old_version}",
        data={
            "service_name": service_name,
            "old_version": old_version,
            "new_version": new_version,
            "rollback": "success",
            "rollback_start_result": start_result,
        },
        error_type="UpgradeFailedRolledBack",
        error_message=reason,
    )


# =============================================================================
# 主入口
# =============================================================================

def plugin_upgrade_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    插件应用升级任务 — 停止旧版 → 下载新版 → 安装新版 → 启动新版，失败自动回滚。

    参数:
        - task_id:       任务ID（必填）
        - service_name:  服务名称（必填），即 plugin 下的目录名
        - download_url:  新版本下载地址（必填）
        - file_name:     文件名（必填），与下载时的 file_name 一致
        - suffix:        文件后缀（必填），如 .zip（兼容 file_suffix）
        - version:       新版本号（必填）
        - sub_dir:       专属子目录（默认 plugin）

    流程:
        1. 读取 {component_dir}/version 获取当前版本
        2. 版本比对，相同则直接返回"已是最新版本"
        3. 停止旧版本（plugin_stop_task）
        4. 下载新版本（plugin_download_task）
        5. 安装新版本（plugin_install_task）
        6. 启动新版本（plugin_start_task）
        7. 任一步失败自动回滚（删新目录 + 恢复 version + 重启旧版）

    返回:
        dict: 任务执行结果
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()
    download_url = str(parameters.get("download_url", "") or "").strip()
    file_name = str(parameters.get("file_name", "") or "").strip()
    # 兼容旧字段名 suffix（后端 xktUpgrade 下发该字段），新流程统一为 file_suffix
    file_suffix = str(parameters.get("file_suffix", "") or parameters.get("suffix", "") or "").strip()
    version = str(parameters.get("version", "") or "").strip()
    sub_dir = str(parameters.get("sub_dir", "") or "plugin").strip()

    # ── 参数校验 ──
    if not task_id:
        return _build_result("", False, "参数缺失: task_id",
                             error_type="ParameterMissing", error_message="task_id 缺失")
    if not service_name:
        return _build_result(task_id, False, "参数缺失: service_name",
                             error_type="ParameterMissing", error_message="service_name 缺失")
    if not download_url:
        return _build_result(task_id, False, "参数缺失: download_url",
                             error_type="ParameterMissing", error_message="download_url 缺失")
    if not file_name:
        return _build_result(task_id, False, "参数缺失: file_name",
                             error_type="ParameterMissing", error_message="file_name 缺失")
    if not file_suffix:
        return _build_result(task_id, False, "参数缺失: suffix/file_suffix",
                             error_type="ParameterMissing", error_message="suffix/file_suffix 缺失")
    if not version:
        return _build_result(task_id, False, "参数缺失: version",
                             error_type="ParameterMissing", error_message="version 缺失")

    if not _DOWNLOAD_BASE:
        return _build_result(task_id, False, "config.yaml 中未配置 server.download",
                             error_type="ConfigMissing", error_message="server.download 未配置")

    component_dir = os.path.join(_DOWNLOAD_BASE, sub_dir, service_name)
    if not os.path.isdir(component_dir):
        return _build_result(
            task_id, False, f"组件目录不存在: {component_dir}",
            data={"service_name": service_name, "component_dir": component_dir},
            error_type="FileNotFoundError",
            error_message=f"{sub_dir}/{service_name} 目录不存在，请先下载",
        )

    # ── 读取当前版本 ──
    old_version = _read_version(component_dir)
    if not old_version:
        return _build_result(
            task_id, False, f"version 文件不存在或为空: {component_dir}/version",
            data={"service_name": service_name, "component_dir": component_dir},
            error_type="VersionMissing",
            error_message="未找到 version 文件，请确认组件已下载",
        )

    logging.info("[plugin_upgrade] 组件目录: %s, 当前版本: %s, 目标版本: %s",
                 component_dir, old_version, version)

    # ── 版本比对 ──
    if old_version == version:
        return _build_result(
            task_id, False, f"当前已是最新版本 {version}，无需升级",
            data={"service_name": service_name, "old_version": old_version,
                  "new_version": version, "status": "already_latest"},
            error_type="AlreadyLatestVersion",
            error_message=f"当前版本 {old_version} 与目标版本 {version} 相同",
        )

    # ── 1. 停止旧版本 ──
    logging.info("[plugin_upgrade] 停止旧版本 %s", old_version)
    stop_result = plugin_stop_task({
        "task_id": task_id,
        "service_name": service_name,
        "sub_dir": sub_dir,
    })
    if not stop_result.get("success"):
        return _build_result(
            task_id, False,
            f"停止旧版本失败: {stop_result.get('message', '')}",
            data={
                "service_name": service_name,
                "old_version": old_version,
                "new_version": version,
                "step": "stop",
                "stop_result": stop_result,
            },
            error_type="StopOldVersionError",
            error_message=stop_result.get("message", ""),
        )

    # ── 2. 下载新版本 ──
    logging.info("[plugin_upgrade] 下载新版本 %s", version)
    download_result = plugin_download_task({
        "task_id": task_id,
        "download_url": download_url,
        "file_name": file_name,
        "file_suffix": file_suffix,
        "version": version,
        "sub_dir": sub_dir,
    })
    if not download_result.get("result"):
        return _rollback(
            task_id, service_name, sub_dir, component_dir,
            old_version, version,
            f"下载新版本失败: {download_result.get('message', '')}",
        )

    # ── 3. 安装新版本 ──
    logging.info("[plugin_upgrade] 安装新版本 %s", version)
    install_result = plugin_install_task({
        "task_id": task_id,
        "file_name": file_name,
        "sub_dir": sub_dir,
    })
    if not install_result.get("success"):
        return _rollback(
            task_id, service_name, sub_dir, component_dir,
            old_version, version,
            f"安装新版本失败: {install_result.get('message', '')}",
        )

    # ── 4. 启动新版本 ──
    logging.info("[plugin_upgrade] 启动新版本 %s", version)
    start_result = plugin_start_task({
        "task_id": task_id,
        "service_name": service_name,
        "sub_dir": sub_dir,
    })
    if not start_result.get("success"):
        return _rollback(
            task_id, service_name, sub_dir, component_dir,
            old_version, version,
            f"启动新版本失败: {start_result.get('message', '')}",
        )

    # ── 升级成功 ──
    logging.info("[plugin_upgrade] 升级成功: %s -> %s", old_version, version)
    return _build_result(
        task_id, True, f"升级成功: {old_version} -> {version}",
        data={
            "service_name": service_name,
            "old_version": old_version,
            "new_version": version,
            "status": "upgraded",
            "download_result": download_result,
            "install_result": install_result,
            "start_result": start_result,
        },
    )


# =============================================================================
# 自测入口
# =============================================================================

if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    test_params = {
        "task_id": "test-plugin-upgrade-001",
        "service_name": "test-service",
        "download_url": "https://example.com/download/test.zip",
        "file_name": "test-service",
        "suffix": ".zip",
        "version": "2.0.0",
        "sub_dir": "plugin",
    }
    result = plugin_upgrade_task(test_params)
    print(json.dumps(result, indent=2, ensure_ascii=False))