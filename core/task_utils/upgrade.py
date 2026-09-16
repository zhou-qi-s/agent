"""
升级服务任务模块

基于统一目录结构，实现版本升级的完整流程：
版本比对 → 停止旧版本 → 下载新版本 → 安装 → 启动
"""

import logging
import os
import shutil
import subprocess
import traceback
from typing import Any, Dict, List, Optional

from utils.config_loader import load_config
from core.task_utils.download import download_task
from core.task_utils.install import install_task
from core.task_utils.start import start_task

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
        # 顶层状态码（平台约定）: 3=成功 / 13=升级失败
        "status": 3 if success else 13,
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
        if len(fields) < 21:
            return None
        return int(fields[20])
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
            # ── 检查2: PID 回收 → starttime 已变化 ──
            if expected_start_ticks is not None:
                current_start = _get_process_start_ticks(pid)
                if current_start is not None and current_start != expected_start_ticks:
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
    component_dir: str,
    current_version: str,
    new_version: str,
    was_running: bool,
    old_bin_dir: str,
    timeout: int,
) -> List[str]:
    """
    回滚升级操作：清理新版本目录 → 恢复 version 文件 → 如旧进程之前运行则重启。

    返回回滚步骤描述列表。
    """
    rollback_steps: List[str] = []

    # 1. 删除新版本目录 {new_version}/
    new_version_dir = os.path.join(component_dir, new_version)
    if os.path.isdir(new_version_dir):
        try:
            shutil.rmtree(new_version_dir)
            msg = f"已删除新版本目录: {new_version_dir}"
            logging.info("[upgrade_task][rollback] %s", msg)
            rollback_steps.append(msg)
        except Exception as e:
            msg = f"删除新版本目录失败: {e}"
            logging.warning("[upgrade_task][rollback] %s", msg)
            rollback_steps.append(msg)
    else:
        rollback_steps.append("新版本目录不存在，无需清理")

    # 2. 恢复 version 文件为旧版本号（如旧版本为空则删除 version 文件）
    version_file = os.path.join(component_dir, "version")
    try:
        if current_version:
            with open(version_file, "w", encoding="utf-8") as vf:
                vf.write(current_version)
            msg = f"version 文件已恢复为: {current_version}"
        else:
            if os.path.isfile(version_file):
                os.remove(version_file)
            msg = "version 文件已删除（旧版本为空）"
        logging.info("[upgrade_task][rollback] %s", msg)
        rollback_steps.append(msg)
    except Exception as e:
        msg = f"恢复 version 文件失败: {e}"
        logging.warning("[upgrade_task][rollback] %s", msg)
        rollback_steps.append(msg)

    # 3. 如果旧进程之前是运行的，重新启动
    if was_running:
        ext = ".bat" if os.name == "nt" else ".sh"
        start_script = os.path.join(old_bin_dir, f"start{ext}")
        if os.path.isfile(start_script):
            logging.info("[upgrade_task][rollback] 重新启动旧版本，执行: %s", start_script)
            restart_exec = _execute_script(start_script, old_bin_dir, min(timeout, 60))
            if restart_exec["success"]:
                msg = f"旧版本已重新启动 (exit_code={restart_exec['exit_code']})"
                logging.info("[upgrade_task][rollback] %s", msg)
            else:
                msg = f"旧版本重启失败 (exit_code={restart_exec['exit_code']}), stderr={restart_exec.get('stderr', '')}"
                logging.error("[upgrade_task][rollback] %s", msg)
            rollback_steps.append(msg)
        else:
            msg = f"旧版本启动脚本不存在，无法重启: {start_script}"
            logging.warning("[upgrade_task][rollback] %s", msg)
            rollback_steps.append(msg)
    else:
        rollback_steps.append("旧版本未运行，无需重启")

    return rollback_steps


# =============================================================================
# 主入口
# =============================================================================

def upgrade_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    升级服务任务（版本比对 → 停止旧版 → 下载 → 安装 → 启动）。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），同时也是组件目录名
        - download_url: 下载地址（必填）
        - file_suffix:  文件后缀，如 .zip（必填）
        - version:      新版本号（必填）

    Nacos 配置从 {service_name}/runtime/config.yaml 读取
    启动/停止脚本从 bin/ 文件夹读取

    流程:
        1. 读取 {download}/{service_name}/version 获取当前版本
        2. 校验新版本号（任务参数 version，必填）
        3. 若当前版本 == 新版本 → 返回 "该版本正在使用"
        4. 若当前版本不同:
           a. 检查 runtime/pid 是否存在 → 执行 bin/stop 脚本停止旧服务
           b. 校验进程已销毁 → 删除 pid 文件
           c. 下载新版本
           d. 安装新版本
           e. 启动新版本
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()
    download_url = str(parameters.get("download_url", "") or "").strip()
    file_suffix = str(parameters.get("file_suffix", "") or "").strip()
    version = str(parameters.get("version", "") or "").strip()

    # ── 参数校验 ──
    missing: List[str] = []
    for key, val in [
        ("task_id", task_id),
        ("service_name", service_name),
        ("download_url", download_url),
        ("file_suffix", file_suffix),
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

    # ── 组件目录 ──
    component_dir = os.path.join(_DOWNLOAD_BASE, service_name)
    steps: List[Dict[str, Any]] = []

    if not os.path.isdir(component_dir):
        return _build_result(
            task_id, False, f"组件目录不存在: {component_dir}",
            data={"service_name": service_name, "component_dir": component_dir},
            error_type="FileNotFoundError",
            error_message=f"组件目录不存在: {component_dir}",
        )

    # ── 新版本号来自任务参数 version（必填，缺失已在上方校验拦截）──
    new_version = version

    # ── Step 1: 读取当前版本，比对 ──
    version_file = os.path.join(component_dir, "version")
    if os.path.isfile(version_file):
        try:
            with open(version_file, "r", encoding="utf-8") as vf:
                current_version = vf.read().strip()
        except Exception as e:
            return _build_result(
                task_id, False, f"读取 version 文件失败: {e}",
                data={"service_name": service_name, "component_dir": component_dir},
                error_type="VersionReadError", error_message=str(e),
            )
    else:
        current_version = ""

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

    # ── Step 2: 停止旧版本服务 ──
    was_running = False   # 回滚标记：旧进程是否原本在运行
    old_bin_dir_for_rollback = ""  # 回滚所需：旧版本 bin 目录
    if current_version:
        old_runtime_dir = os.path.join(component_dir, current_version, "runtime")
        old_pid_file = os.path.join(old_runtime_dir, "pid")
        old_bin_dir = os.path.join(component_dir, current_version, "bin")
        old_bin_dir_for_rollback = old_bin_dir

        pid_list = []
        pid_before = ""
        if os.path.isfile(old_pid_file):
            try:
                with open(old_pid_file, "r", encoding="utf-8") as pf:
                    content = pf.read().strip()
                logging.info("[upgrade_task] 检测到 PID 文件内容: %s", content)
                if content:
                    for line in content.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            pid_list.append(int(line))
                        except ValueError:
                            pass
            except Exception as e:
                logging.warning("[upgrade_task] 读取旧 PID 文件失败: %s", e)
            pid_before = ",".join(str(p) for p in pid_list)

            # 执行 bin/stop 脚本
            ext = ".bat" if os.name == "nt" else ".sh"
            stop_script = os.path.join(old_bin_dir, f"stop{ext}")

            if os.path.isfile(stop_script):
                logging.info("[upgrade_task] 执行停止脚本: %s", stop_script)
                stop_exec = _execute_script(stop_script, old_bin_dir, min(timeout, 60))
                logging.info(
                    "[upgrade_task] 停止脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
                    stop_exec["exit_code"], stop_exec.get("stdout", ""), stop_exec.get("stderr", ""),
                )
                steps.append({
                    "step": "stop_old",
                    "success": stop_exec["success"],
                    "message": "停止旧版本服务" + ("成功" if stop_exec["success"] else f"失败 (exit_code={stop_exec['exit_code']})"),
                    "data": {
                        "pid_before": pid_before,
                        "exit_code": stop_exec["exit_code"],
                        "stdout": stop_exec.get("stdout", ""),
                        "stderr": stop_exec.get("stderr", ""),
                    },
                })

                # ── 校验进程是否已销毁（多 PID 逐一检查）──
                if pid_list:
                    # 停止前记录各 PID 的 starttime，防止 kill 后 PID 被回收复用导致误判
                    pid_start_ticks = {p: _get_process_start_ticks(p) for p in pid_list}
                    still_alive = []
                    for pid_int in pid_list:
                        if _process_exists(pid_int, pid_start_ticks.get(pid_int)):
                            still_alive.append(str(pid_int))
                    if still_alive:
                        return _build_result(
                            task_id, False, f"停止旧版本失败: 进程 {', '.join(still_alive)} 仍然存活",
                            data={
                                "service_name": service_name,
                                "current_version": current_version,
                                "new_version": new_version,
                                "pid": pid_before,
                                "process_still_alive": True,
                                "steps": steps,
                            },
                            error_type="ProcessStillAlive",
                            error_message=f"PID {', '.join(still_alive)} 进程仍然存活，无法升级",
                        )
                    else:
                        logging.info("[upgrade_task] 旧进程 %s 已销毁", pid_before)
                        was_running = True  # 标记旧进程曾运行，回滚时需要重启
                else:
                    # PID 文件存在但无有效 PID，保守起见标记为曾运行
                    logging.info("[upgrade_task] PID 文件无有效 PID，跳过进程校验")
                    was_running = True

                # ── 删除旧 PID 文件 ──
                if os.path.isfile(old_pid_file):
                    try:
                        os.remove(old_pid_file)
                        logging.info("[upgrade_task] 旧 PID 文件已删除: %s", old_pid_file)
                    except Exception as e:
                        logging.warning("[upgrade_task] 删除旧 PID 文件失败: %s", e)
            else:
                logging.error("[upgrade_task] 停止脚本不存在: %s，无法停止运行中的旧版本", stop_script)
                return _build_result(
                    task_id, False, f"停止脚本不存在，无法停止运行中的旧版本 {current_version}: {stop_script}",
                    data={
                        "service_name": service_name,
                        "current_version": current_version,
                        "new_version": new_version,
                        "pid": pid_before,
                        "steps": steps,
                    },
                    error_type="StopScriptNotFound",
                    error_message=f"停止脚本不存在: {stop_script}",
                )
        else:
            logging.info("[upgrade_task] 未找到 PID 文件，旧版本可能未运行，跳过停止步骤")
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

    # ── 销毁旧 version 文件，避免 download_task 读旧版本号误判为已下载 ──
    if current_version:
        version_file = os.path.join(component_dir, "version")
        if os.path.isfile(version_file):
            try:
                os.remove(version_file)
                logging.info("[upgrade_task] 已删除 version 文件: %s", version_file)
                steps.append({
                    "step": "cleanup_old_version_file",
                    "success": True,
                    "message": f"已删除旧 version 文件，原版本号: {current_version}",
                })
            except Exception as e:
                logging.warning("[upgrade_task] 删除 version 文件失败: %s", e)
                steps.append({
                    "step": "cleanup_old_version_file",
                    "success": False,
                    "message": f"删除 version 文件失败: {e}",
                })

    # ── Step 3: 下载新版本 ──
    logging.info("[upgrade_task] 开始下载新版本: %s", new_version)
    download_result = download_task({
        "task_id": task_id,
        "download_url": download_url,
        "file_name": service_name,
        "file_suffix": file_suffix,
        "version": new_version,
    }, retry=retry, timeout=timeout)

    download_ok = _task_success(download_result)
    steps.append({
        "step": "download",
        "success": download_ok,
        "message": _task_message(download_result),
        "data": download_result.get("data", {}),
    })

    if not download_ok:
        rollback_info = _rollback(component_dir, current_version, new_version, was_running, old_bin_dir_for_rollback, timeout)
        return _build_result(
            task_id, False, f"下载新版本失败，已回滚: {_task_message(download_result)}",
            data={
                "service_name": service_name,
                "current_version": current_version,
                "new_version": new_version,
                "steps": steps,
                "rollback": rollback_info,
            },
            error_type="UpgradeDownloadFailed",
            error_message=_task_message(download_result),
        )

    # ── Step 4: 安装新版本 ──
    logging.info("[upgrade_task] 开始安装新版本")
    install_result = install_task({
        "task_id": task_id,
        "file_name": service_name,
    }, retry=retry, timeout=timeout)

    install_ok = _task_success(install_result)
    steps.append({
        "step": "install",
        "success": install_ok,
        "message": _task_message(install_result),
        "data": install_result.get("data", {}),
    })

    if not install_ok:
        rollback_info = _rollback(component_dir, current_version, new_version, was_running, old_bin_dir_for_rollback, timeout)
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

    # ── Step 5: 启动新版本（start_task 从 bin/ 目录读取脚本）──
    logging.info("[upgrade_task] 启动新版本服务")
    start_result = start_task({
        "task_id": task_id,
        "service_name": service_name,
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
        rollback_info = _rollback(component_dir, current_version, new_version, was_running, old_bin_dir_for_rollback, timeout)
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
