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


def _resolve_paths(service_name: str) -> tuple:
    """
    根据 service_name 解析组件路径。

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

    component_dir = os.path.join(_DOWNLOAD_BASE, service_name)
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
    os.makedirs(bin_dir, exist_ok=True)

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

    # 规则2: 写入主进程 PID — 必须有 $! 并且写入 runtime/pid（含变量引用）
    has_pid_capture = bool(re.search(r'\$!', script))
    has_pid_file = bool(re.search(r'runtime.*[/\\]pid', script, re.IGNORECASE))
    if not has_pid_capture or not has_pid_file:
        violations.append(
            "规则2（写入PID）: 必须用 $! 获取主进程 PID 并写入 runtime/pid，"
            '示例: echo $! > "$RUNTIME_DIR/pid"'
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

    # 规则2: 写入主进程 PID — 必须有 runtime 与 \pid 的组合字样
    #  匹配: runtime\pid、runtime/pid、!RUNTIME_DIR!\pid、%RUNTIME_DIR%\pid 等
    if not re.search(r'runtime.*[\\/]pid', script, re.IGNORECASE):
        violations.append(
            r'规则2（写入PID）: 必须将主进程 PID 写入 runtime\pid，'
            r'示例: echo !PID! > "!RUNTIME_DIR!\pid"'
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
# 进程端口查询 & application.yml 更新
# =============================================================================

def _get_listening_ports(pid: int):
    """获取指定进程的 TCP 监听端口列表"""
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


def _update_application_yml(component_dir: str, version: str, pid_str: str) -> Dict[str, Any]:
    """
    用 PID 查询进程监听端口，将 IP 和端口写入 application.yml 的 service 字段。

    流程:
        1. 根据 PID 查询进程 TCP 监听端口
        2. 获取本机 IP
        3. 读取 {component_dir}/{version}/application.yml
        4. 在 service 字段下写入 ip 和 port
        5. 写回文件

    返回:
        {"updated": True/False, "ip": "", "ports": []}
    """
    result = {"updated": False, "ip": "", "ports": []}

    if not pid_str:
        return result

    # 支持多行 PID，取第一个有效值
    pid = None
    for line in pid_str.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid = int(line)
            break
        except ValueError:
            continue
    if pid is None:
        return result

    # 查询进程监听端口
    ports = _get_listening_ports(pid)
    if not ports:
        logging.warning("[start_task] 未检测到进程监听端口: pid=%s", pid)
        return result

    import yaml

    from utils.util import get_ip

    ip = get_ip()
    result["ip"] = ip
    result["ports"] = ports

    # 定位 application.yml
    yml_path = os.path.join(component_dir, version, "application.yml")
    if not os.path.isfile(yml_path):
        logging.warning("[start_task] application.yml 不存在: %s", yml_path)
        return result

    try:
        with open(yml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logging.warning("[start_task] 读取 application.yml 失败: %s", e)
        return result

    if not isinstance(data, dict):
        data = {}

    # 在 service 字段下写入 ip 和 port
    if "service" not in data:
        data["service"] = {}
    data["service"]["ip"] = ip
    data["service"]["port"] = ports[0] if len(ports) == 1 else ports

    try:
        with open(yml_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
        result["updated"] = True
        logging.info("[start_task] application.yml 已更新: service.ip=%s, service.port=%s", ip, ports)
    except Exception as e:
        logging.warning("[start_task] 写入 application.yml 失败: %s", e)

    return result


# =============================================================================
# 主入口
# =============================================================================

def start_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    启动服务任务 — 从 bin/ 目录读取启动脚本并执行。

    参数:
        - task_id:      任务ID（必填）
        - service_name: 服务名称（必填），同时也是组件目录名

    脚本强制规则（执行前校验，不通过则拒绝）:
        1. 后台启动应用（不阻塞终端）
        2. 主进程 PID 写入 {bin_dir}/../runtime/pid
        3. 脚本返回退出码

    流程:
        1. 根据 service_name 在 {base_path}/{service_name}/ 定位组件目录
        2. 读取 version 文件获取版本号
        3. 从 {version}/bin/start.sh 读取启动脚本
        4. 校验脚本是否符合三项规则
        5. 执行启动脚本
        6. 读取 runtime/pid 中的主进程 PID，返回结果
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

    # runtime/pid 路径
    runtime_dir = os.path.join(component_dir, version, "runtime")
    pid_file = os.path.join(runtime_dir, "pid")

    # 确保 runtime 目录存在（组件脚本可能只写 pid 不建目录）
    try:
        os.makedirs(runtime_dir, exist_ok=True)
        logging.info("[start_task] 已确保 runtime 目录存在: %s", runtime_dir)
    except Exception as e:
        return _build_result(
            task_id, False, f"创建 runtime 目录失败: {e}",
            data={"service_name": service_name, "version": version, "runtime_dir": runtime_dir},
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

    # ── 读取 PID ──
    pid = ""
    app_yml_result = {}
    if os.path.isfile(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as pf:
                pid = pf.read().strip()
            logging.info("[start_task] 主进程 PID: %s", pid)
        except Exception as e:
            logging.warning("[start_task] 读取 PID 文件失败: %s", e)

    # ── 查询端口并写入 application.yml ──
    if pid:
        import time as _time
        _time.sleep(1)  # 给进程一点时间绑定端口
        app_yml_result = _update_application_yml(component_dir, version, pid)

    return _build_result(task_id, True, "启动脚本写入并执行成功", data={
        **base_data,
        "status": "started",
        "pid": pid,
        "exit_code": exec_result["exit_code"],
        "stdout": exec_result.get("stdout", ""),
        "app_yml": app_yml_result,
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

    # 将测试脚本写入 bin/ 模拟已安装状态
    test_service = "hellogitworld-master"
    test_bin_dir = os.path.join(_DOWNLOAD_BASE, test_service, "1.0.0", "bin") if _DOWNLOAD_BASE else ""
    if test_bin_dir and not os.path.isfile(os.path.join(test_bin_dir, "start.sh" if os.name != "nt" else "start.bat")):
        os.makedirs(test_bin_dir, exist_ok=True)
        ext = ".bat" if os.name == "nt" else ".sh"
        with open(os.path.join(test_bin_dir, f"start{ext}"), "w", encoding="utf-8") as f:
            f.write(test_script)
        if os.name != "nt":
            os.chmod(os.path.join(test_bin_dir, "start.sh"), 0o755)

    result = start_task(
        {
            "task_id": "test-start-001",
            "service_name": test_service,
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
