"""
core/task_utils/xkt_start.py - 显控启动模块

通过 bin/ 目录下的启动脚本启动显控服务。
路径: download/displayConsole/{service_name}/{version}/bin/start.sh
"""

import logging
import os
import subprocess
import time as _time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.xkt.process_check import collect_service_pids
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
        "task_id": task_id,
        "task_type": "xkt_start",
        "status": 3 if success else 4,
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "StartTaskError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


# =============================================================================
# 路径解析
# =============================================================================

def _resolve_paths(service_name: str, sub_dir: str = "displayConsole") -> Tuple[Optional[Dict[str, Any]], str, str, str]:
    """
    解析 displayConsole 组件路径。

    路径结构: download/displayConsole/{service_name}/{version}/bin/

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
                error_message=f"{sub_dir}/{service_name} 目录不存在",
            ),
            "", "", "",
        )

    # 读取 version 文件
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
    os.makedirs(bin_dir, exist_ok=True)

    return None, component_dir, version, bin_dir


# =============================================================================
# 脚本读取 & 执行
# =============================================================================

def _read_script(bin_dir: str) -> Tuple[str, str]:
    """
    从 bin/ 目录读取启动脚本。

    返回:
        (script_content, script_path) — 文件不存在时返回 ("", "")
    """
    ext = ".bat" if os.name == "nt" else ".sh"
    script_path = os.path.join(bin_dir, f"start{ext}")

    if not os.path.isfile(script_path):
        return "", ""

    # 兼容多种编码：UTF-8 失败回退 GB18030（Windows 编辑器保存的中文注释常为 GBK/GB2312）
    content = None
    for enc in ("utf-8", "gb18030"):
        try:
            with open(script_path, "r", encoding=enc) as f:
                content = f.read()
            break
        except (UnicodeDecodeError, OSError):
            continue
    if content is None:
        try:
            with open(script_path, "rb") as f:
                content = f.read().decode("utf-8", errors="replace")
        except Exception as e:
            logging.error("[xkt_start] 读取启动脚本失败: %s, %s", script_path, e)
            return "", ""
    return content, script_path


def _execute_script(script_path: str, bin_dir: str, timeout: int) -> Dict[str, Any]:
    """
    执行启动脚本。

    返回字段: success, exit_code, stdout, stderr
    """
    try:
        if os.name == "nt":
            proc = subprocess.Popen(
                [script_path],
                cwd=bin_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=True,
            )
        else:
            proc = subprocess.Popen(
                ["bash", script_path],
                cwd=bin_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return {
                "success": False,
                "exit_code": -1,
                "stdout": stdout.strip() if stdout else "",
                "stderr": f"脚本执行超时 ({timeout}s)",
            }

        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout.strip() if stdout else "",
            "stderr": stderr.strip() if stderr else "",
        }
    except Exception as e:
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"脚本执行异常: {e}",
        }


# =============================================================================
# PID 回写（唯一落点：{version}/runtime/pid）
# =============================================================================

def _write_runtime_pid(pid_file: str, pid_list: List[int]) -> bool:
    """
    将 PID 写入 {version}/runtime/pid。

    参数:
        pid_file: runtime/pid 完整路径
        pid_list: PID 列表

    返回:
        是否写入成功
    """
    try:
        Path(os.path.dirname(pid_file)).mkdir(parents=True, exist_ok=True)
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write("\n".join(str(p) for p in pid_list))
        logging.info("[xkt_start] 已回写 PID 文件: %s, PIDs=%s", pid_file, pid_list)
        return True
    except Exception as e:
        logging.warning("[xkt_start] 回写 PID 文件失败: %s -> %s", pid_file, e)
        return False


# =============================================================================
# 主入口
# =============================================================================

def xkt_start_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    显控启动任务 — 从 bin/ 目录读取 start.sh(start.bat) 并执行。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），即 displayConsole 下的目录名

    流程:
        1. 解析 displayConsole/{service_name}/ 组件目录
        2. 读取 version 文件获取版本号
        3. 从 {version}/bin/ 读取启动脚本
        4. 执行启动脚本
        5. 读取 runtime/pid 中的主进程 PID
        6. 写入 download/xkt/{service_name} PID 文件

    返回:
        dict: 任务执行结果
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
        error["data"] = {**(error.get("data") or {}), "service_name": service_name}
        return error

    logging.info("[xkt_start] 组件目录: %s, 版本: %s, bin目录: %s", component_dir, version, bin_dir)

    # ── 读取启动脚本 ──
    script_content, script_path = _read_script(bin_dir)
    if not script_content:
        ext = ".bat" if os.name == "nt" else ".sh"
        return _build_result(
            task_id, False, f"启动脚本不存在: {bin_dir}/start{ext}",
            data={"service_name": service_name, "version": version, "bin_dir": bin_dir},
            error_type="ScriptNotFound",
            error_message=f"未找到启动脚本: {bin_dir}/start{ext}",
        )

    logging.info("[xkt_start] 启动脚本已读取: %s", script_path)

    # ── 执行启动脚本 ──
    base_data = {
        "service_name": service_name,
        "version": version,
        "component_dir": component_dir,
        "bin_dir": bin_dir,
        "script_path": script_path,
    }
    exec_timeout = min(timeout, 60)
    exec_result = _execute_script(script_path, bin_dir, exec_timeout)
    logging.info(
        "[xkt_start] 脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
        exec_result["exit_code"], exec_result.get("stdout", ""), exec_result.get("stderr", ""),
    )

    if not exec_result["success"]:
        return _build_result(
            task_id, False, f"启动脚本执行失败 (exit_code={exec_result['exit_code']})",
            data={
                **base_data,
                "exit_code": exec_result["exit_code"],
                "stdout": exec_result.get("stdout", ""),
                "stderr": exec_result.get("stderr", ""),
            },
            error_type="ScriptExecutionError",
            error_message=exec_result.get("stderr", "") or f"脚本退出码: {exec_result['exit_code']}",
        )

    # ── 读取 runtime/pid ──
    runtime_dir = os.path.join(component_dir, version, "runtime")
    pid_file = os.path.join(runtime_dir, "pid")
    pids: List[int] = []

    if os.path.isfile(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as pf:
                pid_str = pf.read().strip()
            for line in pid_str.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
            logging.info("[xkt_start] runtime/pid 中的 PID: %s", pids)
        except Exception as e:
            logging.warning("[xkt_start] 读取 PID 文件失败: %s", e)

    # ── 刷新 runtime/pid（启动/升级后必须是新进程的 PID）──
    # 优先采用"实际在跑的进程"扫描结果：runtime/pid 可能是脚本尚未刷新，
    # 或上一版本残留的旧值，直接沿用会把旧 PID 留下
    version_dir = os.path.join(component_dir, version)
    scanned_pids = collect_service_pids(component_dir, version_dir)
    pids = scanned_pids or pids

    pid_written = False
    if pids:
        pid_written = _write_runtime_pid(pid_file, pids)
    else:
        logging.warning("[xkt_start] 未获取到存活进程 PID, 保留 runtime/pid 原内容待巡检修正")

    return _build_result(task_id, True, "显控启动成功", data={
        **base_data,
        "status": "started",
        "pids": pids,
        "scanned_pids": scanned_pids,
        "pid_file": pid_file,
        "pid_written": pid_written,
        "exit_code": exec_result["exit_code"],
        "stdout": exec_result.get("stdout", ""),
    })


# =============================================================================
# 自测入口
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    test_params = {
        "task_id": "test-xkt-start-001",
        "service_name": "test-service",
    }
    result = xkt_start_task(test_params)
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False))
