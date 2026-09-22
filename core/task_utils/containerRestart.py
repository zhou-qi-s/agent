"""
容器升级/重启任务模块

从 Harbor 仓库拉取 Helm Chart 并升级 k8s/k3s 集群中已有的 release。
"""

import logging
import os
import subprocess
import traceback
from typing import Any, Dict, Optional

from utils.config_loader import load_config

# ── 全局配置 ──
_CONFIG = load_config()
_HARBOR_CFG = _CONFIG.get("harbor", {})
_K8S_CFG = _CONFIG.get("k8s", {})

_LOG = "container_restart"


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
        # 本任务对应平台按钮"修改版本"（TaskTypeEnum.CONTAINER_RESTART），
        # 实际执行 helm upgrade --install，失败态用 7（升级失败）是对的，勿改成 4/14
        "status": 3 if success else 7,
        "task_id": task_id,
        "task_type": "container_restart",
        "message": message,
        "data": data or {},
        "error": (
            {}
            if success
            else {
                "error_type": error_type or "ContainerRestartError",
                "error_message": error_message or message,
                "traceback": tb,
            }
        ),
    }


# =============================================================================
# 配置读取
# =============================================================================

def _get_harbor_config() -> tuple:
    """
    读取 Harbor 连接配置。

    返回:
        (error, dict|None)
        - 出错: (error_result, None)
        - 正常: (None, {agreement, port_str, project, is_https, harbor_addr, oci_registry})
    """
    agreement = (_HARBOR_CFG.get("agreement", "") or "").strip()
    port = _HARBOR_CFG.get("port", "")
    port_str = str(port).strip() if port else ""
    project = (_HARBOR_CFG.get("template", "") or "").strip()

    missing = []
    if not agreement:
        missing.append("harbor.agreement")
    if not project:
        missing.append("harbor.template")

    if missing:
        return (
            _build_result(
                "", False, f"Harbor 配置不完整，缺少: {', '.join(missing)}",
                error_type="ConfigMissing",
                error_message=f"Harbor 配置缺失: {', '.join(missing)}",
            ),
            None,
        )

    is_https = "https" in agreement

    harbor_addr = "127.0.0.1"
    if port_str:
        harbor_addr = f"127.0.0.1:{port_str}"

    oci_registry = f"oci://{harbor_addr}/{project}"

    return None, {
        "agreement": agreement,
        "port_str": port_str,
        "project": project,
        "is_https": is_https,
        "harbor_addr": harbor_addr,
        "oci_registry": oci_registry,
    }


def _get_chart_download_dir() -> tuple:
    """
    获取 Harbor Chart 下载目录。

    返回:
        (error, str)
    """
    download_dir = (_HARBOR_CFG.get("download", "") or "").strip()
    if not download_dir:
        return (
            _build_result(
                "", False, "Harbor 下载目录未配置",
                error_type="ConfigMissing",
                error_message="harbor.download 未配置",
            ),
            "",
        )
    return None, download_dir


def _get_k8s_download_dir() -> tuple:
    """
    获取 k8s 下载目录（用于存放临时 kubeconfig 文件）。

    返回:
        (error, str)
    """
    download = (_K8S_CFG.get("download", "") or "").strip()
    if not download:
        return (
            _build_result(
                "", False, "k8s.download 未配置",
                error_type="ConfigMissing",
                error_message="k8s.download 未配置，请在 config.yaml 中设置 k8s.download",
            ),
            "",
        )
    k8s_dir = download
    if os.path.isfile(k8s_dir):
        k8s_dir = os.path.dirname(k8s_dir)
    if not os.path.isdir(k8s_dir):
        return (
            _build_result(
                "", False, f"k8s.download 目录不存在: {k8s_dir}",
                error_type="FileNotFoundError",
                error_message=f"k8s.download 目录不存在: {k8s_dir}",
            ),
            "",
        )
    return None, k8s_dir


# =============================================================================
# Helm 操作
# =============================================================================

def _run_command(cmd: list, timeout: int = 120, env: Optional[dict] = None) -> tuple:
    """
    执行命令并返回结果。

    返回:
        (success, stdout, stderr)
    """
    logging.info("[%s] 执行: %s", _LOG, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env or os.environ,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        logging.info("[%s] stdout: %s", _LOG, stdout)
        logging.info("[%s] stderr: %s", _LOG, stderr)
        return result.returncode == 0, stdout, stderr
    except subprocess.TimeoutExpired:
        logging.error("[%s] 命令超时: %s", _LOG, " ".join(cmd))
        return False, "", "命令执行超时"
    except Exception as e:
        logging.error("[%s] 命令异常: %s", _LOG, e)
        return False, "", str(e)


def _helm_pull(oci_ref: str, version: str, chart_download_dir: str, is_https: bool) -> tuple:
    """
    从 Harbor 拉取 Helm Chart。

    返回:
        (success, stdout, stderr)
    """
    pull_cmd = [
        "helm", "pull", oci_ref,
        "--version", version,
        "--destination", chart_download_dir,
    ]
    pull_cmd.append("--insecure-skip-tls-verify" if is_https else "--plain-http")

    logging.info("[%s] 拉取 Chart: %s --version %s", _LOG, oci_ref, version)
    return _run_command(pull_cmd, timeout=300)


def _helm_upgrade(
    release_name: str,
    chart_path: str,
    namespace: str,
    kubeconfig: str,
    timeout: Optional[int] = None,
    atomic: bool = False,
) -> tuple:
    """
    使用 helm upgrade --install 升级 k8s/k3s 集群中的 release。
    --install 确保 release 不存在时自动安装（兼容首次部署场景）。

    Args:
        timeout: helm --timeout，单位为秒；为 None/0 则不传（用 helm 默认值）
        atomic: 是否启用 --atomic，失败自动回滚清理

    返回:
        (success, stdout, stderr)
    """
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig

    upgrade_cmd = [
        "helm", "upgrade", "--install", release_name, chart_path,
        "--namespace", namespace,
        "--create-namespace",
    ]
    if timeout and int(timeout) > 0:
        upgrade_cmd += ["--timeout", f"{int(timeout)}s"]
    if atomic:
        upgrade_cmd.append("--atomic")

    logging.info(
        "[%s] 升级 release: release=%s, namespace=%s, timeout=%s, atomic=%s, kubeconfig=%s",
        _LOG, release_name, namespace, timeout, atomic, kubeconfig,
    )
    return _run_command(upgrade_cmd, timeout=300, env=env)


# =============================================================================
# 清理
# =============================================================================

def _cleanup_temp_files(kubeconfig_path: str, chart_path: str):
    """删除临时 kubeconfig 文件和下载的 Chart 文件"""
    for path, name in [(kubeconfig_path, "kubeconfig"), (chart_path, "Chart")]:
        if path and os.path.exists(path):
            try:
                os.remove(path)
                logging.info("[%s] 已删除临时%s文件: %s", _LOG, name, path)
            except Exception as e:
                logging.warning("[%s] 删除临时%s文件失败: %s", _LOG, name, e)


# =============================================================================
# 主入口
# =============================================================================

def container_restart_task(
    parameters: Dict[str, Any],
    retry: int = 0,
    timeout: int = 300,
) -> Dict[str, Any]:
    """
    从 Harbor 仓库拉取 Helm Chart 并升级 k8s/k3s 集群中已有的 release。

    必填参数（与 container_start_task 相同）:
        - task_id:            任务ID
        - kubeconfig_content: k8s/k3s 的 config 文件内容（文本）
        - chart_name:         Chart 名称，如 mysql
        - chart_version:      Chart 版本号，如 14.0.3
        - release_name:       Helm release 名称
        - namespace:          部署的目标命名空间

    流程:
        1. 将 kubeconfig_content 写入 k8s.download 目录下的临时文件
        2. helm pull 从 Harbor 拉取 Chart 到本地
        3. 使用临时 kubeconfig 执行 helm upgrade --install 升级集群中的 release
        4. helm 完成后删除临时 kubeconfig 和下载的 Chart 文件
    """
    task_id = str(parameters.get("task_id", "") or "").strip()
    kubeconfig_content = str(parameters.get("kubeconfig_content", "") or "")
    chart_name = str(parameters.get("chart_name", "") or "").strip()
    chart_version = str(parameters.get("chart_version", "") or "").strip()
    release_name = str(parameters.get("release_name", "") or "").strip()
    namespace = str(parameters.get("namespace", "") or "").strip()
    helm_timeout = parameters.get("helm_timeout")
    helm_atomic = parameters.get("helm_atomic")

    # ── 参数校验 ──
    if not task_id:
        return _build_result("", False, "参数缺失: task_id",
                             error_type="ParameterMissing", error_message="task_id 缺失")
    if not kubeconfig_content:
        return _build_result(task_id, False, "参数缺失: kubeconfig_content",
                             error_type="ParameterMissing", error_message="kubeconfig_content 缺失")
    if not chart_name:
        return _build_result(task_id, False, "参数缺失: chart_name",
                             error_type="ParameterMissing", error_message="chart_name 缺失")
    if not chart_version:
        return _build_result(task_id, False, "参数缺失: chart_version",
                             error_type="ParameterMissing", error_message="chart_version 缺失")
    if not release_name:
        return _build_result(task_id, False, "参数缺失: release_name",
                             error_type="ParameterMissing", error_message="release_name 缺失")
    if not namespace:
        return _build_result(task_id, False, "参数缺失: namespace",
                             error_type="ParameterMissing", error_message="namespace 缺失")

    # ── 读取 Harbor 配置 ──
    error, harbor_cfg = _get_harbor_config()
    if error:
        error["task_id"] = task_id
        return error

    # ── 读取 Chart 下载目录 ──
    error, chart_download_dir = _get_chart_download_dir()
    if error:
        error["task_id"] = task_id
        return error

    # ── 读取 k8s 下载目录 ──
    error, k8s_download_dir = _get_k8s_download_dir()
    if error:
        error["task_id"] = task_id
        return error

    oci_registry = harbor_cfg["oci_registry"]
    project = harbor_cfg["project"]
    is_https = harbor_cfg["is_https"]
    harbor_addr = harbor_cfg["harbor_addr"]

    oci_ref = f"{oci_registry}/{chart_name}"
    chart_file_name = f"{chart_name}-{chart_version}.tgz"
    save_path = os.path.join(chart_download_dir, chart_file_name)
    kubeconfig_path = os.path.join(k8s_download_dir, f".kubeconfig_{task_id}")

    base_data = {
        "chart_name": chart_name,
        "chart_version": chart_version,
        "chart_file": f"{chart_name}:{chart_version}",
        "release_name": release_name,
        "namespace": namespace,
        "project": project,
        "local_path": save_path,
        "kubeconfig_path": kubeconfig_path,
        "oci_ref": f"oci://{harbor_addr}/{project}/{chart_name}:{chart_version}",
    }

    try:
        # ── Step 0: 生成临时 kubeconfig 文件 ──
        try:
            with open(kubeconfig_path, "w", encoding="utf-8") as f:
                f.write(kubeconfig_content)
            os.chmod(kubeconfig_path, 0o600)
            logging.info("[%s] 已生成临时 kubeconfig: %s", _LOG, kubeconfig_path)
        except Exception as e:
            return _build_result(
                task_id, False, f"生成 kubeconfig 失败: {e}",
                data=base_data,
                error_type="KubeconfigWriteError",
                error_message=str(e),
            )

        # ── Step 1: helm pull 拉取 Chart ──
        success, stdout, stderr = _helm_pull(oci_ref, chart_version, chart_download_dir, is_https)
        if not success:
            _cleanup_temp_files(kubeconfig_path, save_path)
            return _build_result(
                task_id, False, f"helm pull 失败: {stderr}",
                data=base_data,
                error_type="HelmPullError",
                error_message=stderr,
            )

        # ── Step 2: helm upgrade --install 升级集群中的 release ──
        success, stdout, stderr = _helm_upgrade(
            release_name, save_path, namespace, kubeconfig_path,
            timeout=helm_timeout, atomic=helm_atomic == 1,
        )

        # ── 清理临时文件（无论成功失败都删）──
        _cleanup_temp_files(kubeconfig_path, save_path)

        if not success:
            return _build_result(
                task_id, False, f"helm upgrade 失败: {stderr}",
                data=base_data,
                error_type="HelmUpgradeError",
                error_message=stderr,
            )

        logging.info("[%s] Chart 升级成功: %s", _LOG, chart_file_name)

        return _build_result(task_id, True, f"Chart 升级成功: {chart_file_name}", data={
            **base_data,
            "status": "upgraded",
            "helm_upgrade_stdout": stdout,
        })

    except Exception as e:
        _cleanup_temp_files(kubeconfig_path, save_path)
        logging.error("[%s] 升级异常: %s", _LOG, e)
        return _build_result(
            task_id, False, f"升级异常: {e}",
            data=base_data,
            error_type=type(e).__name__,
            error_message=str(e),
            tb=traceback.format_exc(),
        )


# ── 自测入口 ──

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    test_kubeconfig = ""
    k8s_cfg = _CONFIG.get("k8s", {})
    kube_path = k8s_cfg.get("kubeconfig", "")
    if kube_path and os.path.exists(kube_path):
        with open(kube_path, "r", encoding="utf-8") as f:
            test_kubeconfig = f.read()

    result = container_restart_task({
        "task_id": "test-container-restart-001",
        "kubeconfig_content": test_kubeconfig,
        "chart_name": "mysql",
        "chart_version": "14.0.3",
        "release_name": "my-mysql",
        "namespace": "default",
    })

    print("\n" + "=" * 60)
    print("  容器升级任务结果")
    print("=" * 60)
    print(f"  task_id  : {result.get('task_id', '')}")
    print(f"  成功     : {result['success']}")
    print(f"  消息     : {result['message']}")
    data = result.get("data", {})
    if data:
        for key in ("chart_name", "chart_version", "chart_file", "release_name",
                     "namespace", "project", "status", "local_path", "oci_ref"):
            print(f"  {key}: {data.get(key, 'N/A')}")
    if not result["success"]:
        err = result.get("error", {})
        print(f"  错误类型 : {err.get('error_type', '')}")
        print(f"  错误信息 : {err.get('error_message', '')}")
    print("=" * 60)
