"""
Helm release 管理相关的 API 路由
查询 Helm release 详情
"""
import logging
import subprocess

from fastapi import APIRouter, Query

router = APIRouter()

_LOG = "helm_api"


@router.get("/get")
async def helm_get_release(
    name: str = Query(..., description="Release 名称，如 mysql"),
    namespace: str = Query("default", description="命名空间"),
):
    """
    查询 Helm release 的全部信息（helm get all）

    - **name**: release 名称，如 mysql
    - **namespace**: 命名空间，默认 default
    """
    cmd = ["helm", "get", "all", name, "-n", namespace]

    logging.info("[%s] 执行: %s", _LOG, " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        logging.error("[%s] 命令超时: %s", _LOG, " ".join(cmd))
        return {
            "code": 500,
            "message": "helm get all 命令执行超时",
            "data": None,
        }
    except Exception as e:
        logging.error("[%s] 命令异常: %s", _LOG, e)
        return {
            "code": 500,
            "message": f"命令执行异常: {e}",
            "data": None,
        }

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0:
        logging.error("[%s] helm 命令失败: %s", _LOG, stderr)
        return {
            "code": 500,
            "message": stderr or "helm get all 执行失败",
            "data": None,
        }

    logging.info("[%s] 查询成功: %s/%s", _LOG, namespace, name)

    return {
        "code": 200,
        "message": "查询成功",
        "data": {
            "name": name,
            "namespace": namespace,
            "info": stdout,
        },
    }
