"""
显控台下载任务模块

与 download.py 结构完全对齐，唯一区别：
  1. 路径多一层 displayConsole/ 子目录
  2. task_type 为 xkt_download
  3. 成功状态码为 8（显控下载成功约定）

version 为必传参数：下载前 {file_name}/runtime/config.yaml 尚不存在，无法从配置文件读取版本号。
"""

import hashlib
import logging
import os
import re
import sys
import time
import traceback
from typing import Any, Dict, Optional

import requests
import yaml

from utils import util
from utils.config_loader import load_config

# 复用虚拟机下载的元信息解析与目录归位逻辑，保证三类应用（虚拟机/插件/显控台）
# 的落地结构完全一致，避免三份重复实现产生行为差异。
from core.task_utils.download import (
    read_app_meta,
    reorganize_to_standard_layout,
    cleanup_dir,
    write_app_id,
)


# ── YAML flow-style 辅助类：使内层 dict 输出为紧凑的 {} 格式 ──
class FlowDict(dict):
    """标记 dict 在 YAML dump 时使用 flow style（紧凑 JSON 风格）"""
    pass


def _flowdict_representer(dumper, data):
    return dumper.represent_mapping('tag:yaml.org,2002:map', data, flow_style=True)


yaml.add_representer(FlowDict, _flowdict_representer)

# =============================================================================
# 全局配置（模块加载时一次性读取 config.yaml，所有方法统一使用）
# =============================================================================

# 加载完整配置文件，load_config 内置缓存，多次调用也只会读取一次
_CONFIG = load_config()

# ── app_store 配置 ──
# app_store 用于 Linux 系统下载文件前的认证登录，获取 token
_APP_STORE_CFG = _CONFIG.get("app_store", {})
_APP_STORE_ADDRESS = _APP_STORE_CFG.get("address", "")  # 登录接口地址
_APP_STORE_USERNAME = _APP_STORE_CFG.get("username", "")  # 登录用户名
_APP_STORE_PASSWORD = _APP_STORE_CFG.get("password", "")  # 登录密码
_SERVER = _CONFIG.get("server", {})
_SERVER_IP = _SERVER.get("ip", "")
_SERVER_PORT = _SERVER.get("port", "")
# interface 是顶层节点，包含 registerUrl / deregisterUrl 等路径
_INTERFACE = _CONFIG.get("interface", {}) or {}
_INTERFACE_REGISTER_URL = _INTERFACE.get("registerUrl", "")
_INTERFACE_DEREGISTER_URL = _INTERFACE.get("deregisterUrl", "")
# ── server 配置 ──
_SERVER_CFG = _CONFIG.get("server", {})

# 默认下载目录（config.yaml 中 server.download 字段）
_DEFAULT_DOWNLOAD_PATH = _SERVER_CFG.get("download", "download")

# 显控台应用的子目录前缀（与一般下载的唯一路径差异）
_DISPLAY_CONSOLE_PREFIX = "displayConsole"


# =============================================================================
# 系统检测
# =============================================================================

def _is_windows() -> bool:
    """检测当前系统是否为 Windows"""
    return sys.platform == "win32"


def _is_linux() -> bool:
    """检测当前系统是否为 Linux"""
    return sys.platform.startswith("linux")


# =============================================================================
# 工具函数
# =============================================================================

def _get_app_store_config() -> dict:
    """
    获取 app_store 配置（直接返回模块级变量，无需重复读文件）

    返回:
        dict: app_store 配置字典
    """
    return _APP_STORE_CFG


def _login_app_store() -> str:
    """
    登录 app_store 获取 Token（使用模块级配置变量，无需每次调用 load_config()）

    返回:
        登录成功返回token，失败返回空字符串
    """
    # 直接使用模块级全局配置变量
    address = _APP_STORE_ADDRESS
    username = _APP_STORE_USERNAME
    password = _APP_STORE_PASSWORD

    if not address or not username or not password:
        logging.warning(f"[显控下载] app_store配置不完整，跳过登录 (address={address or '<空>'})")
        return ""

    try:
        logging.info(f"[显控下载] 尝试登录app_store: {address}")
        response = requests.post(
            address,
            json={"username": username, "password": password},
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            token = result.get("token", "")
            logging.info(f"[显控下载] app_store登录成功，获取Token (address={address})")
            return token
        else:
            logging.warning(f"[显控下载] app_store登录失败: HTTP {response.status_code}, address={address}")
            return ""

    except Exception as e:
        logging.error(f"[显控下载] 登录app_store异常: {e}, address={address}")
        return ""


def check_md5(file_path: str, expected_md5: str) -> bool:
    """
    检查文件的MD5值

    参数:
        file_path: 文件路径
        expected_md5: 期望的MD5值

    返回:
        bool: MD5是否匹配
    """
    import hashlib

    md5_hash = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest() == expected_md5


# =============================================================================
# 结果构建
# =============================================================================

def _build_download_result(
        task_id: str,
        result: bool,
        message: str,
        data: Optional[Dict[str, Any]] = None,
        error_type: str = "",
        error_message: str = "",
        tb: str = ""
) -> Dict[str, Any]:
    """
    构建显控下载任务统一返回结构，供 xkt_download_task 及其他下载相关方法复用。

    参数:
        task_id:      任务ID
        result:       执行结果 True/False
        message:      结果描述
        data:         附加数据字典
        error_type:   错误类型标识
        error_message:错误详细描述
        tb:           traceback 字符串

    返回:
        标准化的任务结果字典
    """
    payload = {
        "ip": util.get_ip() or "unknown",
        "task_id": task_id,
        "result": result,
        "task_type": "xkt_download",
        "status": 2 if result else 12,
        "message": message,
        "data": {
            **(data or {}),
            "error_type": error_type,
            "error_message": error_message,
            "traceback": tb
        }
    }
    return payload


# =============================================================================
# 读取 runtime/config.yaml
# =============================================================================
# 注：原 _read_runtime_config() 已删除。
#     相关逻辑在新架构中已失效（详见 download.py 同类说明），且无调用方。

# =============================================================================
# 参数校验
# =============================================================================

def _validate_and_extract_params(parameters: Dict[str, Any]) -> tuple:
    """
    校验必传参数并提取参数值。
    version 为必传参数：下载前 {file_name}/runtime/config.yaml 尚不存在，无法从配置文件读取版本号。

    返回:
        (error_result, params_dict)
        - 校验失败: (error_result, None)
        - 校验通过: (None, params_dict)
    """
    task_id = parameters.get('task_id', '')

    def _fail(msg: str, error_type: str, error_message: str):
        return (
            _build_download_result(task_id, False, msg,
                                   error_type=error_type, error_message=error_message),
            None
        )

    # 基础必传参数
    if not task_id:
        logging.error("[显控下载] 参数缺失: task_id")
        return _fail("参数缺失: task_id", "ParameterMissing", "task_id 参数缺失")

    download_url = parameters.get('download_url', '')
    if not download_url:
        logging.error("[显控下载] 参数缺失: download_url")
        return _fail("参数缺失: download_url", "ParameterMissing",
                     "download_url 参数缺失")

    # save_path 从 config.yaml 的 server.download 读取，不从参数传入
    save_path = _DEFAULT_DOWNLOAD_PATH
    if not save_path:
        logging.error("[显控下载] config.yaml 中未配置 server.download")
        return _fail("配置缺失: server.download", "ConfigMissing",
                     "config.yaml 中未配置 server.download")

    custom_file_name = str(parameters.get('file_name', '') or '').strip()
    if not custom_file_name:
        logging.error("[显控下载] 参数缺失: file_name")
        return _fail("参数缺失: file_name", "ParameterMissing", "file_name 参数缺失")

    # 兼容旧字段名 suffix（xkt_upgrade.py 仍用该字段），新流程统一为 file_suffix
    file_suffix = parameters.get('file_suffix', '') or parameters.get('suffix', '')
    if not file_suffix:
        logging.error("[显控下载] 文件后缀参数缺失: file_suffix")
        return _fail("参数缺失: file_suffix", "ParameterMissing", "file_suffix 参数缺失")

    # version 为必传参数：下载前 runtime/config.yaml 尚不存在，版本号必须由调用方显式传入
    version = str(parameters.get('version', '') or '').strip()
    if not version:
        logging.error("[显控下载] 参数缺失: version")
        return _fail("参数缺失: version", "ParameterMissing",
                     "version 参数缺失，下载前无法从 runtime/config.yaml 读取版本")

    params = {
        'task_id': task_id,
        'download_url': download_url,
        'save_path': save_path,
        'file_name': custom_file_name,
        'file_suffix': file_suffix,
        'version': version,
        # 平台下发的应用记录 ID（写入包内 config/app.yaml，键名 appId）
        'app_id': str(parameters.get('app_id', '') or '').strip(),
    }

    return None, params


# =============================================================================
# 服务数据构建
# =============================================================================

def _build_service_data(file_name: str, version: str = "") -> Dict[str, Any]:
    """构建服务相关字段（组件尚未下载，无 runtime/config.yaml 可读，使用默认值），用于填充返回结果的 data 字段"""
    return {
        "displayName": file_name,
        "version": version,
        "description": "",
        "serviceName": file_name,
        "groupName": "DEFAULT_GROUP",
        "clusterName": "DEFAULT",
        "weight": 1.0,
        "healthy": True,
        "enabled": True,
        "ephemeral": True,
        "metadata": {},
    }


# =============================================================================
# 下载环境准备（目录、版本检查、application.yml）
# =============================================================================

def _prepare_download_environment(
        task_id: str,
        save_path: str,
        file_name: str,
        version: str,
        sub_dir: str = "displayConsole",
) -> tuple:
    """
    准备显控下载目录结构：找 {sub_dir}/file_name 目录（不存在则创建）、找版本号目录（不存在则创建）。
    版本号目录下已有文件时直接覆盖重新下载（与 download.py 行为一致）。
    版本号仅从参数传入（下载前 {file_name}/runtime/config.yaml 尚不存在，无法读取）。
    路径比 download.py 多一层 {sub_dir}/（显控台=displayConsole，插件应用=plugin）。

    参数:
        task_id:        任务ID
        save_path:      保存根目录
        file_name:      文件名（不含后缀），即服务名
        version:        版本号（参数必传）
        sub_dir:        专属子目录（默认 displayConsole）

    返回:
        (error_result, save_dir, version_dir)
        - 出错: (error_result, "", "")
        - 正常: (None, save_dir, version_dir)
    """
    # ── 版本号：仅从参数取（下载前 runtime/config.yaml 尚不存在）──
    version = (version or "").strip()
    if not version:
        return (
            _build_download_result(task_id, False, "缺少版本号: 未传入 version 参数",
                                   error_type="VersionMissing",
                                   error_message="下载前无法读取 runtime/config.yaml，version 必须由参数显式传入"),
            "", ""
        )

    # 检查 save_path 是否为目录
    if os.path.exists(save_path) and not os.path.isdir(save_path):
        logging.error(f"[显控下载] save_path 不是目录: {save_path}")
        return (
            _build_download_result(task_id, False, "save_path 必须为目录路径",
                                   error_type="InvalidSavePath",
                                   error_message="save_path 必须为目录路径，不能是文件路径"),
            "", ""
        )

    # 创建 save_path/{sub_dir}/file_name 子文件夹（不存在则创建）
    save_dir = os.path.join(save_path, sub_dir, file_name)
    os.makedirs(save_dir, exist_ok=True)

    # 版本号目录：不存在则创建
    version_dir = os.path.join(save_dir, version)
    os.makedirs(version_dir, exist_ok=True)

    # 版本号目录下已有文件 → 直接覆盖重新下载（不再提示已存在）
    if os.listdir(version_dir):
        logging.info(f"[显控下载] 版本目录下已有文件，将直接覆盖重新下载: {version_dir}")

    logging.info(f"[显控下载] 创建版本目录: {version_dir}")

    # ── 不再生成 version 文件与 application.yml ──
    # 下载区仅作「缓存」：只落应用包解压后的原始内容（app/ bin/ config/）。
    # 版本信息由 {save_dir}/{version}/ 目录名体现；
    # 运行状态与 Nacos 配置改由 install 阶段在「运行区」生成。

    return None, save_dir, version_dir


# =============================================================================
# 文件下载（含重试）
# =============================================================================

def _download_file(
        task_id: str,
        download_url: str,
        target_file_path: str,
        retry: int,
        timeout: int,
        token: str,
) -> Dict[str, Any]:
    """
    执行文件下载，支持重试机制和完整性校验。

    返回:
        标准化的任务结果字典
    """
    tmp_path = target_file_path + ".tmp"

    # URL 容错: app_store 下载接口第一段路径拼写纠正（下发方曾用 appStrore/appstore，正确为 appStore）
    m = re.match(r'^(https?://[^/]+/)app(store|Strore)/', download_url, flags=re.IGNORECASE)
    if m and m.group(2) != "Store":
        corrected_url = m.group(1) + "appStore/" + download_url[m.end():]
        logging.warning(f"[显控下载] 下载URL拼写错误({m.group(2)})已纠正为: {corrected_url}")
        download_url = corrected_url

    # 清理残留临时文件
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
            logging.info(f"[显控下载] 删除残留临时文件: {tmp_path}")
        except Exception as e:
            logging.warning(f"[显控下载] 删除残留临时文件失败: {e}")

    # 覆盖已存在文件
    if os.path.exists(target_file_path):
        logging.info(f"[显控下载] 文件已存在，将覆盖重新下载: {target_file_path}")
        os.remove(target_file_path)

    # 下载重试循环
    for attempt in range(retry + 1):
        try:
            logging.info(f"[显控下载] 下载尝试 {attempt + 1}/{retry + 1}")

            headers = {}
            if token:
                headers["token"] = token

            response = requests.get(download_url, headers=headers, stream=True, timeout=timeout)
            response.raise_for_status()

            with open(tmp_path, "wb") as f:
                for chunk in response.iter_content(8192):
                    if chunk:
                        f.write(chunk)

            # 大小校验
            actual_size = os.path.getsize(tmp_path)
            if actual_size == 0:
                raise ValueError("下载文件大小为0，可能是空文件")

            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                expected_size = int(content_length)
                if actual_size != expected_size:
                    raise ValueError(
                        f"下载文件不完整: 期望大小 {expected_size} 字节, 实际大小 {actual_size} 字节"
                    )
            logging.info(f"[显控下载] 完整性校验通过，文件大小: {actual_size} 字节")

            # 移动到最终位置
            os.replace(tmp_path, target_file_path)
            file_size = os.path.getsize(target_file_path)
            logging.info(f"[显控下载] 下载成功: {target_file_path}, 大小: {file_size} bytes")

            return _build_download_result(task_id, True, "下载成功", data={
                "status": "downloaded",
                "file_path": target_file_path,
                "file_size": file_size,
                "attempts": attempt + 1,
            })

        except Exception as e:
            logging.error(f"[显控下载] 下载失败 (尝试 {attempt + 1}): {e}")

            cleanup_success = False
            cleanup_error = ""
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                    cleanup_success = True
                except Exception as rm_err:
                    cleanup_error = str(rm_err)

            if attempt < retry:
                logging.info("[显控下载] 2秒后重试...")
                time.sleep(2)
                continue

            logging.error(f"[显控下载] 下载任务失败，已重试 {retry} 次")
            return _build_download_result(task_id, False, f"下载失败: {str(e)}", data={
                "download_url": download_url,
                "file_path": target_file_path,
                "attempts": retry + 1,
                "cleanup_result": {"success": cleanup_success, "error": cleanup_error}
            }, error_type=type(e).__name__, error_message=str(e), tb=traceback.format_exc())

    return _build_download_result(task_id, False, "下载失败，超出重试次数", data={
        "download_url": download_url,
        "file_path": target_file_path,
        "attempts": retry + 1
    }, error_type="MaxRetriesExceeded", error_message="超出最大重试次数")


# =============================================================================
# 压缩包解压
# =============================================================================

def _restore_zip_permissions(zf: Any, dest_dir: str) -> None:
    """
    恢复 zip 内文件的 Unix 权限位。

    Python zipfile.extractall 不会恢复 external_attr 中的权限位（尤其是
    Windows 上打出的 zip），导致解压后的脚本/二进制失去执行权限，后续
    start/stop 脚本直接执行二进制时会报 Permission denied。这里按 zip
    内记录的 Unix 模式位重新 chmod。

    参数:
        zf:       已打开的 zipfile.ZipFile 对象
        dest_dir: 解压目标目录
    """
    try:
        for info in zf.infolist():
            if info.is_dir():
                continue
            # external_attr 高 16 位为 Unix 权限位（与 unzip 命令行为一致）
            mode = (info.external_attr >> 16) & 0o7777
            if not mode:
                continue
            target = os.path.join(dest_dir, info.filename)
            if os.path.exists(target):
                try:
                    os.chmod(target, mode)
                except OSError as e:
                    logging.warning("[显控下载] 恢复文件权限失败 %s: %s", target, e)
    except Exception as e:
        logging.warning("[显控下载] 恢复 zip 文件权限异常: %s", e)


def _extract_archive(archive_path: str, dest_dir: str) -> bool:
    """
    解压压缩包到目标目录。

    参数:
        archive_path: 压缩包路径
        dest_dir:     解压目标目录（不存在会自动创建）

    返回:
        bool: 解压成功返回 True，否则返回 False
    """
    lower = archive_path.lower()
    try:
        os.makedirs(dest_dir, exist_ok=True)
        if lower.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(dest_dir)
                # Python zipfile 不恢复 Unix 权限位，需手动恢复
                _restore_zip_permissions(zf, dest_dir)
        elif lower.endswith(".tar.gz") or lower.endswith(".tgz"):
            import tarfile
            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(dest_dir)
        elif lower.endswith(".tar.bz2") or lower.endswith(".tbz2"):
            import tarfile
            with tarfile.open(archive_path, "r:bz2") as tf:
                tf.extractall(dest_dir)
        elif lower.endswith(".tar"):
            import tarfile
            with tarfile.open(archive_path, "r:") as tf:
                tf.extractall(dest_dir)
        else:
            logging.warning("[显控下载] 不支持解压的压缩格式，跳过解压: %s", archive_path)
            return False
        logging.info("[显控下载] 解压成功: %s -> %s", archive_path, dest_dir)
        return True
    except Exception as e:
        logging.error("[显控下载] 解压失败: %s -> %s: %s", archive_path, dest_dir, e)
        return False
# 注：原 _strip_single_top_dir() 已删除。
#     相关逻辑在新架构中已失效（详见 download.py 同类说明），且无调用方。

# =============================================================================
# 显控下载任务（主入口）
# =============================================================================

def xkt_download_task(parameters: Dict[str, Any], retry: int = 0, timeout: int = 300) -> Dict[str, Any]:
    """
    显控下载任务 — 主入口。

    与 download.py 的 download_task 结构完全对齐，路径多一层 displayConsole/。

    处理流程: 参数校验 → 环境准备 → 文件下载 → 解压

    必传参数:
        task_id, download_url, file_suffix, file_name, version

    version 必须由调用方显式传入：下载前 {file_name}/runtime/config.yaml 尚不存在，无法读取版本号。
    save_path 从 config.yaml 的 server.download 读取。
    """
    # ── 1. 参数校验与提取 ──
    error, params = _validate_and_extract_params(parameters)
    if error:
        return error

    task_id = params['task_id']
    save_path = params['save_path']
    file_name = params['file_name']
    file_suffix = params['file_suffix']
    version = params.get('version', '')
    sub_dir = str(parameters.get("sub_dir", "") or "displayConsole").strip()

    # ── 2. 版本号：仅从参数取（下载前 runtime/config.yaml 尚不存在）──
    if not version:
        return _build_download_result(task_id, False,
                                      "缺少版本号: 未传入 version 参数",
                                      data={"file_name": file_name},
                                      error_type="VersionMissing",
                                      error_message="下载前无法读取 runtime/config.yaml，version 必须由参数显式传入")

    # ── 3. 准备下载环境（目录、版本检查、application.yml）──
    error, save_dir, version_dir = _prepare_download_environment(
        task_id, save_path, file_name, version, sub_dir
    )
    if error:
        # 合并服务数据到返回结果
        error['data'] = {**error.get('data', {}), **_build_service_data(file_name, version)}
        return error

    # ── 4. 获取 Token（Linux 必需）──
    token = ""
    if _is_linux():
        token = _login_app_store()
        if not token:
            file_name_with_suffix = file_name + file_suffix
            target_file_path = os.path.join(version_dir, file_name_with_suffix)
            logging.error("[显控下载] Linux系统获取token失败，无法下载")
            return _build_download_result(task_id, False,
                                          "获取token失败，Linux系统无法下载",
                                          data={
                                              "download_url": params['download_url'],
                                              "save_path": save_path,
                                              "file_name": file_name_with_suffix,
                                              "file_path": target_file_path,
                                              **_build_service_data(file_name, version)
                                          },
                                          error_type="TokenRequired",
                                          error_message=f"Linux系统必须登录app_store获取token，请检查app_store配置(app_store.address={_APP_STORE_ADDRESS})"
                                          )
        logging.info("[显控下载] Linux系统，已获取token")
    else:
        logging.info("[显控下载] Windows系统，跳过token认证")

    # ── 5. 拼接文件名并下载到临时位置 ──
    # 与虚拟机/插件下载一致：先下到临时目录，解压读出 app.yaml 的 name/version 后再归位。
    if not file_suffix.startswith('.'):
        file_suffix = '.' + file_suffix
    file_name_with_suffix = file_name + file_suffix

    staging_dir = os.path.join(save_path, ".staging", f"{sub_dir}-{file_name}-{int(time.time())}")
    try:
        os.makedirs(staging_dir, exist_ok=True)
    except Exception as e:
        return _build_download_result(task_id, False,
                                      f"创建临时目录失败: {e}",
                                      data={"staging_dir": staging_dir},
                                      error_type="StagingDirError", error_message=str(e))
    target_file_path = os.path.join(staging_dir, file_name_with_suffix)

    logging.info(f"[显控下载] 下载文件: {params['download_url']} -> {target_file_path}"
                 f", 重试: {retry}, 超时: {timeout}s")

    result = _download_file(task_id, params['download_url'], target_file_path, retry, timeout, token)
    if not result.get("result"):
        cleanup_dir(staging_dir)
        result['data'] = {
            "download_url": params['download_url'],
            "save_path": save_path,
            "file_name": file_name_with_suffix,
            **result.get('data', {}),
            **_build_service_data(file_name, version)
        }
        return result

    # ── 6. 解压到临时目录 ──
    extract_dir = os.path.join(staging_dir, "extract")
    if not _extract_archive(target_file_path, extract_dir):
        cleanup_dir(staging_dir)
        return _build_download_result(task_id, False,
                                      f"解压失败: {target_file_path}",
                                      data={
                                          "download_url": params['download_url'],
                                          "save_path": save_path,
                                          "file_name": file_name_with_suffix,
                                          "file_path": target_file_path,
                                          **_build_service_data(file_name, version)
                                      },
                                      error_type="ExtractError",
                                      error_message="下载成功但解压失败，请检查压缩包格式")

    # 解压成功即可删除压缩包
    try:
        os.remove(target_file_path)
        logging.info("[显控下载] 已删除压缩包: %s", target_file_path)
    except Exception as rm_err:
        logging.warning("[显控下载] 删除压缩包失败: %s", rm_err)

    # ── 7. 从 app.yaml 读取权威的应用名与版本号 ──
    meta = read_app_meta(extract_dir)
    meta_name = meta.get("name", "")
    meta_version = meta.get("version", "")
    # 应用名与版本号一律以「平台下发参数」为准，不用 app.yaml 覆盖。
    # 原因：后续 install/start/stop/upgrade/uninstall 均用平台的应用名与版本号
    #       定位目录（见 InstanceServiceImpl 各任务均取 app.getAppName()），
    #       此处若按 app.yaml 改名会导致定位失败（下载落地 nacos/、安装找 nacos-xkt/）。
    if meta_name and meta_name != file_name:
        logging.warning("[显控下载] app.yaml 应用名(%s) 与平台应用名(%s) 不一致，"
                        "以平台为准归位", meta_name, file_name)
    if meta_version and meta_version != version:
        logging.warning("[显控下载] app.yaml 版本号(%s) 与平台版本号(%s) 不一致，"
                        "以平台为准归位", meta_version, version)
    final_name = file_name
    final_version = version

    # ── 8. 归位到 {download}/{sub_dir}/{name}/{version}/ ──
    try:
        final_version_dir = reorganize_to_standard_layout(
            extract_dir, save_path, final_name, final_version, sub_dir
        )
    except Exception as e:
        logging.error("[显控下载] 归位失败: %s", e, exc_info=True)
        cleanup_dir(staging_dir)
        return _build_download_result(task_id, False,
                                      f"整理目录结构失败: {e}",
                                      data={
                                          "download_url": params['download_url'],
                                          "save_path": save_path,
                                          "extract_dir": extract_dir,
                                          **_build_service_data(file_name, version)
                                      },
                                      error_type="ReorganizeError", error_message=str(e),
                                      tb=traceback.format_exc())

    cleanup_dir(staging_dir)

    # ── 9. 下载成功：把平台下发的应用记录 ID 写入包内 config/app.yaml（键名 appId）──
    write_app_id(final_version_dir, params.get("app_id"))

    result['data']["status"] = "downloaded_and_extracted"
    result['data']["extracted_dir"] = final_version_dir
    # 合并通用数据和服务数据到返回结果（服务信息以 app.yaml 解析结果为准）
    result['data'] = {
        "download_url": params['download_url'],
        "save_path": save_path,
        "file_name": file_name_with_suffix,
        **result.get('data', {}),
        **_build_service_data(final_name, final_version)
    }
    return result


if __name__ == "__main__":
    import json

    # ── 配置日志 ──
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    test_save_path = r"D:\bontor\tools" if _is_windows() else "/tmp/test_download"
    test_file_name = "hellogitworld-master"
    test_version = "1.0.0"

    # ── 执行下载（version 必须显式传入，下载前不存在 runtime/config.yaml）──
    result = xkt_download_task({
        "task_id": "test-xkt-download-001",
        "download_url": "https://github.com/githubtraining/hellogitworld/archive/refs/heads/master.zip",
        "file_name": test_file_name,
        "file_suffix": ".zip",
        "version": test_version,
    })

    # ── 输出结果 ──
    print("\n" + "=" * 60)
    print("  显控下载任务结果")
    print("=" * 60)
    print(f"  状态: {'成功' if result['result'] else '失败'}")
    print(f"  消息: {result['message']}")
    if result.get("data"):
        d = result["data"]
        if d.get("status"):
            print(f"  详情: {d['status']}")
        if d.get("file_path"):
            print(f"  文件: {d['file_path']}")
    print("=" * 60)

    # ── 打印生成的文件结构 ──
    save_dir = os.path.join(test_save_path, "displayConsole", test_file_name)
    if os.path.isdir(save_dir):
        print("\n文件结构:")
        for root, dirs, files in os.walk(save_dir):
            level = root.replace(save_dir, "").count(os.sep)
            indent = "  " * level
            print(f"  {indent}{os.path.basename(root)}/")
            sub_indent = "  " * (level + 1)
            for f in files:
                fpath = os.path.join(root, f)
                size = os.path.getsize(fpath)
                print(f"  {sub_indent}{f}  ({size} bytes)")

    # ── 打印 application.yml 内容 ──
    yml_path = os.path.join(save_dir, test_version, "application.yml")
    if os.path.isfile(yml_path):
        print(f"\n{yml_path}:")
        print("-" * 60)
        with open(yml_path, "r", encoding="utf-8") as yf:
            print(yf.read(), end="")
        print("-" * 60)
