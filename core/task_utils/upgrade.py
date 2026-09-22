"""
升级服务任务模块

完整流程：版本比对 → 校验缓存区已下载版本 → 停止旧版本 → 安装 → 启动（**不再下载**）

缓存区/运行区分离后：
    · 版本来源   → state/config.yaml 的 version 字段（原为 {component}/version 文件）
    · 组件定位   → 运行区 {server.apps}/{service_name}/
    · 升级动作   → **不再下载**：升级入口只允许选缓存区里已下载的版本，
                   这里校验 `{download}/{service_name}/{版本}/` 存在后，
                   由 install 阶段重建 current 软链接指向该版本（包已在节点上）
    · 回滚动作   → **仅把 current 软链接指回旧版本**，其余文件共用无需动
                   （新版本内容保留在缓存区，可再次升级时复用）

说明：app/bin/config 三个软链接指向 `current/xxx`，因此改 current 的指向
即可整体切换版本；子任务（download/install/start）均已完成运行区适配。
"""

import logging
import os
import shutil
import subprocess
import traceback
from typing import Any, Dict, List, Optional

from utils.app_path import (
    find_apps_component_dir,
    find_cache_version_dir,
    read_state,
    write_state,
    read_state_pids,
    filter_alive_pids,
    refresh_state_pids,
    link_to_current,
    list_cache_versions,
)
from utils.config_loader import load_config
from core.task_utils.install import install_task
from core.task_utils.start import start_task

# ── 全局配置 ──
_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")
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
        # 顶层状态码（平台约定）: 3=已启动 / 7=升级失败
        "status": 3 if success else 7,
        "task_id": task_id,
        "task_type": "upgrade",
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "UpgradeTaskError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


def _task_success(result: Dict[str, Any]) -> bool:
    """兼容两种任务返回格式（download 用 result，install/start 用 success）"""
    if "success" in result:
        return bool(result["success"])
    if "result" in result:
        return bool(result["result"])
    return False


def _task_message(result: Dict[str, Any]) -> str:
    return str(result.get("message", "") or "")


# =============================================================================
# 进程检测工具
# =============================================================================

def _get_process_start_ticks(pid: int) -> Optional[int]:
    """读取 /proc/{pid}/stat 的 starttime，用于 PID 复用检测。

    kill -9 后 PID 可能被内核回收并被新进程复用，仅靠 PID 存在性判断会误判。
    返回 None 表示读取失败（进程已不存在或平台不支持）。
    """
    if os.name == "nt":
        return None
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            stat = f.read().strip()
        # 格式: "pid (comm) state ... starttime ..."，starttime 是 ')' 后第 21 个字段
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
    """检查进程是否为僵尸态(Z)或已死亡(X)。

    kill -9 后进程会短暂进入僵尸态，/proc/{pid} 仍存在、os.kill(pid,0) 仍成功，
    但进程实际已终止。此时必须检查 /proc/{pid}/stat 的 state 字段才能识别。
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
        return fields[0] in ("Z", "z", "X", "x")
    except (OSError, FileNotFoundError):
        return False


def _process_exists(pid: int, expected_start_ticks: Optional[int] = None) -> bool:
    """检查进程是否真的存活（跨平台）。

    多重校验防止误判：
    1. os.kill(pid, 0) 检查信号发送
    2. /proc/{pid}/stat 状态为 Z(僵尸)/X(死亡) → 视为已停止
    3. 若提供了停止前的 starttime，对比发现 PID 已被复用 → 视为已停止
    """
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True,
            )
            return str(pid) in proc.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            # ── 检查1: 僵尸/已死亡进程视为已停止 ──
            if _is_zombie_or_dead(pid):
                logging.info("[upgrade_task] PID %d 处于僵尸/死亡态，判定为已停止", pid)
                return False
            # ── 检查2: starttime 比对 ──
            # 读不到 starttime 说明 /proc/{pid}/stat 已不可读（进程正在消失），
            # 同样判定为已停止；读到但值不同说明 PID 被复用。
            if expected_start_ticks is not None:
                current_start = _get_process_start_ticks(pid)
                if current_start is None:
                    logging.info("[upgrade_task] PID %d 的 starttime 已不可读（进程正在退出），判定为已停止", pid)
                    return False
                if current_start != expected_start_ticks:
                    logging.warning(
                        "[upgrade_task] PID %d 启动时间已变化，该 PID 已被复用，原进程已停止",
                        pid,
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
# 回滚
# =============================================================================

def _rollback(
    service_name: str,
    current_version: str,
    new_version: str,
    was_running: bool,
    timeout: int,
) -> List[str]:
    """
    回滚升级操作。

    缓存区/运行区分离后，回滚极其轻量：
        ① 把运行区 current 软链接**指回旧版本**（核心动作，其余文件共用无需动）
        ② 恢复 state/config.yaml 的 version 字段
        ③ 若旧进程升级前在运行，则重新启动

    **不再删除新版本目录** —— 新版本内容与旧版本平级共存在缓存区，
    以后想再升级可直接复用，无需重新下载。

    返回回滚步骤描述列表。
    """
    rollback_steps: List[str] = []

    if not current_version:
        rollback_steps.append("无旧版本可回滚")
        return rollback_steps

    # 1. 把 current 软链接指回旧版本（app/bin/config 无需重建，它们指向 current/xxx）
    relink = link_to_current(service_name, current_version)
    if relink.get("ok"):
        msg = f"current 软链接已回滚到旧版本: {current_version}"
        logging.info("[upgrade_task][rollback] %s", msg)
    else:
        msg = f"回滚软链接失败: {relink.get('error')}"
        logging.error("[upgrade_task][rollback] %s", msg)
    rollback_steps.append(msg)

    # 2. 恢复运行状态中的版本号
    state = read_state(service_name)
    if state:
        state["version"] = current_version
        if write_state(service_name, state):
            msg = f"运行状态 version 已恢复为: {current_version}"
            logging.info("[upgrade_task][rollback] %s", msg)
        else:
            msg = "运行状态写入失败"
            logging.warning("[upgrade_task][rollback] %s", msg)
        rollback_steps.append(msg)
    else:
        rollback_steps.append("运行状态文件不存在，跳过版本号恢复")

    # 3. 如果旧进程之前是运行的，重新启动
    if was_running:
        start_result = start_task({
            "task_id": "rollback-start",
            "service_name": service_name,
            "version": current_version,
        }, retry=0, timeout=timeout)
        if _task_success(start_result):
            msg = "旧版本已重新启动"
            logging.info("[upgrade_task][rollback] %s", msg)
        else:
            msg = f"旧版本重启失败: {_task_message(start_result)}"
            logging.error("[upgrade_task][rollback] %s", msg)
        rollback_steps.append(msg)
    else:
        rollback_steps.append("旧版本未运行，无需重启")

    return rollback_steps


# =============================================================================
# 主入口
# =============================================================================

def upgrade_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    升级服务任务（版本比对 → 校验缓存区已下载版本 → 停止旧版 → 安装 → 启动；**不再下载**）。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），同时也是组件目录名
        - version:      新版本号（必填，必须已在缓存区下载过）
        - download_url / file_suffix: 兼容保留，不再使用

    流程:
        1. 从运行区 state/config.yaml 读取当前版本
        2. 校验参数（task_id / service_name / version 必填；download_url 已不再需要）
        3. 若当前版本 == 新版本 → 返回 "该版本正在使用"
        4. **先校验该版本已在缓存区**（未下载则直接报错返回，此时旧服务未被动过）
        5. 若当前版本不同，停止旧版本:
           a. 依 state 的存活 pids 判断是否在运行 → 执行运行区 bin/stop.sh
           b. 校验进程已销毁 → 清空运行状态
        6. 安装（rebuild current 软链接指向新版本）
        7. 启动新版本
        8. 任一步失败 → 回滚（current 切回旧版本，并尝试恢复运行）

    版本切换只重建 current 软链接，app/bin/config 保持共用。
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()
    download_url = str(parameters.get("download_url", "") or "").strip()
    file_suffix = str(parameters.get("file_suffix", "") or "").strip()
    version = str(parameters.get("version", "") or "").strip()
    # 平台应用记录 ID（下载成功后写入新版目录的 config/app.yaml）
    app_id = str(parameters.get("app_id", "") or "").strip()

    # ── 参数校验 ──
    # 注意：升级不再下载（直接用缓存区已下载的版本），
    # 所以 download_url / file_suffix 不再是必填（兼容旧调用，传了也不使用）。
    missing: List[str] = []
    for key, val in [
        ("task_id", task_id),
        ("service_name", service_name),
        ("version", version),
    ]:
        if not val:
            missing.append(key)
    if missing:
        return _build_result(
            task_id, False, f"参数缺失: {', '.join(missing)}",
            error_type="ParameterMissing",
            error_message=f"缺失参数: {', '.join(missing)}",
        )

    if not _DOWNLOAD_BASE:
        return _build_result(
            task_id, False, "config.yaml 中未配置 server.download",
            error_type="ConfigMissing",
            error_message="server.download 未配置",
        )

    if not _APPS_BASE:
        return _build_result(
            task_id, False, "config.yaml 中未配置 server.apps 运行区路径",
            error_type="ConfigMissing",
            error_message="server.apps 未配置",
        )

    # ── 运行区组件目录 ──
    component_dir = find_apps_component_dir(service_name)
    steps: List[Dict[str, Any]] = []

    if not component_dir:
        return _build_result(
            task_id, False,
            f"运行区组件不存在: {os.path.join(_APPS_BASE, service_name)}",
            data={"service_name": service_name, "apps_dir": _APPS_BASE},
            error_type="AppsComponentNotFound",
            error_message=f"运行区未找到组件 {service_name}，请先执行安装任务",
        )

    # ── 新版本号来自任务参数 version（必填，缺失已在上方校验拦截）──
    new_version = version

    # ── Step 1: 读取当前版本（来自运行状态文件），比对 ──
    state = read_state(service_name)
    current_version = str(state.get("version", "") or "").strip()

    logging.info("[upgrade_task] 当前版本: %s, 新版本: %s", current_version or "(无)", new_version)

    if current_version == new_version:
        return _build_result(
            task_id, True, f"版本 {new_version} 正在使用，无需升级",
            data={
                "status": "already_uptodate",
                "service_name": service_name,
                "version": current_version,
                "component_dir": component_dir,
            },
        )

    # ── Step 2: 校验「缓存区已下载的版本」（不下载）──
    # 升级入口（平台侧）只允许选节点缓存区里已下载的版本，所以这里只校验目录存在：
    # 存在 → 继续（Step 3 停旧版 → Step 4 安装）；不存在 → 直接报错，不再自动下载。
    # ★ 必须放在「停止旧版本」之前：校验失败时旧服务还没被动过，
    #   不会出现"把旧版本停了、才发现新版本没下载"导致服务停在那儿起不来的情况。
    cache_version_dir = find_cache_version_dir(service_name, new_version)
    if not cache_version_dir:
        available = list_cache_versions(service_name)
        steps.append({
            "step": "check_cache",
            "success": False,
            "message": f"缓存区未找到版本 {new_version}",
        })
        return _build_result(
            task_id, False,
            f"版本 {new_version} 未下载到节点，请先执行「下载」任务",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "new_version": new_version,
                "available_versions": available,
                "steps": steps,
            },
            error_type="VersionNotDownloaded",
            error_message=f"缓存区中未找到版本 {new_version}，已下载的版本: {available or '无'}",
        )
    steps.append({
        "step": "check_cache",
        "success": True,
        "message": f"使用缓存区已下载版本（跳过下载）: {cache_version_dir}",
    })
    logging.info("[upgrade_task] 跳过下载，直接安装缓存区版本: %s", cache_version_dir)

    # ── Step 3: 停止旧版本服务 ──
    # 判据与 stop_task 一致：state.runtime 为真且存在存活进程
    was_running = False   # 回滚标记：旧进程是否原本在运行
    if current_version:
        alive_pids = filter_alive_pids(read_state_pids(service_name), service_name)
        pid_before = ",".join(str(p) for p in alive_pids)

        if alive_pids:
            # 执行运行区的 bin/stop.sh（经 current/bin 软链接）
            ext = ".bat" if os.name == "nt" else ".sh"
            bin_dir = os.path.join(component_dir, "bin")
            stop_script = os.path.join(bin_dir, f"stop{ext}")

            if not os.path.isfile(stop_script):
                return _build_result(
                    task_id, False,
                    f"停止脚本不存在，无法停止运行中的旧版本 {current_version}: {stop_script}",
                    data={"service_name": service_name,
                          "current_version": current_version,
                          "new_version": new_version,
                          "pids": alive_pids, "steps": steps},
                    error_type="StopScriptNotFound",
                    error_message=f"停止脚本不存在: {stop_script}",
                )

            logging.info("[upgrade_task] 执行停止脚本: %s", stop_script)
            stop_exec = _execute_script(stop_script, bin_dir, min(timeout, 60))
            logging.info(
                "[upgrade_task] 停止脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
                stop_exec["exit_code"], stop_exec.get("stdout", ""), stop_exec.get("stderr", ""),
            )
            steps.append({
                "step": "stop_old",
                "success": stop_exec["success"],
                "message": "停止旧版本服务" + ("成功" if stop_exec["success"] else f"失败 (exit_code={stop_exec['exit_code']})"),
                "data": {"pid_before": pid_before,
                         "exit_code": stop_exec["exit_code"],
                         "stdout": stop_exec.get("stdout", ""),
                         "stderr": stop_exec.get("stderr", "")},
            })

            # ── 校验进程是否已销毁（多 PID 逐一检查，starttime 防 PID 复用误判）──
            pid_start_ticks = {p: _get_process_start_ticks(p) for p in alive_pids}
            still_alive = [str(p) for p in alive_pids
                           if _process_exists(p, pid_start_ticks.get(p))]
            if still_alive:
                return _build_result(
                    task_id, False, f"停止旧版本失败: 进程 {', '.join(still_alive)} 仍然存活",
                    data={"service_name": service_name,
                          "current_version": current_version,
                          "new_version": new_version,
                          "pids": alive_pids, "process_still_alive": True,
                          "steps": steps},
                    error_type="ProcessStillAlive",
                    error_message=f"PID {', '.join(still_alive)} 进程仍然存活，无法升级",
                )

            logging.info("[upgrade_task] 旧进程 %s 已销毁", pid_before)
            was_running = True   # 标记旧进程曾运行，回滚时需要重启

            # 清空运行状态（pids/processes 清空，runtime 置 false）
            refresh_state_pids(service_name, [], [])
        else:
            logging.info("[upgrade_task] 无存活进程，旧版本未运行，跳过停止步骤")
            steps.append({
                "step": "stop_old",
                "success": True,
                "message": "旧版本未运行，跳过停止",
            })
    else:
        logging.info("[upgrade_task] 无当前版本，首次安装，跳过停止步骤")
        steps.append({
            "step": "stop_old",
            "success": True,
            "message": "无旧版本，跳过停止",
        })

    # ── Step 4: 安装新版本 ──
    # install 会重建 current 软链接指向新版本（app/bin/config 保持共存共用）
    logging.info("[upgrade_task] 开始安装新版本")
    install_result = install_task({
        "task_id": task_id,
        "file_name": service_name,
        "version": new_version,
    }, retry=retry, timeout=timeout)

    install_ok = _task_success(install_result)
    steps.append({
        "step": "install",
        "success": install_ok,
        "message": _task_message(install_result),
        "data": install_result.get("data", {}),
    })

    if not install_ok:
        rollback_info = _rollback(service_name, current_version, new_version, was_running, timeout)
        return _build_result(
            task_id, False, f"安装新版本失败，已回滚: {_task_message(install_result)}",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "new_version": new_version,
                "steps": steps,
                "rollback": rollback_info,
            },
            error_type="UpgradeInstallFailed",
            error_message=_task_message(install_result),
        )

    # ── Step 5: 启动新版本（start_task 从运行区 bin/ 读取脚本）──
    logging.info("[upgrade_task] 启动新版本服务")
    start_result = start_task({
        "task_id": task_id,
        "service_name": service_name,
        "version": new_version,
    }, retry=retry, timeout=timeout)

    start_ok = _task_success(start_result)
    start_data = start_result.get("data", {})
    steps.append({
        "step": "start",
        "success": start_ok,
        "message": _task_message(start_result),
        "data": start_data,
    })

    if not start_ok:
        rollback_info = _rollback(service_name, current_version, new_version, was_running, timeout)
        return _build_result(
            task_id, False, f"启动新版本失败，已回滚: {_task_message(start_result)}",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "new_version": new_version,
                "steps": steps,
                "rollback": rollback_info,
            },
            error_type="UpgradeStartFailed",
            error_message=_task_message(start_result),
        )

    return _build_result(task_id, True, "升级完成", data={
        "status": "upgraded",
        "service_name": service_name,
        "old_version": current_version,
        "new_version": new_version,
        "component_dir": component_dir,
        "pid": start_data.get("pid", ""),
        "steps": steps,
    })


# ── 自测入口 ──

if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if os.name == "nt":
        test_start_script = (
            "@echo off\n"
            "chcp 65001 >nul\n"
            "setlocal enabledelayedexpansion\n"
            'set "RUNTIME_DIR=%~dp0..\\runtime"\n'
            'if not exist "!RUNTIME_DIR!" mkdir "!RUNTIME_DIR!"\n'
            'powershell -Command "$p=Start-Process -FilePath \'cmd\' -ArgumentList \'/c ping -n 6 127.0.0.1 > nul\' -WindowStyle Hidden -PassThru; $p.Id | Out-File -FilePath \'!RUNTIME_DIR!\\pid\' -Encoding ascii -NoNewline"\n'
            "exit /b 0\n"
        )
    else:
        test_start_script = (
            "#!/bin/bash\n"
            "set -e\n"
            'BIN_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'RUNTIME_DIR="$BIN_DIR/../runtime"\n'
            'mkdir -p "$RUNTIME_DIR"\n'
            "sleep 5 &\n"
            'echo $! > "$RUNTIME_DIR/pid"\n'
            "exit 0\n"
        )

    if os.name == "nt":
        test_stop_script = (
            "@echo off\n"
            "chcp 65001 >nul\n"
            "setlocal enabledelayedexpansion\n"
            'set "RUNTIME_DIR=%~dp0..\\runtime"\n'
            'set "PID_FILE=!RUNTIME_DIR!\\pid"\n'
            'if exist "!PID_FILE!" (\n'
            '    set /p PID=<"!PID_FILE!"\n'
            '    taskkill /PID !PID! /F >nul 2>&1\n'
            '    del "!PID_FILE!" 2>nul\n'
            ")\n"
            "exit /b 0\n"
        )
    else:
        test_stop_script = (
            "#!/bin/bash\n"
            "set -e\n"
            'BIN_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'RUNTIME_DIR="$BIN_DIR/../runtime"\n'
            'PID_FILE="$RUNTIME_DIR/pid"\n'
            'if [ -f "$PID_FILE" ]; then\n'
            '    PID=$(cat "$PID_FILE")\n'
            '    kill "$PID" 2>/dev/null || true\n'
            '    rm -f "$PID_FILE"\n'
            "fi\n"
            "exit 0\n"
        )

    result = upgrade_task({
        "task_id": "test-upgrade-001",
        "service_name": "hellogitworld-master",
        "version": "2.0.0",
        "script": test_start_script,
        "stop_script": test_stop_script,
        "download_url": "https://github.com/githubtraining/hellogitworld/archive/refs/heads/master.zip",
        "file_suffix": ".zip",
        "displayName": "Hello Git World v2",
        "description": "升级测试",
        "serviceName": "radar-service",
        "groupName": "DEFAULT_GROUP",
        "clusterName": "DEFAULT",
        "weight": 1.0,
        "healthy": True,
        "enabled": True,
        "ephemeral": True,
        "metadata": {"version": "2.0", "protocol": "http"},
    })

    print("\n" + "=" * 60)
    print("  升级任务结果")
    print("=" * 60)
    print(f"  task_id : {result.get('task_id', '')}")
    print(f"  成功    : {result['success']}")
    print(f"  消息    : {result['message']}")
    data = result.get("data", {})
    if data:
        for key in ("service_name", "old_version", "new_version", "status", "pid"):
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
