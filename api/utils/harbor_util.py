"""
Harbor 相关公共工具方法
"""
import logging
import os
import subprocess
from typing import Dict, Optional, Tuple

from fastapi import HTTPException
from utils.config_loader import load_config
from utils.util import get_ip


# 日志前缀常量
LOG_PUSH = "harbor_push"
LOG_PUSH_CHART = "harbor_push_chart"


def get_harbor_config() -> dict:
    """加载 config.yaml 并返回 harbor 配置字典"""
    config = load_config()
    return config.get("harbor", {})


def get_download_dir() -> str:
    """获取 harbor 下载目录，未配置则抛出 500"""
    harbor_config = get_harbor_config()
    download_dir = harbor_config.get("download", "")
    if not download_dir:
        raise HTTPException(status_code=500, detail="Harbor 下载目录未配置")
    return download_dir


def get_repository_ip() -> str:
    """获取本机 IP 作为仓库地址，失败则抛出 500"""
    repository = get_ip()
    if not repository:
        raise HTTPException(status_code=500, detail="无法获取本机 IP 地址")
    logging.info("本机 IP: %s", repository)
    return repository


def get_harbor_connection_info(require_auth: bool = True, project_key: str = "project") -> dict:
    """
    读取 Harbor 连接配置，返回统一字典。
    
    Args:
        require_auth: 是否需要用户名密码
        project_key: 项目配置 key（project 或 template）
    
    Returns:
        {
            "agreement": str,
            "port_str": str,
            "project": str,
            "is_https": bool,
            "harbor_addr_local": str,
            "username": str (if require_auth),
            "password": str (if require_auth),
        }
    """
    harbor_config = get_harbor_config()
    agreement = (harbor_config.get("agreement", "") or "").strip()
    port = harbor_config.get("port", "")
    port_str = str(port).strip() if port else ""
    project = (harbor_config.get(project_key, "") or "").strip()

    missing = [k for k, v in {"agreement": agreement, project_key: project}.items() if not v]

    if require_auth:
        username = str(harbor_config.get("username", "") or "").strip()
        password = str(harbor_config.get("password", "") or "").strip()
        for k, v in {"username": username, "password": password}.items():
            if not v:
                missing.append(k)

    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Harbor 配置不完整，缺少: {', '.join(missing)}"
        )

    is_https = "https" in agreement

    # 回环地址避免证书问题
    harbor_addr_local = "127.0.0.1"
    if port_str:
        harbor_addr_local = f"127.0.0.1:{port_str}"

    result = {
        "agreement": agreement,
        "port_str": port_str,
        "project": project,
        "is_https": is_https,
        "harbor_addr_local": harbor_addr_local,
    }
    if require_auth:
        result["username"] = username
        result["password"] = password

    return result


def run_command(
    cmd: list,
    timeout: int = 60,
    input_str: Optional[str] = None,
    env: Optional[dict] = None,
    log_prefix: str = "",
    step_name: str = "",
    step_desc: str = "",
    steps: Optional[list] = None,
) -> Tuple[str, str]:
    """
    执行命令，统一处理日志和错误。

    Args:
        cmd: 命令列表
        timeout: 超时秒数
        input_str: 标准输入文本（如密码）
        env: 环境变量
        log_prefix: 日志前缀
        step_name: 步骤名称
        step_desc: 步骤描述（用于日志和错误信息）
        steps: 步骤记录列表

    Returns:
        (stdout, stderr)

    Raises:
        HTTPException: 命令执行失败时抛出
    """
    logging.info("[%s] 执行: %s", log_prefix, " ".join(cmd))
    result = subprocess.run(
        cmd,
        input=input_str,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    logging.info("[%s] %s stdout: %s", log_prefix, step_name, stdout)
    logging.info("[%s] %s stderr: %s", log_prefix, step_name, stderr)

    if result.returncode != 0:
        if steps is not None:
            steps.append({
                "step": step_name,
                "success": False,
                "message": f"{step_desc} 失败",
                "stderr": stderr,
            })
        raise HTTPException(
            status_code=500,
            detail=f"{step_desc} 失败: {stderr}"
        )

    if steps is not None:
        steps.append({
            "step": step_name,
            "success": True,
            "message": f"{step_desc} 成功",
            "stdout": stdout,
        })

    return stdout, stderr


def parse_chart_info(file_name: str) -> Tuple[str, str, str]:
    """
    从 Chart 文件名解析信息。

    Args:
        file_name: 文件名，如 mysql-14.0.3.tgz

    Returns:
        (chart_name, chart_version, chart_file): 如 ("mysql", "14.0.3", "mysql:14.0.3")
    """
    chart_full_name = os.path.splitext(file_name)[0]  # mysql-14.0.3
    if "-" in chart_full_name:
        parts = chart_full_name.rsplit("-", 1)
        chart_name = parts[0]
        chart_version = parts[1]
    else:
        chart_name = chart_full_name
        chart_version = ""

    chart_file = f"{chart_name}:{chart_version}"
    return chart_name, chart_version, chart_file


def build_display_url(agreement: str, repository: str, port_str: str) -> str:
    """构建对外展示的 Harbor URL"""
    url = f"{agreement}{repository}"
    if port_str:
        url = f"{agreement}{repository}:{port_str}"
    return url


def build_oci_registry(harbor_addr_local: str, project: str) -> str:
    """构建 OCI registry 地址"""
    return f"oci://{harbor_addr_local}/{project}"


def safe_error_handler(log_prefix: str, e: Exception, error_desc: str):
    """统一的异常处理：HTTPException 直接抛出，其他包装为 500"""
    if isinstance(e, HTTPException):
        raise
    logging.error("[%s] %s: %s", log_prefix, error_desc, e)
    raise HTTPException(
        status_code=500,
        detail=f"{error_desc}: {str(e)}",
    )
