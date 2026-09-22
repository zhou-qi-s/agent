"""
安装任务模块

职责：把「缓存区」中已下载的指定版本部署到「运行区」（软链接结构），
写入运行状态，再执行包内的 bin/install.sh。

缓存区（server.download）：应用包下载、解压后的原始内容，可随时清理重下
    {download}/{app_name}/{version}/{app,bin,config}

运行区（server.apps）：实际运行位置，子目录以软链接指向缓存区
    {apps}/{app_name}/
        ├── state/config.yaml      运行状态（pid / name / runtime / version）
        ├── current -> {download}/{app_name}/{version}
        ├── app     -> current/app
        ├── bin     -> current/bin
        └── config  -> current/config

处理流程：
    1. 校验参数（version 必传）
    2. 在缓存区查找 {download}/{file_name}/{version}/ 版本目录，
       不存在则结束（区分「应用目录都没有」与「版本缺失」）
    3. 检查运行区：runtime=true 表示正在运行 → 拒绝安装；
       否则清空残留 pid
    4. 建立/切换软链接（current 指向该版本）并校验可解析
    5. 写入运行状态（version 更新为本次传入值，runtime=false）
    6. 执行运行区的 bin/install.sh
"""

import logging
import os
import traceback
from typing import Any, Dict, Optional

from core.executor.shell import Shell
from utils.config_loader import load_config
from utils.app_path import (
    find_cache_version_dir,
    list_cache_versions,
    find_apps_component_dir,
    read_state,
    write_state,
    is_running,
    link_to_current,
    verify_links,
)

# ── 全局配置 ──
_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

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
    安装任务 — 把缓存区的指定版本部署到运行区，并执行 bin/install.sh。

    必传参数:
        - task_id:   任务ID
        - file_name: 应用名称（与下载时的 file_name 一致）
        - version:   要安装的版本号（后端下发）
    可选参数:
        - retry:     重试次数（暂未使用，保留兼容）
        - timeout:   脚本执行超时秒数
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    file_name = str(parameters.get("file_name", "") or "").strip()
    version = str(parameters.get("version", "") or "").strip()

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
    if not version:
        return _build_result(
            task_id,
            False,
            "参数缺失: version",
            error_type="ParameterMissing",
            error_message="version 参数缺失，安装需明确指定要部署的版本",
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

    # ── 第一步：在下载（缓存）目录下查找该应用「指定版本」的目录 ──
    # 目标路径：{server.download}/{file_name}/{version}/
    # 查找逻辑抽到 utils/app_path.py，其他任务模块（start/stop/upgrade 等）可复用。
    version_dir = find_cache_version_dir(file_name, version)
    if not version_dir:
        expected = os.path.join(base_path, file_name, version)
        component_dir = os.path.join(base_path, file_name)
        logging.error(f"[安装任务] 版本目录不存在: {expected}")
        # 区分「应用目录都没有」与「应用在但版本缺失」，便于排查
        if not os.path.isdir(component_dir):
            return _build_result(
                task_id,
                False,
                f"组件目录不存在: {component_dir}",
                data={"base_path": base_path, "file_name": file_name,
                      "component_dir": component_dir},
                error_type="ComponentDirNotFound",
                error_message=f"下载目录下未找到应用 {file_name}，请先执行下载任务",
            )
        available = list_cache_versions(file_name)
        return _build_result(
            task_id,
            False,
            f"版本目录不存在: {expected}",
            data={"base_path": base_path, "file_name": file_name,
                  "version": version, "version_dir": expected,
                  "available_versions": available},
            error_type="VersionDirNotFound",
            error_message=(f"缓存区中未找到版本 {version}，"
                           f"已下载的版本: {available or '无'}"),
        )
    component_dir = os.path.dirname(version_dir)
    logging.info(f"[安装任务] 组件目录: {component_dir}, 版本目录: {version_dir}")

    # ── 第二步：检查运行区是否已有该应用，且在运行则拒绝安装 ──
    apps_component_dir = find_apps_component_dir(file_name)
    if apps_component_dir:
        state = read_state(file_name)
        logging.info(f"[安装任务] 检测到运行区已存在: {apps_component_dir}, 状态={state}")

        if is_running(file_name):
            running_ver = state.get("version", "")
            running_pids = state.get("pids", [])
            logging.error(
                f"[安装任务] 应用正在运行，拒绝安装: version={running_ver} pids={running_pids}"
            )
            return _build_result(
                task_id,
                False,
                f"应用正在运行 (version={running_ver}, pids={running_pids})，请先停止后再安装",
                data={"file_name": file_name, "app_dir": apps_component_dir,
                      "running_version": running_ver, "pids": running_pids},
                error_type="ServiceRunning",
                error_message="该应用当前处于运行状态，无法执行安装",
            )

        # 未运行：清空残留的 pids / processes（保留其余字段，稍后统一覆写 version）
        if state.get("pids") or state.get("pid"):
            logging.info(f"[安装任务] 应用未运行，清空残留进程记录: {state.get('pids')}")
        state["pids"] = []
        state["processes"] = []
        state.pop("pid", None)
    else:
        logging.info(f"[安装任务] 运行区尚无该应用，将新建: {file_name}")
        state = {"pids": [], "processes": []}

    # ── 第三步：建立/切换运行区软链接，指向该版本 ──
    # （目标版本目录已在第一步校验存在，此处直接建链）
    link_result = link_to_current(file_name, version, _DOWNLOAD_BASE, _APPS_BASE)
    if not link_result["ok"]:
        return _build_result(
            task_id,
            False,
            link_result["error"],
            data={"file_name": file_name, "version": version,
                  "apps_dir": _APPS_BASE},
            error_type="LinkFailed",
            error_message=link_result["error"],
        )

    app_dir = link_result["app_dir"]
    logging.info(f"[安装任务] 软链接建立完成: {app_dir}")

    # ── 第四步：校验软链接可解析（防缓存区被清理后残留断链） ──
    broken = verify_links(file_name)
    if broken:
        return _build_result(
            task_id,
            False,
            f"运行区存在失效软链接: {', '.join(broken)}",
            data={"file_name": file_name, "version": version,
                  "app_dir": app_dir, "broken_links": broken},
            error_type="BrokenSymlink",
            error_message="软链接目标不存在，缓存区可能已被清理，请重新下载",
        )

    # ── 第五步：写入运行状态（version 更新为本次传入的版本） ──
    state.update({
        "pids": state.get("pids", []) or [],
        "processes": state.get("processes", []) or [],
        "name": file_name,
        "runtime": False,
        "version": version,
    })
    state.pop("pid", None)
    if not write_state(file_name, state):
        return _build_result(
            task_id,
            False,
            "写入运行状态文件失败",
            data={"file_name": file_name, "version": version, "app_dir": app_dir},
            error_type="StateWriteError",
            error_message="无法写入 state/config.yaml",
        )
    logging.info(f"[安装任务] 运行状态已更新: {state}")

    # ── 第六步：定位安装脚本（运行区，经软链接访问） ──
    # 从运行区寻址，使脚本内 $(dirname $0)/.. 落在运行区，
    # 与运行时 bin/app/config 的访问路径一致。
    script_path = _find_install_script(app_dir)
    if script_path is None:
        bin_dir = os.path.join(app_dir, "bin")
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
    test_version = "3.9.2"

    result = install_task(
        {
            "task_id": "test-install-001",
            "file_name": test_file_name,
            "version": test_version,
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
