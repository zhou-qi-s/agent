"""
版本管理相关的 API 路由
"""
import os
from typing import List

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()


class VersionListResponse(BaseModel):
    """版本列表响应模型"""
    service_name: str
    versions: List[str]
    current_version: str = None
    count: int


@router.get("/list", response_model=dict)
async def get_service_versions(
    service_name: str = Query(..., description="应用名称")
):
    """
    根据应用名称，返回该应用下所有版本文件夹的名称
    """
    from core.utils import load_config

    config = load_config()
    download_dir = config.get("server", {}).get("download", "")

    if not download_dir:
        raise HTTPException(status_code=500, detail="下载目录未配置")

    # 构建服务目录路径
    app_dir = os.path.join(download_dir, service_name)

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
        ]
        # 按版本号排序（降序，新版本在前）
        versions.sort(reverse=True)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"读取版本列表失败: {str(e)}"
        )

    # 读取当前正在运行的版本，并从列表中排除
    current_version = None
    version_file = os.path.join(download_dir, service_name, "version")
    if os.path.exists(version_file):
        try:
            with open(version_file, "r", encoding="utf-8") as f:
                current_version = f.read().strip()
        except Exception:
            pass

    # 排除当前正在运行的版本
    if current_version and current_version in versions:
        versions.remove(current_version)

    return {
        "code": 200,
        "message": "获取成功",
        "data": {
            "service_name": service_name,
            "versions": versions,
            "current_version": current_version,
            "count": len(versions)
        }
    }
