"""
版本管理相关的 API 路由

【改造说明】
· 可用版本列表 → 扫「缓存区」{download}/{service_name}/ 下的版本目录（缓存区存各版本原始内容）
· 当前运行版本 → 读「运行区」{apps}/{service_name}/state/config.yaml 的 version 字段
  （原从 {download}/{service_name}/version 文件读取，该文件已不再生成）
· 每个版本对应的「平台应用记录 ID」→ 读该版本目录下 config/app.yaml 的 appId
  （下载成功时由 write_app_id() 写入；旧版本目录可能没有该字段）
· 当前正在运行的版本**不再从列表中剔除**，而是由调用方用 current_version 对比后标记「正在使用」
"""
import logging
import os
import shutil
from typing import List

import yaml
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from utils.app_path import resolve_sub_dir, read_state

router = APIRouter()

# 应用包内元信息文件（版本目录下）
_META_DIR = "config"
_META_FILE = "app.yaml"


class VersionListResponse(BaseModel):
    """版本列表响应模型"""
    service_name: str
    versions: List[str]
    app_ids: List[str] = []
    # 与 versions 一一对应的目录占用字节数（供「清除缓存」弹窗展示）
    sizes: List[int] = []
    current_version: str = None
    count: int


class ClearCacheRequest(BaseModel):
    """清除缓存请求模型"""
    service_name: str
    # 可选：只清某个版本；与 versions 二选一
    version: str = ""
    # 可选：要清除的版本列表（前端多选）；不传 version 也不传 versions = 清该应用全部已下载版本
    versions: List[str] = []


def _read_app_id(version_dir: str) -> str:
    """
    读取「版本目录」下 config/app.yaml 的 appId（平台应用记录 ID）。

    下载成功时 Agent 会把平台下发的 appId 写进这里（见 download.write_app_id），
    因此老版本目录可能读不到，此时返回空串，由平台侧按「应用名 + 版本号」兜底查询。
    """
    meta_path = os.path.join(version_dir, _META_DIR, _META_FILE)
    try:
        if not os.path.isfile(meta_path):
            return ""
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = yaml.safe_load(f) or {}
        if not isinstance(meta, dict):
            return ""
        return str(meta.get("appId", "") or "").strip()
    except Exception as e:
        logging.warning("[版本列表] 读取 app.yaml 的 appId 失败: %s -> %s", meta_path, e)
        return ""


@router.get("/list", response_model=dict)
async def get_service_versions(
    service_name: str = Query(..., description="应用名称")
):
    """
    根据应用名称，返回该应用下所有版本文件夹的名称。

    返回字段：
        versions        全部版本（**包含**正在运行的那个，不再剔除）
        app_ids         与 versions 一一对应的平台应用记录 ID（读不到为空串）
        current_version 当前运行版本（来自运行区状态文件），供调用方标记「正在使用」
    """
    from core.utils import load_config

    config = load_config()
    download_dir = config.get("server", {}).get("download", "")

    if not download_dir:
        raise HTTPException(status_code=500, detail="下载目录未配置")

    # 构建服务目录路径（缓存区）
    # 显控台/插件在缓存区多一层（displayConsole/plugin），先探测所在层
    sub_dir = resolve_sub_dir(service_name)
    app_dir = os.path.join(download_dir, sub_dir, service_name) if sub_dir \
        else os.path.join(download_dir, service_name)

    if not os.path.exists(app_dir):
        raise HTTPException(
            status_code=404,
            detail=f"服务 '{service_name}' 的目录不存在: {app_dir}"
        )

    # 列出服务目录下所有的子文件夹作为版本号
    try:
        versions = [
            name for name in os.listdir(app_dir)
            if os.path.isdir(os.path.join(app_dir, name))
            and not os.path.islink(os.path.join(app_dir, name))
        ]
        # 按版本号排序（降序，新版本在前）
        versions.sort(reverse=True)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"读取版本列表失败: {str(e)}"
        )

    # 读取当前正在运行的版本（来自运行区状态文件）。
    # 注意：**不再从列表里剔除**，改由调用方对比 current_version 后标记「正在使用」。
    current_version = None
    run_state = read_state(service_name, sub_dir)
    if run_state and run_state.get("runtime"):
        current_version = str(run_state.get("version", "") or "").strip() or None

    # 各版本对应的平台应用记录 ID：读各自版本目录下 config/app.yaml 的 appId
    app_ids = [_read_app_id(os.path.join(app_dir, v)) for v in versions]
    # 各版本目录占用字节数（供「清除缓存」弹窗展示"可释放多少"）
    sizes = [_dir_size(os.path.join(app_dir, v)) for v in versions]
    logging.info("[版本列表] service=%s versions=%s app_ids=%s sizes=%s current=%s",
                 service_name, versions, app_ids, sizes, current_version)

    return {
        "code": 200,
        "message": "获取成功",
        "data": {
            "service_name": service_name,
            "versions": versions,
            "app_ids": app_ids,
            "sizes": sizes,
            "current_version": current_version,
            "count": len(versions)
        }
    }


def _dir_size(path: str) -> int:
    """统计目录占用字节数（不跟随软链接，读不到的条目跳过）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                continue
    return total


@router.post("/clear_cache", response_model=dict)
async def clear_service_cache(req: ClearCacheRequest):
    """
    清除节点「缓存区」里该应用已下载的版本目录（用于释放磁盘）。

    ★ 安全约束（很重要，别去掉）：
      · **保留运行区当前安装/运行的版本** —— 运行区 current/app/bin/config 都是指向
        缓存区版本目录的软链接，删了它服务直接起不来（重新下载也救不回"已安装"状态）。
      · 跳过隐藏目录（下载中的 `.staging` 等中间态），避免打断正在进行的下载。
      · 只指定单个 version 且它正是已安装版本 → 400 拒绝（明确告诉调用方不能删）；
        多选 versions 里含已安装版本 → 只跳过它并在 skipped 里说明，不影响其它版本删除。

    请求：
        {service_name, versions: ["3.9.2", ...]}  前端「清除缓存」弹窗多选
        {service_name, version: "3.9.2"}          单版本（兼容旧调用）
        {service_name}                            不传版本 = 清该应用全部已下载版本
    返回：data.{deleted[], skipped[], missing[], kept_version, freed_bytes, freed_mb, app_dir}
    """
    from core.utils import load_config

    service_name = (req.service_name or "").strip()
    if not service_name:
        raise HTTPException(status_code=400, detail="service_name 不能为空")

    config = load_config()
    download_dir = config.get("server", {}).get("download", "")
    if not download_dir:
        raise HTTPException(status_code=500, detail="下载目录未配置")

    sub_dir = resolve_sub_dir(service_name)
    app_dir = os.path.join(download_dir, sub_dir, service_name) if sub_dir \
        else os.path.join(download_dir, service_name)

    # 运行区里"当前安装/运行的版本"：必须保留（与 version/list 的取值方式一致）
    run_state = read_state(service_name, sub_dir) or {}
    kept_version = str(run_state.get("version", "") or "").strip()

    only_version = (req.version or "").strip()
    want_versions = [str(v).strip() for v in (req.versions or []) if str(v).strip()]
    # 单版本模式 = 只传了 version（没传 versions）
    single_mode = bool(only_version) and not want_versions
    if single_mode and kept_version and only_version == kept_version:
        raise HTTPException(
            status_code=400,
            detail=f"版本 {only_version} 是当前安装/运行的版本，不能清除（要删请先卸载）"
        )

    if not os.path.isdir(app_dir):
        return {
            "code": 200,
            "message": "该应用在节点缓存区没有目录，无需清理",
            "data": {
                "service_name": service_name,
                "app_dir": app_dir,
                "deleted": [],
                "skipped": [],
                "missing": [],
                "kept_version": kept_version,
                "freed_bytes": 0,
                "freed_mb": 0,
            },
        }

    deleted = []
    skipped = []
    freed = 0
    existing_names = set(os.listdir(app_dir))
    # 请求了、但缓存区里已经找不到的版本（被并发删过 / 名字写错）
    requested = want_versions if want_versions else ([only_version] if single_mode else [])
    missing = [v for v in requested if v not in existing_names]

    for name in sorted(existing_names):
        full = os.path.join(app_dir, name)
        if name.startswith("."):
            # .staging 等下载中间目录：跳过，避免打断正在进行的下载
            skipped.append({"version": name, "reason": "隐藏目录（下载中间态）"})
            continue
        if not os.path.isdir(full) or os.path.islink(full):
            skipped.append({"version": name, "reason": "非版本目录"})
            continue
        # 指定了要删哪些版本时，其余目录原样保留（不写进 skipped，避免干扰前端提示）
        if requested and name not in requested:
            continue
        if kept_version and name == kept_version:
            skipped.append({"version": name, "reason": "当前安装/运行的版本，已保留"})
            continue

        size = _dir_size(full)
        try:
            shutil.rmtree(full)
            deleted.append({"version": name, "size": size})
            freed += size
            logging.info("[清除缓存] 已删除版本目录: %s (%d bytes)", full, size)
        except Exception as e:
            skipped.append({"version": name, "reason": f"删除失败: {e}"})
            logging.warning("[清除缓存] 删除版本目录失败: %s -> %s", full, e)

    freed_mb = round(freed / 1048576.0, 2)
    logging.info("[清除缓存] service=%s app_dir=%s deleted=%s kept=%s freed=%d bytes",
                 service_name, app_dir, [d["version"] for d in deleted], kept_version, freed)
    return {
        "code": 200,
        "message": f"已清除 {len(deleted)} 个版本，释放 {freed_mb} MB",
        "data": {
            "service_name": service_name,
            "app_dir": app_dir,
            "deleted": deleted,
            "skipped": skipped,
            "missing": missing,
            "kept_version": kept_version,
            "freed_bytes": freed,
            "freed_mb": freed_mb,
        },
    }
