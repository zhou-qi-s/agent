"""
core/utils.py - 任务执行工具函数模块

本模块提供任务执行相关的工具函数，包括：
1. Nacos服务注册/注销/心跳管理
2. 进程管理（启动、停止、监控）
3. 脚本文件操作（创建、执行、清理）
4. 任务结果构建和格式化
5. 辅助工具函数（MD5校验、编码处理等）

主要功能：
- Nacos服务注册与发现
- 子进程管理和端口监控
- 脚本生成和执行
- 任务结果标准化
"""

import hashlib
import json
import locale
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple



import psutil
import requests

from utils.config_loader import load_config

# =============================================================================
# 配置加载与常量定义
# =============================================================================

# 加载配置，获取Nacos API路径
_config = load_config()
_nacos_cfg = _config.get("nacos", {})
_api_cfg = _nacos_cfg.get("api", {})

# Nacos API路径（从配置文件读取，使用默认值）
# 这些路径用于与Nacos服务器通信，实现服务注册、心跳、查询等功能
NACOS_API_REGISTER = _api_cfg.get("register", "/nacos/v1/ns/instance")          # 服务注册接口
NACOS_API_DEREGISTER = _api_cfg.get("deregister", "/nacos/v1/ns/instance")      # 服务注销接口
NACOS_API_HEARTBEAT = _api_cfg.get("heartbeat", "/nacos/v1/ns/instance/beat")   # 心跳保活接口
NACOS_API_INSTANCE_LIST = _api_cfg.get("instance_list", "/nacos/v1/ns/instance/list")  # 实例列表查询
NACOS_API_SERVICE_LIST = _api_cfg.get("service_list", "/nacos/v1/ns/service/list")     # 服务列表查询
NACOS_API_SERVICE_DETAIL = _api_cfg.get("service_detail", "/nacos/v1/ns/service")      # 服务详情查询


# =============================================================================
# 参数校验与处理函数
# =============================================================================

def validate_params(params: Dict[str, Any]) -> Tuple[str, str, str]:

    """
    校验启动任务的必要参数
    
    参数:
        params: 任务参数字典，必须包含:
            - install_location: 安装目录路径
            - start_script: 启动脚本内容
            - service_name: 服务名称
    
    返回:
        tuple: (install_location, start_script, service_name)
    
    异常:
        ValueError: 缺少必要参数
        FileNotFoundError: 安装目录不存在
    """
    install_location = params.get("install_location")
    start_script = params.get("start_script")
    service_name = params.get("service_name")

    if not install_location or not start_script or not service_name:
        raise ValueError("缺少必要参数: install_location/start_script/service_name")

    if not os.path.isdir(install_location):
        raise FileNotFoundError(f"目录不存在: {install_location}")

    return install_location, start_script, service_name


def normalize_timeout(timeout: Any, default: int = 300) -> float:
    """
    规范化超时时间
    
    处理逻辑:
        1. 如果timeout <= 0，使用默认值
        2. 如果转换失败，使用默认值
    
    参数:
        timeout: 原始超时时间（秒）
        default: 默认超时时间（秒）
    
    返回:
        float: 规范化后的超时时间（秒）
    """
    try:
        effective_timeout = float(timeout)
    except (TypeError, ValueError):
        effective_timeout = float(default)

    # 如果小于等于0，使用默认值
    if effective_timeout <= 0:
        effective_timeout = float(default)

    return effective_timeout


def ensure_text_content(content: Any) -> str:
    """
    确保内容为字符串类型
    
    参数:
        content: 任意类型的内容
    
    返回:
        str: 字符串形式的内容
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return str(content)


def sanitize_task_id(task_id: Any, default_name: str) -> str:
    """
    清理任务ID，确保文件名安全
    
    将非字母数字字符替换为下划线，避免文件名非法字符
    
    参数:
        task_id: 原始任务ID
        default_name: 默认名称（当task_id为空时使用）
    
    返回:
        str: 清理后的安全任务ID
    """
    raw_task_id = str(task_id or "").strip()
    safe_task_id = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw_task_id)
    return safe_task_id or default_name


# =============================================================================
# 脚本文件操作函数
# =============================================================================

def build_script_path(base_dir: str, prefix: str, task_id: Any, default_name: str) -> str:
    """
    构建脚本文件路径
    
    根据操作系统自动选择.bat或.sh扩展名
    
    参数:
        base_dir: 基础目录
        prefix: 文件名前缀（如start/stop/install）
        task_id: 任务ID
        default_name: 默认名称
    
    返回:
        str: 完整的脚本文件路径
    """
    script_ext = ".bat" if os.name == "nt" else ".sh"
    safe_task_id = sanitize_task_id(task_id, default_name)
    return os.path.join(base_dir, f"{prefix}_{safe_task_id}{script_ext}")


def sanitize_start_script_content(content: str) -> str:
    """清理启动脚本中不适合后台托管执行的内容。"""
    normalized_content = ensure_text_content(content)
    if os.name != "nt":
        return normalized_content

    sanitized_lines: List[str] = []
    for line in normalized_content.splitlines():
        stripped_line = line.strip()
        normalized_line = stripped_line.lstrip("@").strip().lower()
        if normalized_line == "pause" or normalized_line.startswith("pause "):
            continue
        sanitized_lines.append(line)

    return "\n".join(sanitized_lines)



def write_start_script(install_location: str, content: str) -> str:
    """
    写入启动脚本到安装目录
    
    参数:
        install_location: 安装目录路径
        content: 脚本内容
    
    返回:
        str: 脚本文件完整路径
    """
    ext = ".bat" if os.name == "nt" else ".sh"
    script_path = os.path.join(install_location, f"start{ext}")
    write_task_script(script_path, sanitize_start_script_content(content))
    return script_path



def write_task_script(script_path: str, content: str) -> str:
    """
    写入任务脚本到指定路径
    
    根据操作系统自动处理换行符:
        - Windows: \r\n
        - Linux/Mac: \n
    
    非Windows系统会自动添加执行权限(755)
    
    参数:
        script_path: 脚本文件路径
        content: 脚本内容
    
    返回:
        str: 脚本文件路径
    """
    line_ending = "\r\n" if os.name == "nt" else "\n"
    with open(script_path, "w", encoding="utf-8", newline=line_ending) as f:
        f.write(ensure_text_content(content))

    # 非Windows系统添加执行权限
    if os.name != "nt":
        os.chmod(script_path, 0o755)

    return script_path


def cleanup_temp_script(script_path: str, log_prefix: str) -> Dict[str, Any]:
    """
    清理临时脚本文件
    
    参数:
        script_path: 脚本文件路径
        log_prefix: 日志前缀，用于标识操作来源
    
    返回:
        dict: 清理结果，包含:
            - success: 是否成功
            - error: 错误信息（如果有）
            - removed: 是否实际删除了文件
    """
    if not os.path.exists(script_path):
        return {
            "success": True,
            "error": "",
            "removed": False
        }

    try:
        os.remove(script_path)
        logging.info(f"{log_prefix} 已删除临时脚本: {script_path}")
        return {
            "success": True,
            "error": "",
            "removed": True
        }
    except Exception as e:
        logging.warning(f"{log_prefix} 删除临时脚本失败: {e}")
        return {
            "success": False,
            "error": str(e),
            "removed": False
        }


def build_shell_command(script_path: str) -> List[str]:
    """
    构建执行脚本的命令列表
    
    根据操作系统返回不同的命令格式:
        - Windows: ["cmd", "/c", script_path]
        - Linux/Mac: ["/bin/sh", script_path]
    
    参数:
        script_path: 脚本文件路径
    
    返回:
        list: 命令列表，可直接用于subprocess
    """
    return ["cmd", "/c", script_path] if os.name == "nt" else ["/bin/sh", script_path]


# =============================================================================
# 进程管理函数
# =============================================================================

def launch_script_process(
    command: List[str],
    cwd: str,
    env: Optional[Dict[str, str]] = None,
    text: bool = True
) -> subprocess.Popen[Any]:
    """
    启动脚本进程
    
    参数:
        command: 命令列表
        cwd: 工作目录
        env: 环境变量字典（可选）
        text: 是否以文本模式处理输出
    
    返回:
        subprocess.Popen: 进程对象
    """
    kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": text,
        "encoding": "utf-8",
        "errors": "replace"
    }
    if env is not None:
        kwargs["env"] = env
    if os.name == "nt":
        # Windows下不创建新窗口
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, **kwargs)


def launch_process(script_path: str, cwd: str, extra_env: Optional[Dict[str, str]] = None) -> subprocess.Popen[Any]:
    """
    启动脚本进程
    
    参数:
        script_path: 脚本文件路径
        cwd: 工作目录
        extra_env: 额外环境变量（可选）
    
    返回:
        subprocess.Popen: 进程对象
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return launch_script_process(build_shell_command(script_path), cwd=cwd, env=env, text=True)


def get_child_processes(launcher: subprocess.Popen[Any]) -> List[psutil.Process]:
    """
    获取启动器的所有子进程
    
    使用psutil获取launcher进程的所有子进程（递归）
    
    参数:
        launcher: 启动器进程对象
    
    返回:
        list: 子进程列表
    """
    try:
        parent = psutil.Process(launcher.pid)
        return parent.children(recursive=True)
    except Exception:
        return []


def get_listen_ports(proc: psutil.Process) -> List[int]:
    """
    获取进程监听的端口列表
    
    通过psutil获取进程的网络连接，筛选出LISTEN状态的端口
    
    参数:
        proc: psutil进程对象
    
    返回:
        list: 监听的端口号列表
    """
    ports = set()
    try:
        for conn in proc.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN:
                ports.add(conn.laddr.port)
    except (psutil.AccessDenied, psutil.NoSuchProcess, Exception):
        # Windows 下可能需要管理员权限才能获取其他进程的网络连接
        # 尝试通过命令行获取
        try:
            ports = _get_ports_by_pid(proc.pid)
        except Exception:
            pass
    return list(ports)


def _get_ports_by_pid(pid: int) -> Set[int]:
    """
    通过系统命令获取指定进程监听的端口（备选方案）
    
    参数:
        pid: 进程ID
    
    返回:
        set: 监听的端口号集合
    """
    ports = set()
    try:
        if os.name == 'nt':
            # Windows: 使用 netstat 命令
            import subprocess
            result = subprocess.run(
                ['netstat', '-ano'],
                capture_output=True,
                text=True,
                timeout=5
            )
            for line in result.stdout.split('\n'):
                if str(pid) in line and 'LISTENING' in line:
                    # 格式: TCP    0.0.0.0:9000    0.0.0.0:0    LISTENING    1234
                    parts = line.split()
                    if len(parts) >= 2:
                        local_addr = parts[1]
                        if ':' in local_addr:
                            port_str = local_addr.split(':')[-1]
                            try:
                                ports.add(int(port_str))
                            except ValueError:
                                pass
        else:
            # Linux/Mac: 使用 ss 或 netstat 命令
            import subprocess
            try:
                result = subprocess.run(
                    ['ss', '-tlnp'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
            except FileNotFoundError:
                result = subprocess.run(
                    ['netstat', '-tlnp'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
            for line in result.stdout.split('\n'):
                if f'pid={pid}' in line or f'/{pid} ' in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        local_addr = parts[3]
                        if ':' in local_addr:
                            port_str = local_addr.split(':')[-1]
                            try:
                                ports.add(int(port_str))
                            except ValueError:
                                pass
    except Exception:
        pass
    return ports


def _get_pid_file_path(install_location: str, service_name: str) -> str:
    """生成 PID 文件路径，约定为 {install_location}/{service_name}.pid"""
    return os.path.join(install_location, f"{service_name}.pid")


def _collect_service_processes(
    launcher: subprocess.Popen[Any],
    pid_file_path: str = "",
    install_location: str = "",
    service_name: str = ""
) -> List[psutil.Process]:
    """收集服务相关进程，优先级：launcher子进程 > PID文件 > 全局匹配"""
    process_map: Dict[int, psutil.Process] = {}

    # 1. 从 launcher 子进程获取（launcher 未退出时的快速路径）
    try:
        launcher_proc = psutil.Process(launcher.pid)
        process_map[launcher_proc.pid] = launcher_proc
        for child in launcher_proc.children(recursive=True):
            process_map[child.pid] = child
    except Exception:
        pass

    # 2. 从 PID 文件读取（launcher 退出后，后台守护进程的精确查找）
    if pid_file_path and os.path.exists(pid_file_path):
        try:
            with open(pid_file_path, "r") as f:
                content = f.read().strip()
            if content:
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pid = int(line)
                    except ValueError:
                        continue
                    try:
                        proc = psutil.Process(pid)
                        if proc.is_running():
                            process_map[pid] = proc
                    except psutil.NoSuchProcess:
                        logging.warning(f"[_collect_service_processes] PID文件中的进程已不存在: pid={pid}")
        except Exception as e:
            logging.warning(f"[_collect_service_processes] 读取PID文件失败: {pid_file_path}, {e}")

    # 3. 兜底：通过 install_location/service_name 全局扫描匹配
    if install_location or service_name:
        for proc in find_matching_processes(install_location, service_name):
            if proc.pid != os.getpid():
                process_map[proc.pid] = proc

    return list(process_map.values())



def wait_for_service_ready(
    launcher: subprocess.Popen[Any],
    timeout: int,
    health_check_url: Optional[str] = None,
    pid_file_path: str = "",
    install_location: str = "",
    service_name: str = ""
) -> Tuple[List[psutil.Process], List[int]]:

    """
    等待服务启动成功
    
    成功标准（满足以下所有条件）:
        1. 找到服务相关进程
        2. 检测到监听端口
        3. （可选）健康检查URL返回200
    
    参数:
        launcher: 启动器进程
        timeout: 超时时间（秒）
        health_check_url: 健康检查URL（可选）
        pid_file_path: PID 文件路径（后台守护模式通过PID文件定位进程）
        install_location: 安装目录（用于兜底匹配进程）
        service_name: 服务名称（用于兜底匹配进程）
    
    返回:
        tuple: (children, ports) 服务进程列表和监听端口列表
    
    异常:
        TimeoutError: 服务启动超时
        RuntimeError: 启动脚本异常退出
    """
    start = time.time()
    latest_stdout = ""
    latest_stderr = ""
    launcher_output_collected = False

    while time.time() - start < timeout:
        candidates = _collect_service_processes(launcher, pid_file_path, install_location, service_name)
        ready_processes: List[psutil.Process] = []
        ready_ports = set()

        for proc in candidates:
            ports = get_listen_ports(proc)
            if not ports:
                continue
            ready_processes.append(proc)
            ready_ports.update(ports)

        if ready_processes:
            if health_check_url:
                try:
                    r = requests.get(health_check_url, timeout=2)
                    if r.status_code != 200:
                        time.sleep(1)
                        continue
                except Exception:
                    time.sleep(1)
                    continue

            # 服务已就绪（有进程和端口），立即返回成功
            # 不再等待脚本退出，避免 start /B 等场景下脚本阻塞问题
            return ready_processes, sorted(ready_ports)

        # 脚本已退出但未检测到服务进程，记录输出用于错误诊断
        if launcher.poll() is not None and not launcher_output_collected:
            try:
                stdout_text, stderr_text = launcher.communicate(timeout=5)
                latest_stdout = decode_process_output(stdout_text)
                latest_stderr = decode_process_output(stderr_text)
                launcher_output_collected = True

                if not candidates:
                    # 后台守护模式：脚本正常退出（返回码0），等待PID文件出现
                    if pid_file_path and launcher.returncode == 0:
                        logging.info(
                            "[wait_for_service_ready] 启动脚本已退出(返回码0)，服务为后台守护模式，"
                            "等待 PID 文件 %s 出现...", pid_file_path
                        )
                    else:
                        raise RuntimeError(
                            latest_stderr or latest_stdout or f"启动脚本已退出，返回码: {launcher.returncode}"
                        )
            except subprocess.TimeoutExpired:
                # 获取输出超时，继续等待服务就绪
                pass

        time.sleep(1)

    if launcher.poll() is not None and not launcher_output_collected:
        stdout_text, stderr_text = launcher.communicate(timeout=1)
        latest_stdout = decode_process_output(stdout_text)
        latest_stderr = decode_process_output(stderr_text)

    if latest_stderr or latest_stdout:
        raise TimeoutError(f"服务启动超时，脚本输出: {latest_stderr or latest_stdout}")

    raise TimeoutError("服务启动超时")



# =============================================================================
# Nacos服务管理函数
# =============================================================================

def register_to_nacos(
    nacos_addr: str,
    service_name: str,
    ip: str,
    ports: List[int],
    group_name: str = "DEFAULT_GROUP",
    namespace_id: str = "",
    cluster_name: str = "DEFAULT",
    metadata: Optional[Dict[str, Any]] = None,
    ephemeral: bool = True
) -> Tuple[List[int], List[Tuple[int, str]]]:

    """
    注册服务实例到Nacos
    
    将服务的多个端口注册为Nacos服务实例，使其他服务可以发现和调用
    
    参数:
        nacos_addr: Nacos服务器地址，如 http://192.168.1.100:8848
        service_name: 服务名称
        ip: 实例IP地址
        ports: 要注册的端口列表
        group_name: 分组名称（默认DEFAULT_GROUP）
        namespace_id: 命名空间ID（默认空，使用public）
        cluster_name: 集群名称（默认DEFAULT）
        metadata: 元数据字典（默认None）
        ephemeral: 是否临时实例（默认True）
    
    返回:
        tuple: (success_ports, failed_ports)
            - success_ports: 成功注册的端口列表
            - failed_ports: 失败的端口列表，格式为 [(port, error_message), ...]
    
    异常:
        RuntimeError: 所有端口注册失败时抛出
    """
    # 使用配置文件中的注册接口路径
    register_api = NACOS_API_REGISTER
    url = f"{nacos_addr.rstrip('/')}{register_api}"
    
    success = []
    failed = []

    for port in ports:
        payload = {
            "serviceName": service_name,
            "ip": ip,
            "port": port,
            "groupName": group_name,
            "clusterName": cluster_name,
            "ephemeral": str(ephemeral).lower(),
            "healthy": "true",
            "enabled": "true",
            "weight": "1.0"
        }
        
        # 添加命名空间（如果提供）
        if namespace_id:
            payload["namespaceId"] = namespace_id
        
        # 添加元数据（如果提供）
        if metadata:
            payload["metadata"] = json.dumps(metadata, ensure_ascii=False)

        try:
            r = requests.post(url, data=payload, timeout=5)
            if r.status_code == 200:
                success.append(port)
                logging.info(f"[Nacos注册] 成功: {service_name}@{ip}:{port}")
            else:
                failed.append((port, r.text))
                logging.warning(f"[Nacos注册] 失败: {service_name}@{ip}:{port}, 错误: {r.text}")
        except Exception as e:
            failed.append((port, str(e)))
            logging.error(f"[Nacos注册] 异常: {service_name}@{ip}:{port}, 错误: {e}")

    if not success:
        raise RuntimeError(f"Nacos 注册失败: {failed}")

    return success, failed


def start_heartbeat(
    nacos_addr: str,
    service_name: str,
    ip: str,
    ports: List[int],
    interval: int = 5,
    group_name: str = "DEFAULT_GROUP",
    namespace_id: str = "",
    cluster_name: str = "DEFAULT"
) -> None:
    """
    [已弃用] 为多个端口启动心跳线程
    
    警告: 此函数已弃用，请使用 core.task_utils._start_nacos_heartbeats 替代
    
    新机制提供更完善的功能:
        - 进程监控（进程退出自动停止心跳）
        - 优雅停止机制（使用threading.Event）
        - 心跳状态跟踪
        - 统一的错误处理
    
    参数:
        nacos_addr: Nacos服务器地址
        service_name: 服务名称
        ip: 实例IP
        ports: 端口列表
        interval: 心跳间隔（秒，默认5）
        group_name: 分组名称（默认DEFAULT_GROUP）
        namespace_id: 命名空间ID（默认空）
        cluster_name: 集群名称（默认DEFAULT）
    """
    import warnings
    warnings.warn(
        "start_heartbeat is deprecated. Use _start_nacos_heartbeats from core.task_utils instead.",
        DeprecationWarning,
        stacklevel=2
    )
    
    logging.warning("[DEPRECATED] start_heartbeat 已弃用，请使用 _start_nacos_heartbeats")
    
    # 使用配置文件中的心跳接口路径
    heartbeat_api = NACOS_API_HEARTBEAT
    base_url = f"{nacos_addr.rstrip('/')}{heartbeat_api}"

    def beat_loop(port):
        """单个端口的心跳循环"""
        while True:
            try:
                # 构建beat信息JSON
                beat_info = {
                    "ip": ip,
                    "port": port,
                    "serviceName": service_name,
                    "cluster": cluster_name,
                    "scheduled": True
                }
                
                payload = {
                    "serviceName": service_name,
                    "groupName": group_name,
                    "clusterName": cluster_name,
                    "ephemeral": "true",
                    "beat": json.dumps(beat_info, ensure_ascii=False)
                }
                
                # 添加命名空间（如果提供）
                if namespace_id:
                    payload["namespaceId"] = namespace_id

                r = requests.put(base_url, params=payload, timeout=3)

                if r.status_code != 200:
                    logging.warning(f"[Nacos心跳] 异常: port={port}, resp={r.text}")

            except Exception as e:
                logging.error(f"[Nacos心跳] 失败: port={port}, err={e}")

            time.sleep(interval)

    for port in ports:
        t = threading.Thread(target=beat_loop, args=(port,), daemon=True)
        t.start()
        logging.info(f"[Nacos心跳] 已启动: {service_name}@{ip}:{port}, 间隔={interval}s")


def deregister_from_nacos(
    nacos_addr: str,
    service_name: str,
    ip: str,
    port: int,
    group_name: str = "DEFAULT_GROUP",
    namespace_id: str = "",
    cluster_name: str = "DEFAULT",
    ephemeral: bool = True
) -> bool:
    """
    从Nacos注销服务实例
    
    服务停止时调用此函数，将实例从Nacos服务列表中移除
    
    参数:
        nacos_addr: Nacos服务器地址
        service_name: 服务名称
        ip: 实例IP
        port: 端口
        group_name: 分组名称（默认DEFAULT_GROUP）
        namespace_id: 命名空间ID（默认空）
        cluster_name: 集群名称（默认DEFAULT）
        ephemeral: 是否临时实例（默认True）
    
    返回:
        bool: 注销是否成功
    """
    # 使用配置文件中的注销接口路径
    deregister_api = NACOS_API_DEREGISTER
    url = f"{nacos_addr.rstrip('/')}{deregister_api}"
    
    payload = {
        "serviceName": service_name,
        "ip": ip,
        "port": port,
        "groupName": group_name,
        "clusterName": cluster_name,
        "ephemeral": str(ephemeral).lower()
    }
    
    # 添加命名空间（如果提供）
    if namespace_id:
        payload["namespaceId"] = namespace_id
    
    try:
        r = requests.delete(url, params=payload, timeout=5)
        if r.status_code == 200:
            logging.info(f"[Nacos注销] 成功: {service_name}@{ip}:{port}")
            return True
        else:
            logging.warning(f"[Nacos注销] 失败: {service_name}@{ip}:{port}, 错误: {r.text}")
            return False
    except Exception as e:
        logging.error(f"[Nacos注销] 异常: {service_name}@{ip}:{port}, 错误: {e}")
        return False


def list_nacos_instances(
    nacos_addr: str,
    service_name: str,
    group_name: str = "DEFAULT_GROUP",
    namespace_id: str = "",
    healthy_only: bool = False
) -> Optional[Dict[str, Any]]:
    """
    查询Nacos服务实例列表
    
    获取指定服务的所有注册实例信息
    
    参数:
        nacos_addr: Nacos服务器地址
        service_name: 服务名称
        group_name: 分组名称（默认DEFAULT_GROUP）
        namespace_id: 命名空间ID（默认空）
        healthy_only: 是否只返回健康实例（默认False）
    
    返回:
        dict: 实例列表数据，格式为:
            {
                "hosts": [
                    {
                        "ip": "xxx",
                        "port": xxx,
                        "healthy": true/false,
                        ...
                    }
                ],
                ...
            }
        失败返回None
    """
    # 使用配置文件中的查询接口路径
    list_api = NACOS_API_INSTANCE_LIST
    url = f"{nacos_addr.rstrip('/')}{list_api}"
    
    params = {
        "serviceName": service_name,
        "groupName": group_name,
        "healthyOnly": str(healthy_only).lower()
    }
    
    # 添加命名空间（如果提供）
    if namespace_id:
        params["namespaceId"] = namespace_id
    
    try:
        r = requests.get(url, params=params, timeout=5)
        if r.status_code == 200:
            return r.json()
        else:
            logging.warning(f"[Nacos查询] 失败: {service_name}, 错误: {r.text}")
            return None
    except Exception as e:
        logging.error(f"[Nacos查询] 异常: {service_name}, 错误: {e}")
        return None


def get_nacos_api_paths() -> Dict[str, str]:
    """
    获取当前使用的Nacos API路径配置
    
    返回:
        dict: API路径配置字典，包含:
            - register: 注册接口路径
            - deregister: 注销接口路径
            - heartbeat: 心跳接口路径
            - instance_list: 实例列表接口路径
            - service_list: 服务列表接口路径
            - service_detail: 服务详情接口路径
    """
    return {
        "register": NACOS_API_REGISTER,
        "deregister": NACOS_API_DEREGISTER,
        "heartbeat": NACOS_API_HEARTBEAT,
        "instance_list": NACOS_API_INSTANCE_LIST,
        "service_list": NACOS_API_SERVICE_LIST,
        "service_detail": NACOS_API_SERVICE_DETAIL
    }


# =============================================================================
# 编码与数据处理函数
# =============================================================================

def decode_process_output(output: Any) -> str:
    """
    解码进程输出（处理多种编码）
    
    尝试多种编码方式解码字节数据，优先顺序:
        1. 系统首选编码
        2. UTF-8
        3. GBK
        4. CP936
    
    参数:
        output: 进程输出（可以是str或bytes）
    
    返回:
        str: 解码后的字符串
    """
    if output is None:
        return ""
    if isinstance(output, str):
        return output.strip()

    preferred_encoding = locale.getpreferredencoding(False) or "utf-8"
    tried_encodings = []
    for encoding in (preferred_encoding, "utf-8", "gbk", "cp936"):
        normalized = (encoding or "").strip().lower()
        if normalized and normalized not in tried_encodings:
            tried_encodings.append(normalized)
            try:
                return output.decode(encoding).strip()
            except UnicodeDecodeError:
                continue

    # 如果都失败，使用替换模式
    return output.decode(preferred_encoding, errors="replace").strip()


def normalize_pid_list(raw_requested_pids: Any) -> List[int]:
    """
    规范化PID列表
    
    将各种格式的PID数据转换为整数列表
    
    参数:
        raw_requested_pids: 原始PID数据（可以是列表、单个值等）
    
    返回:
        list: 去重后的正整数PID列表
    """
    requested_pids: List[int] = []

    if isinstance(raw_requested_pids, (list, tuple, set)):
        values = raw_requested_pids
    elif raw_requested_pids in (None, ""):
        values = []
    else:
        values = [raw_requested_pids]

    for item in values:
        try:
            pid = int(item)
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid not in requested_pids:
            requested_pids.append(pid)

    return requested_pids


# =============================================================================
# 进程信息查询函数
# =============================================================================

def get_process_info(proc: psutil.Process, default_cwd: str = "") -> Dict[str, Any]:
    """
    获取进程详细信息
    
    安全地获取进程的各种信息，如果某项获取失败则使用默认值
    
    参数:
        proc: psutil进程对象
        default_cwd: 默认工作目录（获取失败时使用）
    
    返回:
        dict: 进程信息字典，包含:
            - pid: 进程ID
            - process_name: 进程名称
            - cmdline: 命令行参数
            - cwd: 工作目录
            - status: 进程状态
            - create_time: 创建时间
    """
    cmdline: List[str] = []
    cwd = default_cwd
    status = "unknown"
    create_time = None
    process_name = "unknown"

    try:
        process_name = proc.name()
    except Exception:
        pass

    try:
        cmdline = proc.cmdline()
    except Exception:
        pass

    try:
        cwd = proc.cwd() or default_cwd
    except Exception:
        pass

    try:
        status = proc.status()
    except Exception:
        pass

    try:
        create_time = proc.create_time()
    except Exception:
        pass

    return {
        "pid": proc.pid,
        "process_name": process_name,
        "cmdline": cmdline,
        "cwd": cwd,
        "status": status,
        "create_time": create_time
    }


def find_matching_processes(install_location: str, service_name: str) -> List[psutil.Process]:
    """
    查找匹配的进程
    
    根据安装目录和服务名称查找相关进程
    匹配条件: 进程的cwd、cmdline、name或exe包含install_location或service_name
    
    参数:
        install_location: 安装目录
        service_name: 服务名称
    
    返回:
        list: 匹配的psutil进程对象列表
    """
    install_location_lower = install_location.lower()
    service_name_lower = service_name.lower()
    matches: Dict[int, psutil.Process] = {}

    for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd", "exe"]):
        try:
            pid = proc.info.get("pid")
            if not pid:
                continue

            markers = []
            for value in (
                proc.info.get("name"),
                proc.info.get("cwd"),
                proc.info.get("exe"),
                " ".join(proc.info.get("cmdline") or [])
            ):
                if value:
                    markers.append(str(value).lower())

            if any(install_location_lower in marker or service_name_lower in marker for marker in markers):
                matches[pid] = proc
        except Exception:
            continue

    return list(matches.values())


def sort_process_infos(process_infos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    对进程信息列表进行排序
    
    排序规则:
        1. 启动器进程（cmd.exe, bash等）排在后面
        2. 按创建时间排序
    
    参数:
        process_infos: 进程信息字典列表
    
    返回:
        list: 排序后的进程信息列表
    """
    def sort_key(info: Dict[str, Any]):
        process_name = str(info.get("process_name", "") or "").lower()
        is_launcher = 1 if process_name in {"cmd.exe", "powershell.exe", "pwsh.exe", "sh", "bash"} else 0
        create_time = info.get("create_time") or 0
        return is_launcher, create_time

    return sorted(process_infos, key=sort_key)


def resolve_monitor_path(base_dir: str, raw_path: Any) -> str:
    """
    解析监控文件路径
    
    将相对路径转换为绝对路径
    
    参数:
        base_dir: 基础目录
        raw_path: 原始路径（可以是相对或绝对路径）
    
    返回:
        str: 绝对路径
    """
    monitor_path = ensure_text_content(raw_path).strip()
    if not monitor_path:
        return ""
    if not os.path.isabs(monitor_path):
        monitor_path = os.path.join(base_dir, monitor_path)
    return os.path.abspath(monitor_path)


# =============================================================================
# 校验与工具函数
# =============================================================================

def check_md5(file_path: str, expected_md5: str) -> bool:
    """
    校验文件的MD5值
    
    参数:
        file_path: 文件路径
        expected_md5: 期望的MD5值
    
    返回:
        bool: MD5是否匹配
    """
    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest().lower() == expected_md5.lower()


# =============================================================================
# 任务结果构建函数
# =============================================================================

def build_task_result(
    success: bool,
    task_type: str,
    message: str,
    data: Optional[Dict[str, Any]] = None,
    error_type: str = "",
    error_message: str = "",
    tb: str = ""
) -> Dict[str, Any]:
    """
    构建标准化的任务执行结果
    
    参数:
        success: 是否成功
        task_type: 任务类型（install/start/stop等）
        message: 结果消息
        data: 附加数据字典（可选）
        error_type: 错误类型（失败时）
        error_message: 错误消息（失败时）
        tb: 错误堆栈（失败时）
    
    返回:
        dict: 标准化的任务结果字典
    """
    return {
        "success": success,
        "task_type": task_type,
        "message": message,
        "data": data or {},
        "error": {} if success else {
            "error_type": error_type or f"{task_type.title()}TaskError",
            "error_message": error_message or message,
            "traceback": tb
        }
    }


def build_install_task_data(
    status: str,
    file_path: str,
    work_dir: str,
    script_path: str,
    attempts: int,
    cleanup_result: Dict[str, Any],
    script_executed: bool = False,
    script_output: str = "",
    script_error: str = "",
    script_return_code: int = 0,
    install_location: Optional[str] = None
) -> Dict[str, Any]:
    """
    构建安装任务的详细数据
    
    参数:
        status: 状态（installed/install_failed）
        file_path: 安装文件路径
        work_dir: 工作目录
        script_path: 脚本路径
        attempts: 尝试次数
        cleanup_result: 脚本清理结果
        script_executed: 脚本是否执行
        script_output: 脚本标准输出
        script_error: 脚本标准错误
        script_return_code: 脚本返回码
        install_location: 安装位置（可选）
    
    返回:
        dict: 安装任务数据字典
    """
    return {
        "status": status,
        "file_path": file_path,
        "work_dir": work_dir,
        "install_location": install_location or work_dir,
        "script_path": script_path,
        "attempts": attempts,
        "script_executed": script_executed,
        "script_output": script_output,
        "script_error": script_error,
        "script_return_code": script_return_code,
        "script_cleanup_result": cleanup_result
    }


def build_stop_task_data(
    status: str,
    install_location: str,
    service_name: str,
    script_path: str,
    attempts: int,
    requested_pids: List[int],
    matched_before: List[Dict[str, Any]],
    matched_after: List[Dict[str, Any]],
    target_pids: List[int],
    stopped_pids: List[int],
    remaining_target_pids: List[int],
    failed_items: List[Dict[str, Any]],
    registered_instances: List[Dict[str, Any]],
    heartbeat_stopped_instances: List[Dict[str, Any]],
    deregistered_instances: List[Dict[str, Any]],
    deregister_failed: List[Dict[str, Any]],
    launcher_stdout: str,
    launcher_stderr: str,
    cleanup_result: Dict[str, Any]
) -> Dict[str, Any]:
    """
    构建停止任务的详细数据
    
    参数:
        status: 状态（stopped/stop_failed）
        install_location: 安装目录
        service_name: 服务名称
        script_path: 停止脚本路径
        attempts: 尝试次数
        requested_pids: 请求的PID列表
        matched_before: 停止前匹配的进程信息
        matched_after: 停止后匹配的进程信息
        target_pids: 目标PID列表
        stopped_pids: 已停止的PID列表
        remaining_target_pids: 剩余未停止的PID
        failed_items: 失败项列表
        registered_instances: 注册的Nacos实例
        heartbeat_stopped_instances: 心跳已停止的实例
        deregistered_instances: 已注销的Nacos实例
        deregister_failed: 注销失败的实例
        launcher_stdout: 启动器标准输出
        launcher_stderr: 启动器标准错误
        cleanup_result: 脚本清理结果
    
    返回:
        dict: 停止任务数据字典
    """
    ordered_before = sort_process_infos(matched_before)
    ordered_after = sort_process_infos(matched_after)
    return {
        "status": status,
        "install_location": install_location,
        "service_name": service_name,
        "stop_script_path": script_path,
        "attempts": attempts,
        "requested_pids": requested_pids,
        "processes_before": ordered_before,
        "process_count_before": len(ordered_before),
        "processes_after": ordered_after,
        "process_count_after": len(ordered_after),
        "target_pids": target_pids,
        "stopped_pids": stopped_pids,
        "remaining_target_pids": remaining_target_pids,
        "failed": failed_items,
        "nacos_registered_instances": registered_instances,
        "nacos_heartbeat_stopped_instances": heartbeat_stopped_instances,
        "nacos_deregistered_instances": deregistered_instances,
        "nacos_deregister_failed": deregister_failed,
        "launcher_stdout": launcher_stdout,
        "launcher_stderr": launcher_stderr,
        "script_cleanup_result": cleanup_result
    }


def build_uninstall_task_data(
    status: str,
    install_location: str,
    script_path: str,
    attempts: int,
    removed: bool,
    success_by_file_path: bool,
    stdout_text: str,
    stderr_text: str,
    return_code: Optional[int],
    cleanup_result: Dict[str, Any],
    install_exists_before: bool,
    monitored_file_path: str = "",
    monitored_file_exists_before: Optional[bool] = None
) -> Dict[str, Any]:
    """
    构建卸载任务的详细数据
    
    参数:
        status: 状态（uninstalled/uninstall_failed）
        install_location: 安装目录
        script_path: 卸载脚本路径
        attempts: 尝试次数
        removed: 是否已删除目录
        success_by_file_path: 是否通过文件路径判定成功
        stdout_text: 标准输出
        stderr_text: 标准错误
        return_code: 返回码
        cleanup_result: 脚本清理结果
        install_exists_before: 卸载前安装目录是否存在
        monitored_file_path: 监控的文件路径
        monitored_file_exists_before: 卸载前监控文件是否存在
    
    返回:
        dict: 卸载任务数据字典
    """
    install_exists_after = os.path.exists(install_location)
    monitored_file_exists_after = os.path.exists(monitored_file_path) if monitored_file_path else None
    return {
        "status": status,
        "install_location": install_location,
        "uninstall_script_path": script_path,
        "attempts": attempts,
        "removed_dir": install_location if removed else None,
        "install_location_exists_before": install_exists_before,
        "install_location_exists_after": install_exists_after,
        "file_path": monitored_file_path or None,
        "file_path_exists_before": monitored_file_exists_before,
        "file_path_exists_after": monitored_file_exists_after,
        "success_by_file_path": success_by_file_path,
        "launcher_stdout": stdout_text,
        "launcher_stderr": stderr_text,
        "return_code": return_code,
        "script_cleanup_result": cleanup_result
    }


def normalize_task_result(raw_result: Any, task_id: str, task_type: str, agent_ip: str) -> Dict[str, Any]:
    """
    规范化任务结果为统一格式
    
    处理各种可能的返回格式，统一转换为标准格式:
    {
        "ip": agent_ip,
        "task_id": task_id,
        "result": True/False,
        "task_type": task_type,
        "message": "...",
        "data": {...}
    }
    
    参数:
        raw_result: 原始返回结果（可以是dict、tuple等）
        task_id: 任务ID
        task_type: 任务类型
        agent_ip: Agent IP地址
    
    返回:
        dict: 规范化的任务结果
    """
    def build_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_data = payload.get("data", {})
        if not isinstance(raw_data, dict):
            raw_data = {"value": raw_data}

        raw_error = payload.get("error", {})
        if not isinstance(raw_error, dict):
            raw_error = {}

        return {
            "ip": payload.get("ip", agent_ip),
            "task_id": payload.get("task_id", task_id),
            "result": payload.get("result", payload.get("success", False)),
            "task_type": payload.get("task_type", task_type),
            "message": payload.get("message", ""),
            "status": payload.get("status", 4),
            "data": {
                **raw_data,
                "error_type": raw_data.get("error_type", raw_error.get("error_type", "")),
                "error_message": raw_data.get("error_message", raw_error.get("error_message", raw_error.get("error", ""))),
                "traceback": raw_data.get("traceback", raw_error.get("traceback", ""))
            }
        }

    # 如果已经是字典格式
    if isinstance(raw_result, dict):
        return build_payload(raw_result)

    # 如果是元组格式 (success, payload)
    if isinstance(raw_result, tuple) and len(raw_result) == 2:
        success, payload = raw_result
        if isinstance(payload, dict):
            if any(key in payload for key in ("success", "result", "task_type", "message", "data", "error")):
                normalized_payload = dict(payload)
                normalized_payload.setdefault("success", bool(success))
                normalized_payload.setdefault("task_type", task_type)
                normalized_payload.setdefault("message", f"{task_type}任务执行成功" if success else f"{task_type}任务执行失败")
                return build_payload(normalized_payload)

            return build_payload({
                "success": bool(success),
                "task_type": task_type,
                "message": f"{task_type}任务执行成功" if success else f"{task_type}任务执行失败",
                "data": payload if success else {
                    key: value for key, value in payload.items()
                    if key not in ("error", "error_type", "error_message", "traceback")
                },
                "error": {} if success else {
                    "error_type": payload.get("error_type", "TaskExecutionError"),
                    "error_message": payload.get("error_message", payload.get("error", "任务执行失败")),
                    "traceback": payload.get("traceback", "")
                }
            })

        return build_payload({
            "success": bool(success),
            "task_type": task_type,
            "message": f"{task_type}任务执行成功" if success else f"{task_type}任务执行失败",
            "data": {"value": payload} if success else {},
            "error": {} if success else {
                "error_type": "TaskExecutionError",
                "error_message": str(payload),
                "traceback": ""
            }
        })

    # 未知格式，返回错误
    return {
        "ip": agent_ip,
        "task_id": task_id,
        "result": False,
        "status": 4,
        "task_type": task_type,
        "message": "任务返回结果格式无效",
        "data": {
            "error_type": "InvalidTaskResult",
            "error_message": f"任务返回结果类型不支持: {type(raw_result).__name__}",
            "traceback": ""
        }
    }
