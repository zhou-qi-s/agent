"""
xkt 显控卸载任务模块

与 uninstall.py 业务逻辑对齐，唯一区别：路径多一层 displayConsole/
即组件目录为 {server.download}/displayConsole/{service_name}/{version}/bin/uninstall.sh
"""

import logging
import os
import subprocess
from typing import Any, Dict, Optional

from utils.config_loader import load_config

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
        # 顶层状态码（平台约定）: 10=UNINSTALLED 已卸载 / 11=UNINSTALL_FAILED 卸载失败
        "status": 10 if success else 11,
        "task_id": task_id,
        "task_type": "xkt_uninstall",
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "XktUninstallTaskError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


def _resolve_paths(service_name: str, sub_dir: str = "displayConsole") -> tuple:
    """
    根据 service_name 解析显控组件路径。

    返回:
        (error, component_dir, version, bin_dir)
        - 出错: (error_result, "", "", "")
        - 正常: (None, component_dir, version, bin_dir)
    """
    if not _DOWNLOAD_BASE:
        return (
            _build_result(
                "", False, "config.yaml 中未配置 server.download",
                error_type="ConfigMissing",
                error_message="server.download 未配置",
            ),
            "", "", "",
        )

    component_dir = os.path.join(_DOWNLOAD_BASE, sub_dir, service_name)
    if not os.path.isdir(component_dir):
        return (
            _build_result(
                "", False, f"组件目录不存在: {component_dir}",
                error_type="FileNotFoundError",
                error_message=f"组件目录不存在: {component_dir}",
            ),
            "", "", "",
        )

    # 读取 version
    version_file = os.path.join(component_dir, "version")
    if not os.path.isfile(version_file):
        return (
            _build_result(
                "", False, f"version 文件不存在: {version_file}",
                error_type="FileNotFoundError",
                error_message="未找到 version 文件，请确认组件已下载",
            ),
            "", "", "",
        )
    try:
        with open(version_file, "r", encoding="utf-8") as vf:
            version = vf.read().strip()
    except Exception as e:
        return (
            _build_result("", False, f"读取 version 失败: {e}",
                          error_type="VersionReadError", error_message=str(e)),
            "", "", "",
        )
    if not version:
        return (
            _build_result("", False, "version 文件为空",
                          error_type="VersionEmpty", error_message="version 文件为空"),
            "", "", "",
        )

    # bin 目录: {component_dir}/{version}/bin/
    bin_dir = os.path.join(component_dir, version, "bin")
    if not os.path.isdir(bin_dir):
        return (
            _build_result(
                "", False, f"bin 目录不存在: {bin_dir}",
                error_type="FileNotFoundError",
                error_message=f"bin 目录不存在: {bin_dir}",
            ),
            "", "", "",
        )

    return None, component_dir, version, bin_dir


def _execute_script(script_path: str, bin_dir: str, timeout: int) -> Dict[str, Any]:
    """执行脚本，返回执行结果。

    返回字段: success, exit_code, stdout, stderr
    """
    try:
        if os.name == "nt":
            proc = subprocess.run(
                [script_path],
                cwd=bin_dir,
                timeout=timeout,
                capture_output=True,
                text=True,
                shell=True,
            )
        else:
            proc = subprocess.run(
                ["bash", script_path],
                cwd=bin_dir,
                timeout=timeout,
                capture_output=True,
                text=True,
            )

        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"脚本执行超时 ({timeout}s)",
        }
    except Exception as e:
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"脚本执行异常: {e}",
        }


# =============================================================================
# 主入口
# =============================================================================

def xkt_uninstall_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    显控卸载任务（执行 bin/uninstall.sh 脚本）。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），同时也是 displayConsole 下的组件目录名

    流程:
        1. 根据 service_name 定位 displayConsole 组件目录，读取 version
        2. 检查 {version}/runtime/pid 是否存在
           - 存在 → 返回失败，提示先停止服务
           - 不存在 → 执行 bin/uninstall.sh 卸载脚本
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()
    sub_dir = str(parameters.get("sub_dir", "") or "displayConsole").strip()

    # ── 参数校验 ──
    if not task_id:
        return _build_result("", False, "参数缺失: task_id",
                             error_type="ParameterMissing", error_message="task_id 缺失")
    if not service_name:
        return _build_result(task_id, False, "参数缺失: service_name",
                             error_type="ParameterMissing", error_message="service_name 缺失")

    # ── 路径解析 ──
    error, component_dir, version, bin_dir = _resolve_paths(service_name, sub_dir)
    if error:
        error["task_id"] = task_id
        error["data"] = {
            **(error.get("data") or {}),
            "service_name": service_name,
        }
        return error

    logging.info("[xkt_uninstall_task] 组件目录: %s, 版本: %s, bin目录: %s", component_dir, version, bin_dir)

    # ── 检查 runtime/pid 是否存在 ──
    runtime_dir = os.path.join(component_dir, version, "runtime")
    pid_file = os.path.join(runtime_dir, "pid")

    base_data = {
        "service_name": service_name,
        "version": version,
        "component_dir": component_dir,
        "bin_dir": bin_dir,
    }

    if os.path.isfile(pid_file):
        pid_value = ""
        try:
            with open(pid_file, "r", encoding="utf-8") as pf:
                pid_value = pf.read().strip()
        except Exception:
            pass
        return _build_result(
            task_id, False, f"服务仍在运行 (PID: {pid_value})，请先执行停止任务",
            data={
                **base_data,
                "status": "uninstall_blocked",
                "pid": pid_value,
                "pid_file_exists": True,
            },
            error_type="ServiceRunning",
            error_message=f"PID 文件存在 ({pid_value})，服务可能仍在运行，请先停止",
        )

    # ── 定位 uninstall.sh 脚本 ──
    ext = ".bat" if os.name == "nt" else ".sh"
    script_path = os.path.join(bin_dir, f"uninstall{ext}")
    if not os.path.isfile(script_path):
        return _build_result(
            task_id, False, f"卸载脚本不存在: {script_path}",
            data=base_data,
            error_type="FileNotFoundError",
            error_message=f"卸载脚本不存在: {script_path}",
        )

    # ── 执行卸载脚本 ──
    exec_timeout = min(timeout, 60)
    exec_result = _execute_script(script_path, bin_dir, exec_timeout)
    logging.info(
        "[xkt_uninstall_task] 脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
        exec_result["exit_code"], exec_result.get("stdout", ""), exec_result.get("stderr", ""),
    )

    if not exec_result["success"]:
        return _build_result(
            task_id, False, f"卸载脚本执行失败 (exit_code={exec_result['exit_code']})",
            data={
                **base_data,
                "exit_code": exec_result["exit_code"],
                "stdout": exec_result.get("stdout", ""),
                "stderr": exec_result.get("stderr", ""),
            },
            error_type="ScriptExecutionError",
            error_message=exec_result.get("stderr", "") or f"脚本退出码: {exec_result['exit_code']}",
        )

    return _build_result(task_id, True, "卸载完成", data={
        **base_data,
        "status": "uninstalled",
        "exit_code": exec_result["exit_code"],
    })


# ── 自测入口 ──

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = xkt_uninstall_task(
        {
            "task_id": "test-xkt-uninstall-001",
            "service_name": "nginx-ruoyi",
        },
    )

    print("\n" + "=" * 60)
    print("  显控卸载任务结果")
    print("=" * 60)
    print(f"  task_id : {result.get('task_id', '')}")
    print(f"  成功    : {result['success']}")
    print(f"  消息    : {result['message']}")
    data = result.get("data", {})
    if data:
        for key in ("service_name", "version", "status", "exit_code", "bin_dir", "script_path"):
            print(f"  {key}: {data.get(key, 'N/A')}")
    if not result["success"]:
        err = result.get("error", {})
        print(f"  错误类型: {err.get('error_type', '')}")
        print(f"  错误信息: {err.get('error_message', '')}")
    print("=" * 60)
