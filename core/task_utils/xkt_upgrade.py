"""
core/task_utils/xkt_upgrade.py - 显控升级模块

模型 A（与 xkt_download / xkt_stop / xkt_start / xkt_Install / xkt_uninstall 一致）：
    {download}/displayConsole/{service_name}/
    ├── version                     # 当前版本号文件
    └── {version}/
        ├── bin/{start,stop,install,uninstall}.sh
        └── runtime/pid

升级流程 = 停止旧版本 + 下载新版本 + 启动新版本：
    1. 读 version 文件获取当前版本
    2. 调用 xkt_stop_task 停止当前版本（读 version 文件定位 bin/stop.sh）
    3. 调用 xkt_download_task 下载并部署新版本（创建 {service_dir}/{version}/，写 version 文件）
    4. 调用 xkt_start_task 启动新版本（读 version 文件定位新版本 bin/start.sh）
    任一步失败 → 回滚：删除新版本目录、恢复 version 文件、重启旧版本。
"""

import logging
import os
import shutil
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.xkt.process_check import collect_service_pids
from utils import util
from utils.config_loader import load_config
from core.task_utils.xkt_download import xkt_download_task
from core.task_utils.xkt_stop import xkt_stop_task
from core.task_utils.xkt_start import xkt_start_task

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")


# =============================================================================
# 工具函数
# =============================================================================

def _read_runtime_pids(version_dir: str) -> List[int]:
    """读取 {version_dir}/runtime/pid 中的 PID 列表（用于升级前后对比）"""
    pid_file = os.path.join(version_dir, "runtime", "pid")
    pids: List[int] = []

    if not os.path.isfile(pid_file):
        return pids

    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            for line in f.read().splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
    except Exception as e:
        logging.warning("[显控升级] 读取 runtime/pid 失败: %s -> %s", pid_file, e)

    return pids


def _build_result(success: bool, message: str, data: Optional[Dict[str, Any]] = None,
                  error_type: str = "", error_message: str = "", tb: str = "") -> Dict[str, Any]:
    """构建任务结果"""
    return {
        "ip": util.get_ip() or "unknown",
        "task_id": "",
        "result": success,
        # 3=已启动 / 7=升级失败（原为 13，而 13 在平台枚举里是“回滚失败”，
        # 会让“升级失败”在页面上显示成“回滚失败”）
        "status": 3 if success else 7,
        "task_type": "xkt_upgrade",
        "message": message,
        "data": {
            **(data or {}),
            "error_type": error_type,
            "error_message": error_message,
            "traceback": tb,
        },
    }


def _task_ok(result: Dict[str, Any]) -> bool:
    """兼容子任务 success / result 两种返回格式"""
    return bool(result.get("success") or result.get("result"))


def _read_current_version(component_dir: str) -> str:
    """读取组件目录下的 version 文件"""
    version_file = os.path.join(component_dir, "version")
    if not os.path.isfile(version_file):
        return ""
    try:
        with open(version_file, "r", encoding="utf-8") as vf:
            return vf.read().strip()
    except Exception:
        return ""


def _service_running(component_dir: str, version: str) -> bool:
    """通过 runtime/pid 文件判断服务是否在运行"""
    pid_file = os.path.join(component_dir, version, "runtime", "pid")
    if not os.path.isfile(pid_file):
        return False
    try:
        with open(pid_file, "r", encoding="utf-8") as pf:
            return bool(pf.read().strip())
    except Exception:
        return False


def _rollback(component_dir: str, old_version: str, new_version: str,
              task_id: str, service_name: str, sub_dir: str, was_running: bool) -> None:
    """升级失败回滚：删除新版本目录 → 恢复 version 文件 → 旧版本原来在运行则重启"""
    try:
        target_dir = os.path.join(component_dir, new_version)
        if os.path.isdir(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
            logging.info("[xkt_upgrade] 已删除新版本目录: %s", target_dir)
        version_file = os.path.join(component_dir, "version")
        with open(version_file, "w", encoding="utf-8") as vf:
            vf.write(old_version)
        logging.info("[xkt_upgrade] 已恢复 version 文件: %s", old_version)
        if was_running:
            xkt_start_task({"task_id": task_id, "service_name": service_name, "sub_dir": sub_dir})
            logging.info("[xkt_upgrade] 已尝试重启旧版本 %s", old_version)
    except Exception as e:
        logging.warning("[xkt_upgrade] 回滚过程出错: %s", e)


# =============================================================================
# 主方法
# =============================================================================

def xkt_upgrade_task(parameters: Dict[str, Any], retry: int = 2, timeout: int = 300) -> Dict[str, Any]:
    """
    显控台升级任务（模型 A）

    流程: 解析参数 → 校验(5个必填) → 组件目录存在性 → 读 version → 版本比对
    → 停止当前版本(xkt_stop) → 下载新版本(xkt_download) → 启动新版本(xkt_start)
    → 任一步失败则回滚（删新版本目录 + 恢复 version + 重启旧版本）

    参数:
        - task_id:       任务ID（必填）
        - service_name:  服务名称（必填），同时也是组件目录名
        - download_url:  新版本下载地址（必填）
        - file_suffix:   下载文件后缀（必填，兼容旧字段名 suffix）
        - version:       目标版本号（必填）
        - sub_dir:       专属子目录（可选，默认 displayConsole）
    """
    task_id = str(parameters.get('task_id', '') or '').strip()

    def build_result(success: bool, message: str, data: Optional[Dict[str, Any]] = None,
                     error_type: str = "", error_message: str = "", tb: str = "") -> Dict[str, Any]:
        result = _build_result(success, message, data, error_type, error_message, tb)
        result['task_id'] = task_id
        return result

    # ── 1. 解析参数 ──
    service_name = str(parameters.get('service_name', '') or '').strip()
    download_url = str(parameters.get('download_url', '') or '').strip()
    file_suffix = str(parameters.get('file_suffix', '') or parameters.get('suffix', '') or '').strip()
    version = str(parameters.get('version', '') or '').strip()
    sub_dir = str(parameters.get('sub_dir', '') or 'displayConsole').strip()

    # ── 2. 参数校验（5 个必填）──
    missing = [k for k, v in {
        'task_id': task_id, 'service_name': service_name,
        'download_url': download_url, 'file_suffix': file_suffix, 'version': version,
    }.items() if not v]
    if missing:
        logging.error("[显控升级] 参数缺失: %s", ", ".join(missing))
        return build_result(False, f"参数缺失: {', '.join(missing)}",
                            error_type="ParameterMissing",
                            error_message=f"缺少必填参数: {', '.join(missing)}")

    # ── 3. 组件目录 ──
    if not _DOWNLOAD_BASE:
        return build_result(False, "config.yaml 中未配置 server.download",
                            error_type="ConfigMissing", error_message="server.download 未配置")
    component_dir = os.path.join(_DOWNLOAD_BASE, sub_dir, service_name)
    if not os.path.isdir(component_dir):
        logging.error("[显控升级] %s 组件目录不存在: %s", sub_dir, component_dir)
        return build_result(False, f"服务目录不存在，请先下载安装: {component_dir}",
                            error_type="FileNotFoundError",
                            error_message=f"{sub_dir} 组件目录不存在: {component_dir}")

    # ── 4. 读当前版本 ──
    current_version = _read_current_version(component_dir)
    if not current_version:
        return build_result(False, "无法读取当前版本号(version 文件缺失或为空)",
                            error_type="VersionEmpty", error_message="version 文件缺失或为空")

    # ── 5. 版本比对 ──
    if current_version == version:
        return build_result(False, f"当前已是目标版本 {version}，无需升级",
                            error_type="VersionConflict", error_message="当前版本与目标版本相同")
    target_dir = os.path.join(component_dir, version)
    if os.path.isdir(target_dir):
        return build_result(False, f"目标版本目录已存在，请先回滚或卸载: {target_dir}",
                            error_type="VersionExists",
                            error_message=f"目标版本 {version} 目录已存在，无法重复升级")

    # ── 6. 记录旧版本运行状态（供失败回滚）──
    was_running = _service_running(component_dir, current_version)
    logging.info("[显控升级] 当前版本 %s, 目标版本 %s, 旧版本运行中: %s",
                 current_version, version, was_running)

    # ── 7. 停止当前版本 ──
    logging.info("[显控升级] 停止当前版本 %s", current_version)
    stop_result = xkt_stop_task({"task_id": task_id, "service_name": service_name, "sub_dir": sub_dir})
    if not _task_ok(stop_result):
        logging.error("[显控升级] 停止旧版本失败: %s", stop_result.get("message", ""))
        return build_result(False, f"停止旧版本失败: {stop_result.get('message', '')}",
                            error_type="StopError",
                            error_message=str(stop_result.get("error", {}).get("error_message", ""))
                            or stop_result.get("message", ""), tb=traceback.format_exc())

    # ── 8. 下载并部署新版本 ──
    logging.info("[显控升级] 下载新版本 %s", version)
    download_result = xkt_download_task({
        "task_id": task_id, "download_url": download_url, "file_name": service_name,
        "file_suffix": file_suffix, "version": version, "sub_dir": sub_dir,
    })
    if not _task_ok(download_result):
        logging.error("[显控升级] 下载新版本失败: %s", download_result.get("message", ""))
        _rollback(component_dir, current_version, version, task_id, service_name, sub_dir, was_running)
        return build_result(False, f"下载新版本失败: {download_result.get('message', '')}",
                            error_type="DownloadError",
                            error_message=str(download_result.get("error", {}).get("error_message", ""))
                            or download_result.get("message", ""), tb=traceback.format_exc())

    # ── 9. 启动新版本 ──
    logging.info("[显控升级] 启动新版本 %s", version)
    start_result = xkt_start_task({"task_id": task_id, "service_name": service_name, "sub_dir": sub_dir})
    if not _task_ok(start_result):
        logging.error("[显控升级] 启动新版本失败: %s", start_result.get("message", ""))
        _rollback(component_dir, current_version, version, task_id, service_name, sub_dir, was_running)
        return build_result(False, f"启动新版本失败，已回滚到 {current_version}: {start_result.get('message', '')}",
                            error_type="StartError",
                            error_message=str(start_result.get("error", {}).get("error_message", ""))
                            or start_result.get("message", ""), tb=traceback.format_exc())

    # ── 10. 刷新 runtime/pid（把旧版本 PID 换成新版本进程 PID）──
    # xkt_start 内部已按"实际在跑的进程"回写一次，这里再兜底刷新并回传新旧 PID，
    # 便于现场核对本次升级是否真的把 PID 换掉了
    old_version_dir = os.path.join(component_dir, current_version)
    new_version_dir = os.path.join(component_dir, version)
    old_pids = _read_runtime_pids(old_version_dir)
    new_pids = collect_service_pids(component_dir, new_version_dir) or old_pids

    new_pid_file = os.path.join(new_version_dir, "runtime", "pid")
    pid_written = False
    if new_pids:
        try:
            Path(os.path.dirname(new_pid_file)).mkdir(parents=True, exist_ok=True)
            with open(new_pid_file, "w", encoding="utf-8") as f:
                f.write("\n".join(str(p) for p in new_pids))
            pid_written = True
        except Exception as e:
            logging.warning("[显控升级] 回写 runtime/pid 失败: %s -> %s", new_pid_file, e)
    logging.info("[显控升级] runtime/pid 已刷新: %s → %s", old_pids, new_pids)

    # ── 11. 成功 ──
    logging.info("[显控升级] 完成: %s %s → %s", service_name, current_version, version)
    return build_result(True, f"显控升级完成: {service_name} {current_version} → {version}", data={
        "service_name": service_name,
        "old_version": current_version,
        "new_version": version,
        "old_pids": old_pids,
        "new_pids": new_pids,
        "pid_file": new_pid_file,
        "pid_written": pid_written,
        "sub_dir": sub_dir,
        "component_dir": component_dir,
    })


# ── 自测入口 ──
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    result = xkt_upgrade_task({
        "task_id": "test-xkt-upgrade-001",
        "service_name": "test-service",
        "download_url": "http://127.0.0.1:8099/nginx-ruoyi-v2.zip",
        "file_suffix": "zip",
        "version": "v2",
    })
    print("\n" + "=" * 60)
    print("  显控升级任务结果")
    print("=" * 60)
    print(f"  task_id : {result.get('task_id', '')}")
    print(f"  成功    : {result['result']}")
    print(f"  消息    : {result['message']}")
    print("=" * 60)
