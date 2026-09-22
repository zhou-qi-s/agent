"""
回滚服务任务模块

与 rollback.py 结构完全对齐，唯一区别：路径多一层 displayConsole/（由 _SUB_DIR 承载）。

完整流程：停止当前版本 → **切换 current 软链接** → 启动目标版本

缓存区/运行区分离后：
    · 版本来源 → state/config.yaml 的 version 字段（原为 {component}/version 文件）
    · 组件定位 → 运行区 {server.apps}/{service_name}/
    · 切换版本 → **重建 current 软链接指向目标版本**（原为改写 version 文件）
                  app/bin/config 无需重建，它们指向 current/xxx
    · 目标版本校验 → 缓存区 {download}/{service_name}/{target_version}/
"""

import logging
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from utils.app_path import (
    SUB_DIR_XKT,
    find_apps_component_dir,
    find_cache_version_dir,
    read_state,
    write_state,
    read_state_pids,
    filter_alive_pids,
    refresh_state_pids,
    link_to_current,
    read_pids_from_file,
    get_process_names,
    wait_process_alive,
)
from utils.config_loader import load_config

# ── 全局配置 ──
_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

# 本模块处理的应用类别（决定路径中的分层目录）
_SUB_DIR = SUB_DIR_XKT

# 启动后等待确认进程存活的秒数（与 start.py 保持一致）
START_CONFIRM_WAIT = 60


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
        # 顶层状态码（平台约定）: 3=成功 / 13=回滚失败
        "status": 3 if success else 13,
        "task_id": task_id,
        "task_type": "xkt_rollback",
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "RollbackTaskError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


# =============================================================================
# 进程与脚本工具
# =============================================================================

def _get_process_start_ticks(pid: int) -> Optional[int]:
    """读取 /proc/{pid}/stat 中进程的 starttime 字段（自系统启动以来的时钟滴答数）。

    用于后续校验时对比启动时间，防止 kill -9 后 PID 被回收复用导致误判"进程仍存活"。
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

    kill 后进程会短暂进入僵尸态，/proc/{pid} 仍存在、os.kill(pid,0) 仍成功，
    但进程实际已终止。此场景下仅靠 os.kill 无法识别，必须检查进程状态。
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

    与 stop.py 保持一致的多重校验，防止误判：
    1. os.kill(pid, 0) 检查信号发送
    2. 检查 /proc/{pid}/stat 状态字段，Z(僵尸)或X(已死亡)视为已停止
    3. 对比 starttime，不一致说明 PID 已被回收复用（原进程已停止）
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
            # 僵尸/已死亡进程视为已停止
            if _is_zombie_or_dead(pid):
                logging.info(
                    "[xkt_rollback_task] PID %d 处于僵尸/死亡态，判定为已停止", pid
                )
                return False
            # starttime 比对：读不到说明 /proc/{pid}/stat 已不可读（进程正在消失），
            # 同样判定为已停止；读到但值不同说明 PID 被复用。
            if expected_start_ticks is not None:
                current_start = _get_process_start_ticks(pid)
                if current_start is None:
                    logging.info(
                        "[xkt_rollback_task] PID %d 的 starttime 已不可读（进程正在退出），"
                        "判定为已停止", pid,
                    )
                    return False
                if current_start != expected_start_ticks:
                    logging.warning(
                        "[xkt_rollback_task] PID %d 启动时间已变化 (之前=%s, 现在=%s)，"
                        "该 PID 已被其他进程复用，原进程已停止",
                        pid, expected_start_ticks, current_start,
                    )
                    return False
            return True
        except (OSError, ProcessLookupError):
            return False


def _execute_script(script_path: str, bin_dir: str, timeout: int) -> Dict[str, Any]:
    """执行脚本，返回 {success, exit_code, stdout, stderr}"""
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
        return {"success": False, "exit_code": -1, "stdout": "", "stderr": f"脚本执行超时 ({timeout}s)"}
    except Exception as e:
        return {"success": False, "exit_code": -1, "stdout": "", "stderr": f"脚本执行异常: {e}"}


# =============================================================================
# 路径解析
# =============================================================================

def _resolve_paths(service_name: str, target_version: str) -> tuple:
    """
    解析回滚所需的路径，并校验目标版本是否已存在于缓存区。

    返回:
        (error, component_dir, current_version, target_version_dir)
        - 出错: (error_result, "", "", "")
        - 正常: (None, 运行区组件目录, 当前版本, 缓存区目标版本目录)
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

    if not _DOWNLOAD_BASE:
        return (
            _build_result(
                "", False, "config.yaml 中未配置 server.download",
                error_type="ConfigMissing",
                error_message="server.download 未配置",
            ),
            "", "", "",
        )

    # 运行区组件目录
    component_dir = find_apps_component_dir(service_name, _SUB_DIR)
    if not component_dir:
        return (
            _build_result(
                "", False,
                f"运行区组件不存在: {os.path.join(_APPS_BASE, _SUB_DIR, service_name)}",
                error_type="AppsComponentNotFound",
                error_message=f"运行区未找到组件 {service_name}，请先执行安装任务",
            ),
            "", "", "",
        )

    # 当前版本来自运行状态文件
    state = read_state(service_name, _SUB_DIR)
    current_version = str(state.get("version", "") or "").strip()

    # 校验目标版本在缓存区中是否存在（回滚是切到已下载过的版本）
    target_version_dir = find_cache_version_dir(service_name, target_version, _SUB_DIR)
    if not target_version_dir:
        return (
            _build_result(
                "", False,
                f"目标版本目录不存在: {os.path.join(_DOWNLOAD_BASE, service_name, target_version)}",
                error_type="TargetVersionNotFound",
                error_message=f"目标版本 {target_version} 未下载，无法回滚",
            ),
            "", "", "",
        )

    return None, component_dir, current_version, target_version_dir


# =============================================================================
# 主入口
# =============================================================================

def xkt_rollback_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    回滚服务任务（停止当前版本 → 切换版本号 → 启动目标版本）。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），同时也是组件目录名
        - version:      回滚目标版本号（必填）

    流程:
        1. 从 state/config.yaml 读取当前版本
        2. 若当前版本 == 目标版本 → 返回提示
        3. 校验目标版本已存在于缓存区 {download}/{service_name}/{target_version}/
        4. 停止当前版本（运行区 bin/stop.sh + 校验进程已销毁）
        5. **重建 current 软链接指向目标版本** + 更新 state.version
        6. 启动目标版本（运行区 bin/start.sh）
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()
    target_version = str(parameters.get("version", "") or "").strip()

    # ── 参数校验 ──
    missing: List[str] = []
    for key, val in [
        ("task_id", task_id),
        ("service_name", service_name),
        ("version", target_version),
    ]:
        if not val:
            missing.append(key)
    if missing:
        return _build_result(
            task_id, False, f"参数缺失: {', '.join(missing)}",
            error_type="ParameterMissing",
            error_message=f"缺失参数: {', '.join(missing)}",
        )

    steps: List[Dict[str, Any]] = []

    # ── 路径解析 ──
    error, component_dir, current_version, target_version_dir = \
        _resolve_paths(service_name, target_version)
    if error:
        error["task_id"] = task_id
        error["data"] = {
            **(error.get("data") or {}),
            "service_name": service_name,
            "target_version": target_version,
            "steps": steps,
        }
        return error

    logging.info(
        "[xkt_rollback_task] service=%s, current_version=%s, target_version=%s",
        service_name, current_version or "(无)", target_version,
    )

    # ── 版本比对 ──
    if current_version == target_version:
        return _build_result(task_id, True, f"当前版本已是 {target_version}，无需回滚", data={
            "service_name": service_name,
            "version": target_version,
            "status": "already_on_target",
            "component_dir": component_dir,
            "steps": steps,
        })

    # ── Step 1: 停止当前版本 ──
    # 判据与 stop_task 一致：state 中有存活的 pids
    ext = ".bat" if os.name == "nt" else ".sh"
    was_running = False   # 记录原版本是否在运行，决定回滚后是否启动

    if current_version:
        alive_pids = filter_alive_pids(read_state_pids(service_name, _SUB_DIR), service_name)
        pid_before = ",".join(str(p) for p in alive_pids)

        if alive_pids:
            # 执行运行区的 bin/stop.sh（经 current/bin 软链接）
            old_bin_dir = os.path.join(component_dir, "bin")
            stop_script_path = os.path.join(old_bin_dir, f"stop{ext}")

            if not os.path.isfile(stop_script_path):
                return _build_result(
                    task_id, False, f"当前版本停止脚本不存在: {stop_script_path}",
                    data={
                        "service_name": service_name,
                        "current_version": current_version,
                        "target_version": target_version,
                        "steps": steps,
                    },
                    error_type="StopScriptNotFound",
                    error_message=f"当前版本 {current_version} 的 bin 目录下无停止脚本，无法停止旧进程",
                )

            # 记录旧进程启动时间戳，用于 stop 后校验 PID 是否被回收复用
            pid_start_ticks = _get_process_start_ticks(alive_pids[0])
            if pid_start_ticks is not None:
                logging.info("[xkt_rollback_task] 旧进程启动时间戳: %s", pid_start_ticks)

            # 执行 stop 脚本
            logging.info("[xkt_rollback_task] 执行停止脚本: %s", stop_script_path)
            stop_exec = _execute_script(stop_script_path, old_bin_dir, min(timeout, 60))
            logging.info(
                "[xkt_rollback_task] 停止脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
                stop_exec["exit_code"], stop_exec.get("stdout", ""), stop_exec.get("stderr", ""),
            )
            steps.append({
                "step": "stop_current",
                "success": stop_exec["success"],
                "message": "停止当前版本" + ("成功" if stop_exec["success"] else f"失败 (exit_code={stop_exec['exit_code']})"),
                "data": {
                    "pid_before": pid_before,
                    "exit_code": stop_exec["exit_code"],
                    "stdout": stop_exec.get("stdout", ""),
                    "stderr": stop_exec.get("stderr", ""),
                },
            })

            if not stop_exec["success"]:
                return _build_result(
                    task_id, False, f"停止当前版本失败 (exit_code={stop_exec['exit_code']})",
                    data={
                        "service_name": service_name,
                        "current_version": current_version,
                        "target_version": target_version,
                        "steps": steps,
                    },
                    error_type="StopCurrentFailed",
                    error_message=stop_exec.get("stderr", "") or f"脚本退出码: {stop_exec['exit_code']}",
                )

            # 校验进程是否已销毁（多 PID 逐一检查，带等待重试）
            still_alive = []
            for pid_int in alive_pids:
                exp_start = pid_start_ticks if pid_int == alive_pids[0] else None
                alive = True
                for attempt in range(3):
                    try:
                        if not _process_exists(pid_int, exp_start):
                            alive = False
                            break
                    except Exception:
                        alive = False
                        break
                    # kill -9 后进程可能短暂处于退出中，等待后重试
                    time.sleep(1)
                if alive:
                    still_alive.append(str(pid_int))
            if still_alive:
                return _build_result(
                    task_id, False, f"进程 {', '.join(still_alive)} 仍然存活，停止失败",
                    data={
                        "service_name": service_name,
                        "current_version": current_version,
                        "target_version": target_version,
                        "pids": alive_pids,
                        "process_still_alive": True,
                        "steps": steps,
                    },
                    error_type="ProcessStillAlive",
                    error_message=f"PID {', '.join(still_alive)} 进程仍然存活，无法继续回滚",
                )

            logging.info("[xkt_rollback_task] 进程 %s 已销毁", pid_before)
            was_running = True

            # 清空运行状态（pids/processes 清空，runtime 置 false）
            refresh_state_pids(service_name, [], [], _SUB_DIR)
        else:
            logging.info("[xkt_rollback_task] 当前版本未运行，跳过停止步骤")
            steps.append({
                "step": "stop_current",
                "success": True,
                "message": "当前版本未运行，跳过停止",
            })
    else:
        logging.info("[xkt_rollback_task] 无当前版本，跳过停止步骤")
        steps.append({
            "step": "stop_current",
            "success": True,
            "message": "无当前版本，跳过停止",
        })

    # ── Step 2: 切换 current 软链接指向目标版本 + 更新 state.version ──
    # 核心动作：只需重建 current 一条链接，app/bin/config 无需重建
    relink = link_to_current(service_name, target_version, _DOWNLOAD_BASE, _APPS_BASE, _SUB_DIR)
    if not relink.get("ok"):
        return _build_result(
            task_id, False, f"切换版本失败: {relink.get('error')}",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "target_version": target_version,
                "steps": steps,
            },
            error_type="VersionSwitchFailed",
            error_message=relink.get("error", "重建 current 软链接失败"),
        )
    logging.info("[xkt_rollback_task] current 软链接已指向: %s", target_version)
    steps.append({
        "step": "switch_version",
        "success": True,
        "message": f"current 软链接已切换到 {target_version}(原: {current_version or '无'})",
    })

    # 同步运行状态中的版本号
    state = read_state(service_name, _SUB_DIR)
    if state:
        state["version"] = target_version
        write_state(service_name, state, _SUB_DIR)

    # ── Step 3: 启动目标版本 ──
    # 只有原版本本来就在运行时才重启，保持「回滚到操作前状态」的语义；
    # 原本未运行的服务，回滚后也保持未运行。
    if not was_running:
        logging.info("[xkt_rollback_task] 原版本未运行，跳过启动步骤")
        steps.append({
            "step": "start_target",
            "success": True,
            "message": "原版本未运行，回滚后保持未运行",
        })
        return _build_result(task_id, True,
                             f"回滚完成: {current_version or '无'} → {target_version}",
                             data={
                                 "status": "rollback_done",
                                 "service_name": service_name,
                                 "old_version": current_version,
                                 "current_version": target_version,
                                 "component_dir": component_dir,
                                 "pids": [],
                                 "alive_confirmed": False,
                                 "steps": steps,
                             })

    target_bin_dir = os.path.join(component_dir, "bin")
    target_start_script = os.path.join(target_bin_dir, f"start{ext}")

    if not os.path.isfile(target_start_script):
        return _build_result(
            task_id, False, f"目标版本启动脚本不存在: {target_start_script}",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "target_version": target_version,
                "steps": steps,
            },
            error_type="StartScriptNotFound",
            error_message=f"目标版本 {target_version} 的 bin 目录下无启动脚本: {target_start_script}",
        )

    logging.info("[xkt_rollback_task] 使用目标版本启动脚本: %s", target_start_script)

    # 执行 start 脚本
    logging.info("[xkt_rollback_task] 执行目标版本启动脚本: %s", target_start_script)
    start_exec = _execute_script(target_start_script, target_bin_dir, min(timeout, 60))
    logging.info(
        "[xkt_rollback_task] 启动脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
        start_exec["exit_code"], start_exec.get("stdout", ""), start_exec.get("stderr", ""),
    )

    if not start_exec["success"]:
        # 启动失败 → 把 current 切回原版本（软链接回切，只需一步）
        logging.error("[xkt_rollback_task] 目标版本启动失败，尝试把 current 切回原版本 %s",
                      current_version or "(无)")
        if current_version:
            back = link_to_current(service_name, current_version, _DOWNLOAD_BASE, _APPS_BASE, _SUB_DIR)
            st = read_state(service_name, _SUB_DIR)
            if st:
                st["version"] = current_version
                write_state(service_name, st, _SUB_DIR)
            steps.append({
                "step": "recover_version",
                "success": bool(back.get("ok")),
                "message": (f"启动失败，current 已切回 {current_version}"
                            if back.get("ok") else f"切回失败: {back.get('error')}"),
            })
        else:
            steps.append({
                "step": "recover_version",
                "success": False,
                "message": "无原版本可切回",
            })

        return _build_result(
            task_id, False, f"启动目标版本 {target_version} 失败 (exit_code={start_exec['exit_code']})",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "target_version": target_version,
                "steps": steps,
            },
            error_type="StartTargetFailed",
            error_message=start_exec.get("stderr", "") or f"脚本退出码: {start_exec['exit_code']}",
        )

    # 启动脚本返回成功 → 等 60 秒确认进程存活，并写入运行状态
    steps.append({
        "step": "start_target",
        "success": True,
        "message": f"目标版本 {target_version} 启动脚本执行成功",
        "data": {
            "exit_code": start_exec["exit_code"],
            "stdout": start_exec.get("stdout", ""),
        },
    })

    # 读取脚本写入的 pid（state/pid），等待确认存活
    pids = read_pids_from_file(service_name, _SUB_DIR)
    confirm = {"confirmed": False, "pids": pids}
    if pids:
        confirmed = wait_process_alive(service_name, pids, START_CONFIRM_WAIT)
        confirm["confirmed"] = confirmed
        if confirmed:
            new_state = read_state(service_name, _SUB_DIR)
            new_state.update({
                "pids": pids,
                "processes": get_process_names(pids),
                "name": service_name,
                "runtime": True,
                "version": target_version,
            })
            new_state.pop("pid", None)
            write_state(service_name, new_state, _SUB_DIR)
            steps.append({
                "step": "confirm_alive",
                "success": True,
                "message": f"进程 {pids} 在 {START_CONFIRM_WAIT} 秒后仍存活",
            })
        else:
            steps.append({
                "step": "confirm_alive",
                "success": False,
                "message": f"进程 {pids} 在 {START_CONFIRM_WAIT} 秒内已退出",
            })
    else:
        steps.append({
            "step": "confirm_alive",
            "success": False,
            "message": "启动脚本未写入 pid 文件",
        })

    return _build_result(task_id, True, f"回滚完成: {current_version or '无'} → {target_version}", data={
        "status": "rollback_done",
        "service_name": service_name,
        "old_version": current_version,
        "current_version": target_version,
        "component_dir": component_dir,
        "pids": pids,
        "alive_confirmed": confirm["confirmed"],
        "steps": steps,
    })


# ── 自测入口 ──

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    result = xkt_rollback_task({
        "task_id": "test-rollback-001",
        "service_name": "hellogitworld-master",
        "version": "1.0.0",
    })

    print("\n" + "=" * 60)
    print("  回滚任务结果")
    print("=" * 60)
    print(f"  task_id : {result.get('task_id', '')}")
    print(f"  成功    : {result['success']}")
    print(f"  消息    : {result['message']}")
    data = result.get("data", {})
    if data:
        for key in ("service_name", "old_version", "current_version", "status", "pid", "component_dir"):
            print(f"  {key}: {data.get(key, 'N/A')}")
    steps = data.get("steps", [])
    if steps:
        print("\n  步骤详情:")
        for s in steps:
            status = "OK" if s.get("success") else "FAIL"
            print(f"  [{status}] {s.get('step')}: {s.get('message')}")
    if not result["success"]:
        err = result.get("error", {})
        print(f"  错误类型: {err.get('error_type', '')}")
        print(f"  错误信息: {err.get('error_message', '')}")
    print("=" * 60)
