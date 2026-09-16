"""
安装任务模块

基于下载任务输出的目录结构，定位 bin/install.sh 脚本并执行。
"""

import logging
import os
import traceback
from typing import Any, Dict, Optional

from core.executor.shell import Shell
from utils.config_loader import load_config

# ── 全局配置 ──
_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")

# ── 结果构建 ──

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
        # 顶层状态码（平台约定）: 8=INSTALLED 已安装 / 9=INSTALL_FAILED 安装失败
        "status": 8 if success else 9,
        "task_id": task_id,
        "task_type": "install",
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "InstallTaskError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


# ── 安装脚本定位 ──

def _find_install_script(version_dir: str) -> Optional[str]:
    """在 {version_dir}/bin/ 下查找 install.sh 脚本"""
    bin_dir = os.path.join(version_dir, "bin")
    if not os.path.isdir(bin_dir):
        return None
    script_path = os.path.join(bin_dir, "install.sh")
    if os.path.isfile(script_path):
        return script_path
    return None


# ── 主入口 ──

def install_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    安装任务 — 定位已下载的组件并执行 bin/install.sh 安装脚本。

    必传参数:
        - task_id:   任务ID
        - file_name: 组件名称（与下载时的 file_name 一致）
    可选参数:
        - retry:     重试次数（暂未使用，保留兼容）
        - timeout:   脚本执行超时秒数

    流程:
        1. 从 config.yaml → server.download 获取基础路径
        2. 进入 {base_path}/{file_name}/ 读取 version 文件
        3. 定位 {base_path}/{file_name}/{version}/bin/install.sh
        4. 执行安装脚本
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    file_name = str(parameters.get("file_name", "") or "").strip()

    # ── 参数校验 ──
    if not task_id:
        return _build_result(
            task_id,
            False,
            "参数缺失: task_id",
            error_type="ParameterMissing",
            error_message="task_id 参数缺失",
        )
    if not file_name:
        return _build_result(
            task_id,
            False,
            "参数缺失: file_name",
            error_type="ParameterMissing",
            error_message="file_name 参数缺失",
        )
    if not _DOWNLOAD_BASE:
        return _build_result(
            task_id,
            False,
            "config.yaml 中未配置 server.download 基础路径",
            error_type="ConfigMissing",
            error_message="server.download 未配置，无法定位组件目录",
        )

    base_path = _DOWNLOAD_BASE
    component_dir = os.path.join(base_path, file_name)
    logging.info(f"[安装任务] 基础路径: {base_path}, 组件目录: {component_dir}")

    # ── 检查组件目录 ──
    if not os.path.isdir(component_dir):
        return _build_result(
            task_id,
            False,
            f"组件目录不存在: {component_dir}",
            data={"base_path": base_path, "file_name": file_name, "component_dir": component_dir},
            error_type="FileNotFoundError",
            error_message=f"组件目录不存在: {component_dir}",
        )

    # ── 读取 version 文件 ──
    version_file = os.path.join(component_dir, "version")
    if not os.path.isfile(version_file):
        return _build_result(
            task_id,
            False,
            f"version 文件不存在: {version_file}",
            data={"component_dir": component_dir},
            error_type="FileNotFoundError",
            error_message="未找到 version 文件，请确认该组件已下载",
        )
    try:
        with open(version_file, "r", encoding="utf-8") as vf:
            version = vf.read().strip()
    except Exception as e:
        return _build_result(
            task_id,
            False,
            f"读取 version 文件失败: {e}",
            data={"component_dir": component_dir},
            error_type="VersionReadError",
            error_message=str(e),
        )
    if not version:
        return _build_result(
            task_id,
            False,
            "version 文件内容为空",
            data={"component_dir": component_dir},
            error_type="VersionEmpty",
            error_message="version 文件内容为空",
        )

    logging.info(f"[安装任务] 组件版本: {version}")

    # ── 定位安装脚本: {component_dir}/{version}/bin/ ──
    version_dir = os.path.join(component_dir, version)
    if not os.path.isdir(version_dir):
        return _build_result(
            task_id,
            False,
            f"版本目录不存在: {version_dir}",
            data={"component_dir": component_dir, "version": version, "version_dir": version_dir},
            error_type="FileNotFoundError",
            error_message=f"版本目录不存在: {version_dir}",
        )

    script_path = _find_install_script(version_dir)
    if script_path is None:
        bin_dir = os.path.join(version_dir, "bin")
        return _build_result(
            task_id,
            False,
            f"未找到安装脚本: {bin_dir}/install.sh",
            data={"component_dir": component_dir, "version": version, "bin_dir": bin_dir},
            error_type="InstallScriptNotFound",
            error_message=f"未在 {bin_dir} 中找到 install.sh",
        )

    logging.info(f"[安装任务] 定位到安装脚本: {script_path}")

    # ── 执行安装脚本 ──
    script_timeout = timeout if timeout > 0 else 300
    try:
        result = Shell.run_script(
            script_path=script_path,
            timeout=script_timeout,
        )

        if result.success:
            logging.info(f"[安装任务] 安装脚本执行成功: {script_path}")
            return _build_result(
                task_id,
                True,
                "安装成功",
                data={
                    "status": "installed",
                    "file_name": file_name,
                    "version": version,
                    "script_path": script_path,
                    "exit_code": result.code,
                    "stdout": result.stdout,
                },
            )
        else:
            logging.error(f"[安装任务] 安装脚本执行失败: exit_code={result.code}, stderr={result.stderr}")
            return _build_result(
                task_id,
                False,
                f"安装脚本执行失败 (exit_code={result.code})",
                data={
                    "status": "install_failed",
                    "file_name": file_name,
                    "version": version,
                    "script_path": script_path,
                    "exit_code": result.code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                error_type="InstallScriptError",
                error_message=result.stderr or f"安装脚本返回非零退出码: {result.code}",
            )

    except Exception as e:
        logging.error(f"[安装任务] 安装脚本执行异常: {e}")
        return _build_result(
            task_id,
            False,
            f"安装脚本执行异常: {e}",
            data={
                "status": "install_error",
                "file_name": file_name,
                "version": version,
                "script_path": script_path,
            },
            error_type=type(e).__name__,
            error_message=str(e),
            tb=traceback.format_exc(),
        )


# ── 自测入口 ──

if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    test_file_name = "hellogitworld-master"

    result = install_task(
        {
            "task_id": "test-install-001",
            "file_name": test_file_name,
        },
        retry=0,
        timeout=300,
    )

    print("\n" + "=" * 60)
    print("  安装任务结果")
    print("=" * 60)
    print(f"  task_id : {result.get('task_id', '')}")
    print(f"  成功    : {result['success']}")
    print(f"  状态    : {result.get('status', 'N/A')}")
    print(f"  消息    : {result['message']}")
    data = result.get("data", {})
    if data:
        print(f"  安装状态: {data.get('status', 'N/A')}")
        print(f"  文件名  : {data.get('file_name', 'N/A')}")
        print(f"  版本    : {data.get('version', 'N/A')}")
        print(f"  脚本    : {data.get('script_path', 'N/A')}")
        print(f"  退出码  : {data.get('exit_code', 'N/A')}")
        stdout = data.get("stdout", "")
        if stdout:
            print(f"  输出    :")
            for line in stdout.split("\n")[:10]:
                print(f"    {line}")
    if not result["success"]:
        err = result.get("error", {})
        print(f"  错误类型: {err.get('error_type', '')}")
        print(f"  错误信息: {err.get('error_message', '')}")
    print("=" * 60)
