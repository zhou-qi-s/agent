"""
启动服务任务模块

基于下载/安装的目录结构，定位组件目录，从 bin/ 读取启动脚本并执行。
"""

import logging
import os
import re
import subprocess
import traceback
from typing import Any, Dict, List, Optional

from utils.config_loader import load_config
from utils.app_path import (
    find_cache_version_dir,
    find_apps_component_dir,
    list_cache_versions,
    get_pid_file,
    read_pid,
    read_pids_from_file,
    read_state,
    write_state,
    wait_process_alive,
    get_process_names,
)

# ── 全局配置 ──
_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

# 启动后等待确认进程存活的秒数（避免"秒退"被误判为启动成功）
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
        "task_id": task_id,
        "task_type": "start",
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


def _resolve_paths(service_name: str, version: str) -> tuple:
    """
    根据 service_name + version 解析启动所需的路径。

    定位规则（缓存区 / 运行区分离后）：
        1. 校验缓存区中存在该版本      {server.download}/{service_name}/{version}/
           —— 不存在则拒绝启动（说明还没下载或版本不对）
        2. 在运行区定位组件目录        {server.apps}/{service_name}/
           —— 由 install 阶段建立软链接结构，
              bin/ 经 current/bin 指向缓存区，脚本从运行区执行

    返回:
        (error, component_dir, version, bin_dir)
        - 出错: (error_result, "", "", "")
        - 正常: (None, 运行区组件目录, version, 运行区 bin 目录)
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

    # ── 1. 校验缓存区版本目录存在 ──
    cache_version_dir = find_cache_version_dir(service_name, version)
    if not cache_version_dir:
        available = list_cache_versions(service_name)
        expected = os.path.join(_DOWNLOAD_BASE, service_name, version)
        return (
            _build_result(
                "", False, f"缓存区中不存在版本 {version}: {expected}",
                data={"service_name": service_name, "version": version,
                      "expected": expected, "available_versions": available},
                error_type="VersionDirNotFound",
                error_message=(f"缓存区中未找到版本 {version}，"
                               f"已下载的版本: {available or '无'}"),
            ),
            "", "", "",
        )

    # ── 2. 在运行区定位组件目录 ──
    if not _APPS_BASE:
        return (
            _build_result(
                "", False, "config.yaml 中未配置 server.apps 运行区路径",
                error_type="ConfigMissing",
                error_message="server.apps 未配置，无法定位运行区组件",
            ),
            "", "", "",
        )

    component_dir = find_apps_component_dir(service_name)
    if not component_dir:
        expected_apps = os.path.join(_APPS_BASE, service_name)
        return (
            _build_result(
                "", False, f"运行区组件不存在: {expected_apps}，请先执行安装",
                data={"service_name": service_name, "version": version,
                      "apps_dir": _APPS_BASE, "expected": expected_apps},
                error_type="AppsComponentNotFound",
                error_message=f"运行区未找到组件 {service_name}，请先执行安装任务",
            ),
            "", "", "",
        )

    # bin 目录：{apps}/{service_name}/bin
    # 这是指向 current/bin 的软链接，而 install 阶段已把 current 指向目标版本，
    # 因此此处实际执行的就是 {version}/bin 下的脚本。
    bin_dir = os.path.join(component_dir, "bin")

    return None, component_dir, version, bin_dir


def _validate_linux_script(script: str) -> List[str]:
    """校验 Linux 脚本是否符合三项规则，返回违规原因列表（空=通过）"""
    violations: List[str] = []

    # 规则1: 后台启动 — 存在 &（行尾）、nohup、setsid、disown、daemon 等
    has_background = (
        bool(re.search(r'&\s*(?:#.*)?$', script, re.MULTILINE))
        or bool(re.search(r'\b(?:nohup|setsid|disown|daemon)\b', script))
    )
    if not has_background:
        violations.append(
            "规则1（后台启动）: 未发现后台启动命令，请在启动命令末尾加 &，或使用 nohup/setsid 等方式"
        )

    # 规则2: 写入主进程 PID — 必须有 $! 并且把 PID 写入 pid 文件
    # 改造后 pid 统一落在运行区 state/ 下（{apps}/{服务}/state/pid），
    # 为兼容历史包仍接受 runtime/pid。
    has_pid_capture = bool(re.search(r'\$!', script))
    has_pid_file = bool(re.search(r'(?:state|runtime).*[/\\]pid', script, re.IGNORECASE))
    if not has_pid_capture or not has_pid_file:
        violations.append(
            "规则2（写入PID）: 必须用 $! 获取主进程 PID 并写入 state/pid，"
            '示例: echo $! > "$STATE_DIR/pid"'
        )

    # 规则3: 返回退出码 — 必须有 exit 语句
    if not re.search(r'\bexit\b', script):
        violations.append("规则3（退出码）: 脚本末尾必须有 exit 语句，如 exit 0")

    return violations


def _validate_windows_script(script: str) -> List[str]:
    """校验 Windows 批处理脚本是否符合三项规则，返回违规原因列表（空=通过）"""
    violations: List[str] = []

    # 规则1: 后台启动 — start /b、start ""、wmic process call create、Start-Process 等
    has_background = bool(re.search(
        r'\bstart\s+/b\b'
        r'|\bstart\s+""'
        r'|\bstart\s+"[^"]*"\b'
        r'|\bStart-Process\b',
        script, re.IGNORECASE,
    ))
    if not has_background:
        violations.append(
            '规则1（后台启动）: 未发现后台启动命令，请使用 start /b、start ""、'
            '或 PowerShell Start-Process -WindowStyle Hidden'
        )

    # 规则2: 写入主进程 PID — 必须有 state/runtime 与 \pid 的组合字样
    #  匹配: state\pid、state/pid、runtime\pid、!STATE_DIR!\pid 等
    #  （改造后 pid 统一落在 state/ 下，为兼容历史包仍接受 runtime/pid）
    if not re.search(r'(?:state|runtime).*[\\/]pid', script, re.IGNORECASE):
        violations.append(
            r'规则2（写入PID）: 必须将主进程 PID 写入 state\pid，'
            r'示例: echo !PID! > "!STATE_DIR!\pid"'
        )

    # 规则3: 返回退出码 — 必须有 exit /b 或 exit
    if not re.search(r'\bexit\b', script):
        violations.append("规则3（退出码）: 脚本末尾必须有 exit /b 0 或 exit 0")

    return violations


def _validate_script(script: str) -> List[str]:
    """校验脚本是否符合三项规则，返回违规原因列表（空列表=通过）"""
    if os.name == "nt":
        return _validate_windows_script(script)
    else:
        return _validate_linux_script(script)


def _read_script(bin_dir: str) -> tuple:
    """
    从 bin/ 目录读取 start.sh（或 start.bat），返回 (script_content, script_path)。
    文件不存在时返回 ("", "")。
    """
    ext = ".bat" if os.name == "nt" else ".sh"
    script_path = os.path.join(bin_dir, f"start{ext}")

    if not os.path.isfile(script_path):
        return "", ""

    # 兼容多种编码：UTF-8 失败回退 GB18030（Windows 编辑器保存的中文注释常为 GBK/GB2312），
    # 避免误报 "未找到启动脚本"（实际是文件读不出）。
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
            logging.error("[start_task] 读取启动脚本失败: %s, %s", script_path, e)
            return "", ""
    return content, script_path


def _execute_script(script_path: str, bin_dir: str, timeout: int) -> Dict[str, Any]:
    """执行启动脚本，返回执行结果。

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
# 进程端口查询
# =============================================================================
# 注：原 _update_application_yml() 已移除。
#     它将监听端口写入组件的 application.yml（组件描述文件），
#     但该文件既无人生成、也无人消费：
#       · Agent 侧 Nacos 注册已改读 runtime/config.yaml（见 nacos_register.py 变更 v3）
#       · 后端平台从未读取该文件（IP/端口一律取自数据库实体 NodeEntity/ApplicationEntity）
#       · /api/component/register 接口后端亦未实现
#     故连同其专用的 _get_listening_ports() 一并删除，避免无用的文件 I/O。


def _get_listening_ports(pid: int):
    """
    获取指定进程的 TCP 监听端口列表（返回数据给调用方使用，不再落盘）。
    """
    try:
        import psutil
        proc = psutil.Process(pid)
        ports = set()
        for conn in proc.net_connections(kind='tcp'):
            if conn.status == 'LISTEN' and conn.laddr:
                ports.add(conn.laddr.port)
        return sorted(ports)
    except Exception:
        return []


# =============================================================================
# 主入口
# =============================================================================

def start_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    启动服务任务 — 从运行区的 bin/ 目录读取启动脚本并执行。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），同时也是组件目录名
        - version:      版本号（必填，后端下发）

    脚本强制规则（执行前校验，不通过则拒绝）:
        1. 后台启动应用（不阻塞终端）
        2. 主进程 PID 写入 state/pid（兼容历史包的 runtime/pid）
        3. 脚本返回退出码

    流程:
        1. 校验缓存区存在该版本目录 {download}/{service_name}/{version}/
        2. 在运行区定位组件目录 {apps}/{service_name}/（install 建立的软链接结构）
        3. 从 {apps}/{service_name}/bin/start.sh 读取启动脚本（经 current 软链接）
        4. 校验脚本是否符合三项规则
        5. 执行启动脚本
        6. 读取 state/pid 中的主进程 PID，等待 60 秒确认存活后写入运行状态
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    service_name = str(parameters.get("service_name", "") or "").strip()
    version = str(parameters.get("version", "") or "").strip()

    # ── 参数校验 ──
    if not task_id:
        return _build_result("", False, "参数缺失: task_id",
                             error_type="ParameterMissing", error_message="task_id 缺失")
    if not service_name:
        return _build_result(task_id, False, "参数缺失: service_name",
                             error_type="ParameterMissing", error_message="service_name 缺失")
    if not version:
        return _build_result(task_id, False, "参数缺失: version",
                             error_type="ParameterMissing",
                             error_message="version 参数缺失，启动需明确指定版本")

    # ── 路径解析 ──
    error, component_dir, version, bin_dir = _resolve_paths(service_name, version)
    if error:
        error["task_id"] = task_id
        error["data"] = {
            **(error.get("data") or {}),
            "service_name": service_name,
        }
        return error

    logging.info("[start_task] 组件目录: %s, 版本: %s, bin目录: %s", component_dir, version, bin_dir)

    # ── 从 bin/ 读取启动脚本 ──
    script_content, script_path = _read_script(bin_dir)
    if not script_content:
        return _build_result(
            task_id, False, f"启动脚本不存在: {bin_dir}/start.{'bat' if os.name == 'nt' else 'sh'}",
            data={"service_name": service_name, "version": version, "bin_dir": bin_dir},
            error_type="ScriptNotFound",
            error_message=f"未找到启动脚本: {bin_dir}/start.*",
        )

    logging.info("[start_task] 启动脚本已读取: %s", script_path)

    # ── 脚本规则校验 ──
    violations = _validate_script(script_content)
    if violations:
        return _build_result(
            task_id, False, f"脚本不符合启动规则（{len(violations)}项）",
            data={
                "service_name": service_name,
                "version": version,
                "bin_dir": bin_dir,
                "violations": violations,
            },
            error_type="ScriptValidationError",
            error_message="; ".join(violations),
        )

    # ── runtime / pid 路径 ──
    # runtime 属运行态产物（日志等），落在运行区而非缓存区
    runtime_dir = os.path.join(_APPS_BASE, service_name, "runtime")
    # pid 文件放在 state/ 下，与状态文件统一管理：{apps}/{service_name}/state/pid
    # 由包内 start.sh 负责写入（echo $! > .../state/pid）
    pid_file = get_pid_file(service_name) or os.path.join(
        _APPS_BASE, service_name, "state", "pid")

    # 预先创建 runtime 与 state 目录（脚本可能只写文件不建目录）
    try:
        os.makedirs(runtime_dir, exist_ok=True)
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        logging.info("[start_task] 已确保 runtime/state 目录存在: %s | %s",
                     runtime_dir, os.path.dirname(pid_file))
    except Exception as e:
        return _build_result(
            task_id, False, f"创建 runtime/state 目录失败: {e}",
            data={"service_name": service_name, "version": version,
                  "runtime_dir": runtime_dir},
            error_type="RuntimeDirCreateError",
            error_message=str(e),
        )

    # ── 执行启动脚本 ──
    base_data = {
        "service_name": service_name,
        "version": version,
        "component_dir": component_dir,
        "bin_dir": bin_dir,
        "script_path": script_path,
        "runtime_dir": runtime_dir,
        "pid_file": pid_file,
    }
    exec_timeout = min(timeout, 60)  # 脚本自身应在数秒内返回
    exec_result = _execute_script(script_path, bin_dir, exec_timeout)
    logging.info(
        "[start_task] 脚本执行完成, exit_code=%s, stdout=%s, stderr=%s",
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

    # ── 读取脚本写入的 PID（支持多值）──
    pids = read_pids_from_file(service_name)
    if not pids:
        return _build_result(
            task_id, False, f"启动脚本未写入 pid 文件: {pid_file}",
            data={**base_data, "exit_code": exec_result["exit_code"],
                  "stdout": exec_result.get("stdout", "")},
            error_type="PidFileMissing",
            error_message="启动脚本执行完成但未找到 pid 文件，请检查 start.sh 是否写入 $!",
        )
    logging.info("[start_task] 读取到 PID 列表: %s，等待 %s 秒确认存活",
                 pids, START_CONFIRM_WAIT)

    # ── 等待并确认进程存活（多 pid 全部存活才算通过）──
    # 同步等待：避免"秒退"被误判为启动成功，平台拿到的是终态
    alive = wait_process_alive(service_name, pids, START_CONFIRM_WAIT)
    if not alive:
        return _build_result(
            task_id, False,
            f"进程 {pids} 在 {START_CONFIRM_WAIT} 秒内已有退出，启动失败",
            data={**base_data, "pids": pids, "exit_code": exec_result["exit_code"],
                  "stdout": exec_result.get("stdout", ""),
                  "stderr": exec_result.get("stderr", "")},
            error_type="ProcessNotAlive",
            error_message=f"启动后 {START_CONFIRM_WAIT} 秒内进程未全部存活",
        )
    logging.info("[start_task] PID %s 存活确认通过", pids)

    # ── 写入运行状态：pids + processes + runtime=true ──
    processes = get_process_names(pids)
    state = read_state(service_name)
    state.update({
        "pids": pids,
        "processes": processes,
        "name": service_name,
        "runtime": True,
        "version": version,
    })
    # 清理旧的单值字段，避免两份数据并存
    state.pop("pid", None)

    if not write_state(service_name, state):
        logging.warning("[start_task] 写入状态文件失败，但不影响服务已启动的事实")
    logging.info("[start_task] 运行状态已写入: pids=%s processes=%s", pids, processes)

    # ── 探测监听端口（仅用于上报展示，不再写文件）──
    listen_ports = _get_listening_ports(pids[0]) if pids else []
    if listen_ports:
        logging.info("[start_task] 监听端口: %s", listen_ports)

    return _build_result(task_id, True, "启动成功", data={
        **base_data,
        "status": "started",
        "pids": pids,
        "processes": processes,
        "listen_ports": listen_ports,
        "runtime": True,
        "state": state,
        "exit_code": exec_result["exit_code"],
        "stdout": exec_result.get("stdout", ""),
    })


# ── 自测入口 ──

if __name__ == "__main__":

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if os.name == "nt":
        test_script = (
            "@echo off\n"
            "chcp 65001 >nul\n"
            "setlocal enabledelayedexpansion\n"
            'set "RUNTIME_DIR=%~dp0..\\runtime"\n'
            'if not exist "!RUNTIME_DIR!" mkdir "!RUNTIME_DIR!"\n'
            "powershell -Command \"$p=Start-Process -FilePath 'cmd' -ArgumentList '/c ping -n 6 127.0.0.1 > nul' -WindowStyle Hidden -PassThru; $p.Id | Out-File -FilePath '!RUNTIME_DIR!\\pid' -Encoding ascii -NoNewline\"\n"
            "exit /b 0\n"
        )
    else:
        test_script = (
            "#!/bin/bash\n"
            "set -e\n"
            'BIN_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'RUNTIME_DIR="$BIN_DIR/../runtime"\n'
            'mkdir -p "$RUNTIME_DIR"\n'
            "sleep 5 &\n"
            'echo $! > "$RUNTIME_DIR/pid"\n'
            "exit 0\n"
        )

    # 模拟已下载 + 已安装状态：
    #   缓存区写入 {download}/{service}/{version}/bin/start.sh
    #   运行区建立 {apps}/{service}/bin -> current/bin 软链接
    test_service = "hellogitworld-master"
    test_version = "1.0.0"
    ext = ".bat" if os.name == "nt" else ".sh"

    if _DOWNLOAD_BASE:
        cache_bin = os.path.join(_DOWNLOAD_BASE, test_service, test_version, "bin")
        os.makedirs(cache_bin, exist_ok=True)
        script_file = os.path.join(cache_bin, f"start{ext}")
        if not os.path.isfile(script_file):
            with open(script_file, "w", encoding="utf-8", newline="\n") as f:
                f.write(test_script)
            if os.name != "nt":
                os.chmod(script_file, 0o755)

    if _APPS_BASE:
        apps_component = os.path.join(_APPS_BASE, test_service)
        os.makedirs(apps_component, exist_ok=True)
        current_link = os.path.join(apps_component, "current")
        if not os.path.islink(current_link):
            try:
                os.symlink(os.path.join(_DOWNLOAD_BASE, test_service, test_version), current_link)
            except OSError:
                pass
        bin_link = os.path.join(apps_component, "bin")
        if not os.path.islink(bin_link):
            try:
                os.symlink(os.path.join("current", "bin"), bin_link)
            except OSError:
                pass

    result = start_task(
        {
            "task_id": "test-start-001",
            "service_name": test_service,
            "version": test_version,
        },
    )

    print("\n" + "=" * 60)
    print("  启动任务结果")
    print("=" * 60)
    print(f"  task_id : {result.get('task_id', '')}")
    print(f"  成功    : {result['success']}")
    print(f"  消息    : {result['message']}")
    data = result.get("data", {})
    if data:
        for key in ("service_name", "version", "status", "pid", "exit_code", "bin_dir", "script_path", "pid_file"):
            print(f"  {key}: {data.get(key, 'N/A')}")
    if not result["success"]:
        err = result.get("error", {})
        print(f"  错误类型: {err.get('error_type', '')}")
        print(f"  错误信息: {err.get('error_message', '')}")
    print("=" * 60)
