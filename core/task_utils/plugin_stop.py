"""
插件应用停止服务任务模块

与 stop.py 结构完全对齐，唯一区别：
  路径多一层 plugin/
"""

import logging
import os
import subprocess
import time
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
        "task_id": task_id,
        "task_type": "plugin_stop",
        "status": 5 if success else 6,
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "PluginStopTaskError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


def _resolve_paths(service_name: str, sub_dir: str = "plugin") -> tuple:
    """
    根据 service_name 解析组件路径（多一层 plugin）。

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
                "", False, f"{sub_dir} 组件目录不存在: {component_dir}",
                error_type="FileNotFoundError",
                error_message=f"{sub_dir} 组件目录不存在: {component_dir}",
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
    os.makedirs(bin_dir, exist_ok=True)

    return None, component_dir, version, bin_dir


def _get_process_start_ticks(pid: int) -> Optional[int]:
    """
    读取 /proc/{pid}/stat 中进程的 starttime 字段（自系统启动以来的时钟滴答数）。

    用于后续校验时对比启动时间，防止 kill -9 后 PID 被回收导致误判"进程仍存活"。
    返回 None 表示读取失败（进程已不存在或平台不支持）。
    """
    if os.name == "nt":
        return None
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            stat = f.read().strip()
        # /proc/pid/stat 格式: "pid (comm) state ... starttime ..."
        # starttime 是 comm 之后的第 21 个字段 (0-indexed: 20)
        parts = stat.rsplit(")", 1)
        if len(parts) != 2:
            return None
        fields = parts[1].split()
        if len(fields) < 21:
            return None
        return int(fields[20])  # starttime, 单位为时钟滴答(通常 100Hz)
    except (OSError, FileNotFoundError, ValueError):
        return None


def _is_zombie_or_dead(pid: int) -> bool:
    """检查进程是否为僵尸态(Z)或已死亡(X)，读取 /proc/{pid}/stat 的 state 字段。
    
    kill -9 后进程会短暂进入僵尸态，/proc/{pid} 仍存在、os.kill(pid,0) 仍成功，
    但进程实际已终止。此时仅靠 starttime 对比无法识别，必须检查进程状态。
    """
    if os.name == "nt":
        return False
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            stat = f.read().strip()
        parts = stat.rsplit(")", 1)
        if len(parts) != 2:
            return False
        fields = parts[1].split()
        if len(fields) == 0:
            return False
        state = fields[0]
        return state in ("Z", "z", "X", "x")
    except (OSError, FileNotFoundError):
        return False


def _process_exists(pid: int, expected_start_ticks: Optional[int] = None) -> bool:
    """检查进程是否真的存活（跨平台）。

    多重校验防止误判：
    1. os.kill(pid, 0) 检查信号发送
    2. 检查 /proc/{pid}/stat 状态字段，Z(僵尸)或X(已死亡)视为已停止
    3. 对比 starttime，不一致说明 PID 已被回收复用
    """
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True,
            )
            return str(pid) in result.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            # ── 检查1: 僵尸/已死亡进程视为已停止 ──
            if _is_zombie_or_dead(pid):
                logging.info(
                    "[plugin_stop] PID %d 处于僵尸/死亡态，判定为已停止", pid
                )
                return False
            # ── 检查2: PID 回收 → starttime 已变化 ──
            if expected_start_ticks is not None:
                current_start = _get_process_start_ticks(pid)
                if current_start is not None and current_start != expected_start_ticks:
                    logging.warning(
                        "[plugin_stop] PID %d 启动时间已变化 (之前=%s, 现在=%s)，"
                        "该 PID 已被其他进程复用，原进程已停止",
                        pid, expected_start_ticks, current_start,
                    )
                    return False
            return True
        except (OSError, ProcessLookupError):
            return False


def _execute_script(script_path: str, bin_dir: str, timeout: int) -> Dict[str, Any]:
    """执行停止脚本，返回执行结果。

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


def plugin_stop_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    插件应用停止服务任务（执行 bin/stop.sh 脚本）。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），即 plugin 下的目录名

    流程:
        1. 根据 service_name 定位 plugin 组件目录，读取 version
        2. 找到 bin/stop.sh 脚本并执行
        3. 校验 PID 进程是否已销毁 → 已销毁则删除 pid 文件并返回成功
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()
    sub_dir = str(parameters.get("sub_dir", "") or "plugin").strip()

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

    logging.info("[plugin_stop] plugin 组件目录: %s, 版本: %s, bin目录: %s", component_dir, version, bin_dir)

    # ── 读取 PID（执行前）──
    runtime_dir = os.path.join(component_dir, version, "runtime")
    pid_file = os.path.join(runtime_dir, "pid")
    pid_before = ""
    pid_start_ticks = None
    pid_list = []  # 所有有效 PID
    if os.path.isfile(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as pf:
                content = pf.read().strip()
            logging.info("[plugin_stop] 读取到 PID 文件内容: %s", content)
            if content:
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pid_list.append(int(line))
                    except ValueError:
                        logging.warning("[plugin_stop] 忽略非法 PID 行: %s", line)
            if pid_list:
                pid_before = ",".join(str(p) for p in pid_list)
                pid_start_ticks = _get_process_start_ticks(pid_list[0])
                if pid_start_ticks is not None:
                    logging.info("[plugin_stop] 目标进程启动时间戳: %s", pid_start_ticks)
        except Exception as e:
            logging.warning("[plugin_stop] 读取 PID 文件失败: %s", e)

    # ── 定位 stop.sh 脚本 ──
    ext = ".bat" if os.name == "nt" else ".sh"
    script_path = os.path.join(bin_dir, f"stop{ext}")
    if not os.path.isfile(script_path):
        return _build_result(
            task_id, False, f"停止脚本不存在: {script_path}",
            data={"service_name": service_name, "version": version, "bin_dir": bin_dir},
            error_type="FileNotFoundError",
            error_message=f"停止脚本不存在: {script_path}",
        )

    base_data = {
        "service_name": service_name,
        "version": version,
        "component_dir": component_dir,
        "bin_dir": bin_dir,
        "script_path": script_path,
        "runtime_dir": runtime_dir,
        "pid_file": pid_file,
        "pid_before": pid_before,
    }

    # ── 执行停止脚本 ──
    exec_timeout = min(timeout, 60)
    exec_result = _execute_script(script_path, bin_dir, exec_timeout)
    logging.info(
        "[plugin_stop] 脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
        exec_result["exit_code"], exec_result.get("stdout", ""), exec_result.get("stderr", ""),
    )

    if not exec_result["success"]:
        return _build_result(
            task_id, False, f"停止脚本执行失败 (exit_code={exec_result['exit_code']})",
            data={
                **base_data,
                "exit_code": exec_result["exit_code"],
                "stdout": exec_result.get("stdout", ""),
                "stderr": exec_result.get("stderr", ""),
            },
            error_type="ScriptExecutionError",
            error_message=exec_result.get("stderr", "") or f"脚本退出码: {exec_result['exit_code']}",
        )

    # ── 校验进程是否已销毁（多 PID 逐一检查）──
    if pid_list:
        still_alive = []
        for pid_int in pid_list:
            try:
                if _process_exists(pid_int, pid_start_ticks if pid_int == pid_list[0] else None):
                    still_alive.append(str(pid_int))
            except Exception:
                still_alive.append(str(pid_int))
        if still_alive:
            return _build_result(
                task_id, False, f"进程 {', '.join(still_alive)} 仍然存活，停止失败",
                data={
                    **base_data,
                    "status": "stop_failed",
                    "pid": pid_before,
                    "process_still_alive": True,
                    "exit_code": exec_result["exit_code"],
                },
                error_type="ProcessStillAlive",
                error_message=f"PID {', '.join(still_alive)} 进程仍然存活",
            )
        logging.info("[plugin_stop] 所有进程 %s 已销毁", pid_before)
    else:
        logging.info("[plugin_stop] 未找到 PID 文件，可能服务未启动，跳过进程校验")

    # ── 删除 PID 文件 ──
    if os.path.isfile(pid_file):
        try:
            os.remove(pid_file)
            logging.info("[plugin_stop] PID 文件已删除: %s", pid_file)
        except Exception as e:
            logging.warning("[plugin_stop] 删除 PID 文件失败: %s", e)

    return _build_result(task_id, True, "进程已停止", data={
        **base_data,
        "status": "stopped",
        "pid": pid_before,
        "process_alive": False,
        "exit_code": exec_result["exit_code"],
        "pid_file_deleted": not os.path.isfile(pid_file),
    })


# ── 自测入口 ──

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = plugin_stop_task(
        {
            "task_id": "test-xkt-stop-001",
            "service_name": "test-service",
        },
    )

    print("\n" + "=" * 60)
    print("  插件停止任务结果")
    print("=" * 60)
    print(f"  task_id : {result.get('task_id', '')}")
    print(f"  成功    : {result['success']}")
    print(f"  消息    : {result['message']}")
    data = result.get("data", {})
    if data:
        for key in ("service_name", "version", "status", "pid", "process_alive", "pid_file_deleted", "exit_code", "bin_dir", "script_path"):
            print(f"  {key}: {data.get(key, 'N/A')}")
    if not result["success"]:
        err = result.get("error", {})
        print(f"  错误类型: {err.get('error_type', '')}")
        print(f"  错误信息: {err.get('error_message', '')}")
    print("=" * 60)
