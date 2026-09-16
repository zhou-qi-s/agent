"""
core/task_utils/xkt_rollback.py - 显控回滚模块

模型 A（与 xkt_download / xkt_stop / xkt_start / xkt_Install / xkt_uninstall 一致）：
    {download}/displayConsole/{service_name}/
    ├── version                     # 当前版本号文件
    └── {version}/
        ├── bin/{start,stop,install,uninstall}.sh
        └── runtime/pid

回滚流程 = 停止当前版本 + 切换 version 文件 + 启动目标版本：
    1. 读 version 文件获取当前版本
    2. 校验目标版本目录 {service_dir}/{target_version}/bin/start.sh 存在
    3. 调用 xkt_stop_task 停止当前版本（读 version 文件定位 bin/stop.sh）
    4. 更新 version 文件为目标版本
    5. 调用 xkt_start_task 启动目标版本（读 version 文件定位目标版本 bin/start.sh）
    启动失败 → 恢复 version 文件为当前版本并重启旧版本。
"""

import logging
import os
import traceback
from typing import Any, Dict, Optional

from utils import util
from utils.config_loader import load_config
from core.task_utils.xkt_stop import xkt_stop_task
from core.task_utils.xkt_start import xkt_start_task

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")


# =============================================================================
# 工具函数
# =============================================================================

def _build_result(success: bool, message: str, data: Optional[Dict[str, Any]] = None,
                  error_type: str = "", error_message: str = "", tb: str = "") -> Dict[str, Any]:
    """构建任务结果"""
    return {
        "ip": util.get_ip() or "unknown",
        "task_id": "",
        "result": success,
        "status": 3 if success else 13,
        "task_type": "xkt_rollback",
        "message": message,
        "data": {
            **(data or {}),
            "error_type": error_type,
            "error_message": error_message,
            "traceback": tb,
        },
    }


def _task_ok(result: Dict[str, Any]) -> bool:
    """兼容子任务 success / result 两种返回格式"""
    return bool(result.get("success") or result.get("result"))


def _read_current_version(component_dir: str) -> str:
    """读取组件目录下的 version 文件"""
    version_file = os.path.join(component_dir, "version")
    if not os.path.isfile(version_file):
        return ""
    try:
        with open(version_file, "r", encoding="utf-8") as vf:
            return vf.read().strip()
    except Exception:
        return ""


# =============================================================================
# 主方法
# =============================================================================

def xkt_rollback_task(parameters: Dict[str, Any], retry: int = 2, timeout: int = 300) -> Dict[str, Any]:
    """
    显控台回滚任务（模型 A）

    流程: 解析参数 → 校验(3个必填) → 组件目录存在性 → 读 version → 版本比对
    → 校验目标版本目录与启动脚本 → 停止当前版本(xkt_stop) → 切换 version 文件
    → 启动目标版本(xkt_start) → 启动失败恢复原版本

    参数:
        - task_id:        任务ID（必填）
        - service_name:   服务名称（必填），同时也是组件目录名
        - version:        回滚目标版本号（必填，兼容字段名 target_version）
        - sub_dir:        专属子目录（可选，默认 displayConsole）
    """
    task_id = str(parameters.get('task_id', '') or '').strip()

    def build_result(success: bool, message: str, data: Optional[Dict[str, Any]] = None,
                     error_type: str = "", error_message: str = "", tb: str = "") -> Dict[str, Any]:
        result = _build_result(success, message, data, error_type, error_message, tb)
        result['task_id'] = task_id
        return result

    # ── 1. 解析参数 ──
    service_name = str(parameters.get('service_name', '') or '').strip()
    target_version = str(parameters.get('version', '') or parameters.get('target_version', '') or '').strip()
    sub_dir = str(parameters.get('sub_dir', '') or 'displayConsole').strip()

    # ── 2. 参数校验（3 个必填）──
    missing = [k for k, v in {
        'task_id': task_id, 'service_name': service_name, 'version': target_version,
    }.items() if not v]
    if missing:
        logging.error("[显控回滚] 参数缺失: %s", ", ".join(missing))
        return build_result(False, f"参数缺失: {', '.join(missing)}",
                            error_type="ParameterMissing",
                            error_message=f"缺少必填参数: {', '.join(missing)}")

    # ── 3. 组件目录 ──
    if not _DOWNLOAD_BASE:
        return build_result(False, "config.yaml 中未配置 server.download",
                            error_type="ConfigMissing", error_message="server.download 未配置")
    component_dir = os.path.join(_DOWNLOAD_BASE, sub_dir, service_name)
    if not os.path.isdir(component_dir):
        logging.error("[显控回滚] %s 组件目录不存在: %s", sub_dir, component_dir)
        return build_result(False, f"服务目录不存在: {component_dir}",
                            error_type="FileNotFoundError",
                            error_message=f"{sub_dir} 组件目录不存在: {component_dir}")

    # ── 4. 读当前版本 ──
    current_version = _read_current_version(component_dir)
    if not current_version:
        return build_result(False, "无法读取当前版本号(version 文件缺失或为空)",
                            error_type="VersionEmpty", error_message="version 文件缺失或为空")

    # ── 5. 版本比对 ──
    if current_version == target_version:
        return build_result(False, f"当前已是目标版本 {target_version}，无需回滚",
                            error_type="VersionConflict", error_message="当前版本与目标版本相同")

    # ── 6. 校验目标版本 ──
    target_dir = os.path.join(component_dir, target_version)
    if not os.path.isdir(target_dir):
        return build_result(False, f"目标版本目录不存在: {target_dir}",
                            error_type="VersionNotFound",
                            error_message=f"目标版本 {target_version} 目录不存在")
    ext = ".bat" if os.name == "nt" else ".sh"
    start_script = os.path.join(target_dir, "bin", f"start{ext}")
    if not os.path.isfile(start_script):
        return build_result(False, f"目标版本缺少启动脚本: {start_script}",
                            error_type="FileNotFoundError",
                            error_message=f"目标版本缺少启动脚本: {start_script}")

    # ── 7. 停止当前版本 ──
    logging.info("[显控回滚] 停止当前版本 %s", current_version)
    stop_result = xkt_stop_task({"task_id": task_id, "service_name": service_name, "sub_dir": sub_dir})
    if not _task_ok(stop_result):
        logging.error("[显控回滚] 停止当前版本失败: %s", stop_result.get("message", ""))
        return build_result(False, f"停止当前版本失败: {stop_result.get('message', '')}",
                            error_type="StopError",
                            error_message=str(stop_result.get("error", {}).get("error_message", ""))
                            or stop_result.get("message", ""), tb=traceback.format_exc())

    # ── 8. 切换 version 文件为目标版本 ──
    version_file = os.path.join(component_dir, "version")
    try:
        with open(version_file, "w", encoding="utf-8") as vf:
            vf.write(target_version)
        logging.info("[显控回滚] version 已切换: %s → %s", current_version, target_version)
    except Exception as e:
        logging.error("[显控回滚] 更新 version 文件失败: %s", e)
        return build_result(False, f"更新版本号失败: {e}",
                            error_type="VersionWriteError", error_message=str(e))

    # ── 9. 启动目标版本 ──
    logging.info("[显控回滚] 启动目标版本 %s", target_version)
    start_result = xkt_start_task({"task_id": task_id, "service_name": service_name, "sub_dir": sub_dir})
    if not _task_ok(start_result):
        # 恢复 version 文件并重启旧版本
        try:
            with open(version_file, "w", encoding="utf-8") as vf:
                vf.write(current_version)
            xkt_start_task({"task_id": task_id, "service_name": service_name, "sub_dir": sub_dir})
            logging.info("[显控回滚] 已恢复版本 %s 并尝试重启", current_version)
        except Exception as e:
            logging.warning("[显控回滚] 恢复旧版本出错: %s", e)
        logging.error("[显控回滚] 启动目标版本失败: %s", start_result.get("message", ""))
        return build_result(False, f"回滚启动失败，已恢复原版本 {current_version}: {start_result.get('message', '')}",
                            error_type="StartError",
                            error_message=str(start_result.get("error", {}).get("error_message", ""))
                            or start_result.get("message", ""), tb=traceback.format_exc())

    # ── 10. 成功 ──
    logging.info("[显控回滚] 完成: %s %s → %s", service_name, current_version, target_version)
    return build_result(True, f"显控回滚完成: {service_name} {current_version} → {target_version}", data={
        "service_name": service_name,
        "old_version": current_version,
        "new_version": target_version,
        "sub_dir": sub_dir,
        "component_dir": component_dir,
    })


# ── 自测入口 ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    result = xkt_rollback_task({
        "task_id": "test-xkt-rollback-001",
        "service_name": "test-service",
        "version": "v1",
    })
    print("\n" + "=" * 60)
    print("  显控回滚任务结果")
    print("=" * 60)
    print(f"  task_id : {result.get('task_id', '')}")
    print(f"  成功    : {result['result']}")
    print(f"  消息    : {result['message']}")
    print("=" * 60)
