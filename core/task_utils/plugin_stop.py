"""
停止服务任务模块

与 stop.py 结构完全对齐，唯一区别：路径多一层 plugin/（由 _SUB_DIR 承载）。

在「运行区」定位组件并执行其 bin/stop.sh 停止脚本。

缓存区/运行区分离后：
    · 组件定位 → {server.apps}/{service_name}/（install 建立的软链接结构）
    · pid 来源 → state/config.yaml 的 pids 数组（原为 {版本}/runtime/pid）
    · 停止后校验进程是否真的退出，成功则清空状态（pids=[] / runtime=false）
"""

import logging
import os
import subprocess
import time
from typing import Any, Dict, Optional

from utils.app_path import (
    SUB_DIR_PLUGIN,
    find_apps_component_dir,
    read_state,
    read_state_pids,
    refresh_state_pids,
    get_pid_file,
)
from utils.config_loader import load_config

# ── 全局配置 ──
_CONFIG = load_config()
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

# 本模块处理的应用类别（决定路径中的分层目录）
_SUB_DIR = SUB_DIR_PLUGIN

# 停止后校验进程退出的重试次数（每次间隔 1 秒）
# JVM 等进程收到 SIGTERM 后需要数秒优雅退出，不能执行完立即判定
STOP_CONFIRM_RETRY = 10


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
        # 5=已停止 / 14=停止失败（原为 6，6 在平台枚举里是“升级中”）
        "status": 5 if success else 14,
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "StopTaskError",
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
    component_dir = find_apps_component_dir(service_name, _SUB_DIR)
    if not component_dir:
        return (
            _build_result(
                "", False, f"运行区组件不存在: {os.path.join(_APPS_BASE, _SUB_DIR, service_name)}",
                error_type="AppsComponentNotFound",
                error_message=f"运行区未找到组件 {service_name}，请先执行安装任务",
            ),
            "", "", "",
        )

    # 版本号来自运行状态文件
    state = read_state(service_name, _SUB_DIR)
    version = str(state.get("version", "") or "").strip()
    if not version:
        return (
            _build_result(
                "", False, "运行状态中 version 字段为空",
                error_type="VersionMissing",
                error_message="state/config.yaml 中未记录版本号，无法定位停止脚本",
            ),
            "", "", "",
        )

    # bin 目录：{apps}/{service_name}/bin（指向 current/bin 的软链接）
    bin_dir = os.path.join(component_dir, "bin")

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
        # starttime 在 comm 之后为 0-indexed: 19
        # （原始 1-indexed 第 22 个字段，减去 pid/comm 两项偏移）
        # 先按 ')' 分割，右边是 state 及之后的字段
        parts = stat.rsplit(")", 1)
        if len(parts) != 2:
            return None
        fields = parts[1].split()
        if len(fields) < 20:
            return None
        return int(fields[19])  # starttime, 单位为时钟滴答(通常 100Hz)
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
        # 格式: "pid (comm) state ..."
        parts = stat.rsplit(")", 1)
        if len(parts) != 2:
            return False
        fields = parts[1].split()
        if len(fields) == 0:
            return False
        state = fields[0]  # 第一个字段是进程状态
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
                    "[plugin_stop_task] PID %d 处于僵尸/死亡态，判定为已停止", pid
                )
                return False
            # ── 检查2: starttime 比对 ──
            # 若提供了期望值，则读不到 starttime 说明 /proc/{pid}/stat 已不可读
            # （进程正在消失），同样判定为已停止；读到但值不同说明 PID 被复用。
            if expected_start_ticks is not None:
                current_start = _get_process_start_ticks(pid)
                if current_start is None:
                    logging.info(
                        "[plugin_stop_task] PID %d 的 starttime 已不可读（进程正在退出），"
                        "判定为已停止", pid,
                    )
                    return False
                if current_start != expected_start_ticks:
                    logging.warning(
                        "[plugin_stop_task] PID %d 启动时间已变化 (之前=%s, 现在=%s)，"
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
    停止服务任务（执行运行区的 bin/stop.sh 脚本）。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），同时也是组件目录名

    流程:
        1. 在运行区 {server.apps}/{service_name}/ 定位组件，从 state 读版本号
        2. 从 state/config.yaml 的 pids 读取目标进程
        3. 找到 bin/stop.sh 脚本并执行
        4. 校验各 PID 是否已销毁 → 已销毁则清空状态（pids=[] / runtime=false）
           并删除 state/pid
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

    logging.info("[plugin_stop_task] 组件目录: %s, 版本: %s, bin目录: %s", component_dir, version, bin_dir)

    # ── 读取 PID（执行前）──
    # pid 来源：运行状态 state/config.yaml 的 pids 数组（支持多进程）
    running_state = read_state(service_name, _SUB_DIR)
    pid_list = read_state_pids(service_name, _SUB_DIR)
    pid_file = get_pid_file(service_name, _SUB_DIR) or os.path.join(
        component_dir, "state", "pid")
    pid_before = ""
    pid_start_ticks = None

    if pid_list:
        pid_before = ",".join(str(p) for p in pid_list)
        pid_start_ticks = _get_process_start_ticks(pid_list[0])
        logging.info("[plugin_stop_task] 读取到 pids=%s（来自 state 状态文件）", pid_list)
        if pid_start_ticks is not None:
            logging.info("[plugin_stop_task] 目标进程启动时间戳: %s", pid_start_ticks)
    else:
        logging.info("[plugin_stop_task] 状态文件中无 pid，可能服务未启动")

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
        "pid_file": pid_file,
        "pid_before": pid_before,
    }

    # ── 执行停止脚本 ──
    exec_timeout = min(timeout, 60)
    exec_result = _execute_script(script_path, bin_dir, exec_timeout)
    logging.info(
        "[plugin_stop_task] 脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
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

    # ── 校验进程是否已销毁（多 PID 逐一检查，带等待重试）──
    # 停止脚本多为 pkill/kill，被停止的进程（如 JVM）可能需要数秒优雅退出，
    # 因此需轮询等待，不能执行完立即判定。
    if pid_list:
        still_alive = []
        for pid_int in pid_list:
            exp_start = pid_start_ticks if pid_int == pid_list[0] else None
            alive = True
            for attempt in range(STOP_CONFIRM_RETRY):
                try:
                    if not _process_exists(pid_int, exp_start):
                        alive = False
                        break
                except Exception:
                    alive = False
                    break
                time.sleep(1)
            if alive:
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
        logging.info("[plugin_stop_task] 所有进程 %s 已销毁", pid_before)
    else:
        logging.info("[plugin_stop_task] 未找到 PID 文件，可能服务未启动，跳过进程校验")

    # ── 更新运行状态：清空 pids / processes，runtime 置 false ──
    # （refresh_state_pids 内部以空列表调用即完成清空与置位）
    refresh_state_pids(service_name, [], [], _SUB_DIR)

    # ── 删除 pid 文件（state/pid）──
    if pid_file and os.path.isfile(pid_file):
        try:
            os.remove(pid_file)
            logging.info("[plugin_stop_task] pid 文件已删除: %s", pid_file)
        except Exception as e:
            logging.warning("[plugin_stop_task] 删除 pid 文件失败: %s", e)

    return _build_result(task_id, True, "进程已停止", data={
        **base_data,
        "status": "stopped",
        "pids": pid_list,
        "pid": pid_before,
        "process_alive": False,
        "runtime": False,
        "exit_code": exec_result["exit_code"],
        "pid_file_deleted": not (pid_file and os.path.isfile(pid_file)),
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
            "task_id": "test-stop-001",
            "service_name": "hellogitworld-master",
        },
    )

    print("\n" + "=" * 60)
    print("  停止任务结果")
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
