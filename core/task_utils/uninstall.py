"""
卸载服务任务模块

在「运行区」定位组件并执行其 bin/uninstall.sh 卸载脚本。

缓存区/运行区分离后：
    · 组件定位 → {server.apps}/{service_name}/（install 建立的软链接结构）
    · 版本号   → state/config.yaml 的 version 字段（原为 {component}/version 文件）
    · 运行检查 → 已运行则拒绝卸载（读 state 的 runtime 与 pids）
    · 卸载收尾 → 清空状态文件（pids=[] / processes=[] / runtime=false）
"""

import logging
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

from utils.app_path import (
    find_apps_component_dir,
    read_state,
    read_state_pids,
    filter_alive_pids,
    is_running,
)
from utils.config_loader import load_config

# ── 全局配置 ──
_CONFIG = load_config()
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")


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
        "task_type": "uninstall",
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "UninstallTaskError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


def _resolve_paths(service_name: str) -> tuple:
    """
    在运行区解析组件路径。

    返回:
        (error, component_dir, version, bin_dir)
        - 出错: (error_result, "", "", "")
        - 正常: (None, 运行区组件目录, version, 运行区 bin 目录)
    """
    if not _APPS_BASE:
        return (
            _build_result(
                "", False, "config.yaml 中未配置 server.apps 运行区路径",
                error_type="ConfigMissing",
                error_message="server.apps 未配置",
            ),
            "", "", "",
        )

    # 在运行区定位组件目录
    component_dir = find_apps_component_dir(service_name)
    if not component_dir:
        return (
            _build_result(
                "", False, f"运行区组件不存在: {os.path.join(_APPS_BASE, service_name)}",
                error_type="AppsComponentNotFound",
                error_message=f"运行区未找到组件 {service_name}，无需卸载",
            ),
            "", "", "",
        )

    # 版本号来自运行状态文件
    state = read_state(service_name)
    version = str(state.get("version", "") or "").strip()
    if not version:
        return (
            _build_result(
                "", False, "运行状态中 version 字段为空",
                error_type="VersionMissing",
                error_message="state/config.yaml 中未记录版本号，无法定位卸载脚本",
            ),
            "", "", "",
        )

    # bin 目录：{apps}/{service_name}/bin（指向 current/bin 的软链接）
    bin_dir = os.path.join(component_dir, "bin")
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

def uninstall_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    卸载服务任务（执行运行区的 bin/uninstall.sh 脚本）。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），同时也是组件目录名

    流程:
        1. 在运行区 {server.apps}/{service_name}/ 定位组件，从 state 读版本号
        2. 检查服务是否仍在运行（state.runtime 为真且存在存活 pid）
           - 在运行 → 返回失败，提示先停止服务
           - 已停止 → 执行 bin/uninstall.sh 卸载脚本
        3. 卸载成功 → 清空运行状态（pids=[] / processes=[] / runtime=false）
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()

    # ── 参数校验 ──
    if not task_id:
        return _build_result("", False, "参数缺失: task_id",
                             error_type="ParameterMissing", error_message="task_id 缺失")
    if not service_name:
        return _build_result(task_id, False, "参数缺失: service_name",
                             error_type="ParameterMissing", error_message="service_name 缺失")

    # ── 路径解析 ──
    error, component_dir, version, bin_dir = _resolve_paths(service_name)
    if error:
        error["task_id"] = task_id
        error["data"] = {
            **(error.get("data") or {}),
            "service_name": service_name,
        }
        return error

    logging.info("[uninstall_task] 组件目录: %s, 版本: %s, bin目录: %s", component_dir, version, bin_dir)

    base_data = {
        "service_name": service_name,
        "version": version,
        "component_dir": component_dir,
        "bin_dir": bin_dir,
    }

    # ── 检查服务是否仍在运行 ──
    # 判据：state.runtime == true 且 pids 中尚有存活进程
    # （仅看 runtime 字段不够，可能因异常退出而残留 true）
    alive_pids = filter_alive_pids(read_state_pids(service_name), service_name)
    if is_running(service_name) and alive_pids:
        pid_value = ",".join(str(p) for p in alive_pids)
        logging.error("[uninstall_task] 服务仍在运行，拒绝卸载: pids=%s", alive_pids)
        return _build_result(
            task_id, False, f"服务仍在运行 (PID: {pid_value})，请先执行停止任务",
            data={
                **base_data,
                "status": "uninstall_blocked",
                "pids": alive_pids,
                "pid": pid_value,
                "runtime": True,
            },
            error_type="ServiceRunning",
            error_message=f"进程 {pid_value} 仍在运行，请先执行停止任务",
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
        "[uninstall_task] 脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
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

    # ── 清理运行区与运行状态 ──
    # 卸载后组件不应再留在运行区（缓存区保留，可重新安装）
    cleared = []
    try:
        # 逐个删除软链接（先 unlink，避免 rmtree 追进缓存区真实目录）
        for name in ("current", "app", "bin", "config"):
            link = os.path.join(component_dir, name)
            if os.path.islink(link):
                os.unlink(link)
                cleared.append(name)
            elif os.path.isdir(link):
                shutil.rmtree(link, ignore_errors=True)
                cleared.append(name)

        # 删除 runtime / state 目录
        for d in ("runtime", "state"):
            p = os.path.join(component_dir, d)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                cleared.append(d)

        # 组件目录若已空则一并删除
        if os.path.isdir(component_dir) and not os.listdir(component_dir):
            os.rmdir(component_dir)
            cleared.append("(组件目录)")

        logging.info("[uninstall_task] 运行区已清理: %s", cleared)
    except Exception as e:
        logging.warning("[uninstall_task] 清理运行区失败（不影响卸载结果）: %s", e)

    return _build_result(task_id, True, "卸载完成", data={
        **base_data,
        "status": "uninstalled",
        "cleared": cleared,
        "exit_code": exec_result["exit_code"],
    })


# ── 自测入口 ──

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = uninstall_task(
        {
            "task_id": "test-uninstall-001",
            "service_name": "hellogitworld-master",
        },
    )

    print("\n" + "=" * 60)
    print("  卸载任务结果")
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
