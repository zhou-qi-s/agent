"""
插件应用回滚任务模块

与 rollback.py 结构对齐，路径多一层 plugin/，并复用插件专属处理器：
  1. 停止当前版本: plugin_stop_task（执行 {version}/bin/stop.sh）
  2. 更新 version 文件为目标版本
  3. 启动目标版本: plugin_start_task（执行 {version}/bin/start.sh）
  4. 启动失败自动恢复 version 文件为原版本

路径结构: download/plugin/{service_name}/{version}/bin/
版本管理: download/plugin/{service_name}/version 文件
"""

import logging
import os
import traceback
from typing import Any, Dict, Optional

from utils.config_loader import load_config

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
        "task_type": "plugin_rollback",
        "status": 3 if success else 4,
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "PluginRollbackTaskError",
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
        logging.error("[plugin_rollback] 读取 version 失败: %s -> %s", version_file, e)
        return ""


def _write_version(component_dir: str, version: str) -> bool:
    """写入 {component_dir}/version 文件，成功返回 True"""
    version_file = os.path.join(component_dir, "version")
    try:
        with open(version_file, "w", encoding="utf-8") as vf:
            vf.write(version)
        logging.info("[plugin_rollback] 已写入 version 文件: %s -> %s", version_file, version)
        return True
    except Exception as e:
        logging.error("[plugin_rollback] 写入 version 失败: %s -> %s", version_file, e)
        return False


# =============================================================================
# 主入口
# =============================================================================

def plugin_rollback_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    插件应用回滚任务 — 停止当前版 → 更新 version 文件 → 启动目标版。

    参数:
        - task_id:       任务ID（必填）
        - service_name:  服务名称（必填），即 plugin 下的目录名
        - version:       目标回滚版本号（必填）
        - sub_dir:       专属子目录（默认 plugin）

    流程:
        1. 读取 {component_dir}/version 获取当前版本
        2. 版本比对，相同则直接返回"已是最新版本"
        3. 校验目标版本目录存在（{component_dir}/{version}/bin/start.sh）
        4. 停止当前版本（plugin_stop_task）
        5. 更新 version 文件为目标版本
        6. 启动目标版本（plugin_start_task）
        7. 启动失败自动恢复 version 文件为原版本

    返回:
        dict: 任务执行结果
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()
    target_version = str(parameters.get("version", "") or "").strip()
    sub_dir = str(parameters.get("sub_dir", "") or "plugin").strip()

    # ── 参数校验 ──
    if not task_id:
        return _build_result("", False, "参数缺失: task_id",
                             error_type="ParameterMissing", error_message="task_id 缺失")
    if not service_name:
        return _build_result(task_id, False, "参数缺失: service_name",
                             error_type="ParameterMissing", error_message="service_name 缺失")
    if not target_version:
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
    current_version = _read_version(component_dir)
    if not current_version:
        return _build_result(
            task_id, False, f"version 文件不存在或为空: {component_dir}/version",
            data={"service_name": service_name, "component_dir": component_dir},
            error_type="VersionMissing",
            error_message="未找到 version 文件，请确认组件已下载",
        )

    logging.info("[plugin_rollback] 组件目录: %s, 当前版本: %s, 目标版本: %s",
                 component_dir, current_version, target_version)

    # ── 版本比对 ──
    if current_version == target_version:
        return _build_result(
            task_id, False, f"当前已是目标版本 {target_version}，无需回滚",
            data={"service_name": service_name, "current_version": current_version,
                  "target_version": target_version, "status": "already_target"},
            error_type="AlreadyTargetVersion",
            error_message=f"当前版本 {current_version} 与目标版本 {target_version} 相同",
        )

    # ── 校验目标版本目录存在 ──
    target_version_dir = os.path.join(component_dir, target_version)
    target_start_script = os.path.join(target_version_dir, "bin", "start.sh")
    if not os.path.isdir(target_version_dir):
        return _build_result(
            task_id, False, f"目标版本目录不存在: {target_version_dir}",
            data={"service_name": service_name, "current_version": current_version,
                  "target_version": target_version, "target_version_dir": target_version_dir},
            error_type="FileNotFoundError",
            error_message=f"目标版本 {target_version} 目录不存在，无法回滚",
        )
    if not os.path.isfile(target_start_script):
        return _build_result(
            task_id, False, f"目标版本启动脚本不存在: {target_start_script}",
            data={"service_name": service_name, "current_version": current_version,
                  "target_version": target_version, "target_start_script": target_start_script},
            error_type="FileNotFoundError",
            error_message=f"目标版本 {target_version} 缺少 bin/start.sh，无法回滚",
        )

    # ── 1. 停止当前版本 ──
    logging.info("[plugin_rollback] 停止当前版本 %s", current_version)
    stop_result = plugin_stop_task({
        "task_id": task_id,
        "service_name": service_name,
        "sub_dir": sub_dir,
    })
    if not stop_result.get("success"):
        return _build_result(
            task_id, False,
            f"停止当前版本失败: {stop_result.get('message', '')}",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "target_version": target_version,
                "step": "stop",
                "stop_result": stop_result,
            },
            error_type="StopCurrentVersionError",
            error_message=stop_result.get("message", ""),
        )

    # ── 2. 更新 version 文件为目标版本 ──
    if not _write_version(component_dir, target_version):
        return _build_result(
            task_id, False, f"更新 version 文件失败: {component_dir}/version",
            data={"service_name": service_name, "current_version": current_version,
                  "target_version": target_version, "step": "write_version"},
            error_type="VersionWriteError",
            error_message="无法写入 version 文件",
        )

    # ── 3. 启动目标版本 ──
    logging.info("[plugin_rollback] 启动目标版本 %s", target_version)
    start_result = plugin_start_task({
        "task_id": task_id,
        "service_name": service_name,
        "sub_dir": sub_dir,
    })
    if not start_result.get("success"):
        # ── 启动失败，恢复 version 文件为原版本 ──
        logging.warning("[plugin_rollback] 启动目标版本失败，恢复 version 文件为 %s", current_version)
        _write_version(component_dir, current_version)
        return _build_result(
            task_id, False,
            f"启动目标版本失败: {start_result.get('message', '')}，已恢复 version 文件为 {current_version}",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "target_version": target_version,
                "step": "start",
                "version_restored": True,
                "start_result": start_result,
            },
            error_type="StartTargetVersionError",
            error_message=start_result.get("message", ""),
        )

    # ── 回滚成功 ──
    logging.info("[plugin_rollback] 回滚成功: %s -> %s", current_version, target_version)
    return _build_result(
        task_id, True, f"回滚成功: {current_version} -> {target_version}",
        data={
            "service_name": service_name,
            "current_version": current_version,
            "target_version": target_version,
            "status": "rolled_back",
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
        "task_id": "test-plugin-rollback-001",
        "service_name": "test-service",
        "version": "1.0.0",
        "sub_dir": "plugin",
    }
    result = plugin_rollback_task(test_params)
    print(json.dumps(result, indent=2, ensure_ascii=False))