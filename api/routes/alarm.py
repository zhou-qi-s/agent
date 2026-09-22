"""
告警相关的 API 路由

【改造说明】
服务的 resources.txt 已改由 process_info.py 写入「运行区」：
    {apps}/{service_name}/runtime/resources.txt
（原为 {download}/{service_name}/app/{version}/runtime/resources.txt，
  依赖已废弃的 version 文件）
"""
import json
import logging
import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from core.alarm.alarm_common import get_local_ip
from utils.app_path import resolve_sub_dir
from utils.config_loader import load_config
from utils.redis_client import get_redis

router = APIRouter()

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")
_APPS_BASE = _CONFIG.get("server", {}).get("apps", "")

# 合法的阈值字段: cpu(%), memory(kb), io(bk)
_THRESHOLD_KEYS = {"cpu", "memory", "io"}


class AlarmRequest(BaseModel):
    """告警请求模型"""
    params: Dict[str, Any]


class K8sAlarmRequest(BaseModel):
    """Kubernetes 容器告警阈值请求"""
    labels: str
    name_space: str
    cpu: float
    memory: float
    io: float


@router.post("/trigger")
async def trigger_alarm(request: AlarmRequest):
    """
    告警触发接口。
    接收服务名称及 cpu / memory / io 阈值，保存到 Redis。

    Redis Key:   alarm:{ip}:{service_name}
    Redis Value: {"service_name":"my-service","cpu":"80","memory":"512","io":"100","alarmType":"2"}
    """
    params = request.params

    if not params:
        raise HTTPException(status_code=400, detail="params 不能为空")

    service_name = params.get("service_name") or params.get("serviceName") or params.get("service")

    if not service_name:
        raise HTTPException(status_code=400, detail="params 中缺少服务名称 (service_name / serviceName / service)")

    service_name = str(service_name)

    # ---- 校验必填的阈值字段 ----
    missing = [k for k in _THRESHOLD_KEYS if k not in params]
    if missing:
        raise HTTPException(status_code=400, detail=f"缺少必填参数: {', '.join(missing)}")

    # ---- 保存到 Redis ----
    try:
        r = get_redis()
        local_ip = get_local_ip()
        key = f"alarm:{local_ip}:{service_name}"

        # 读取已有配置（兼容双重 JSON 编码）
        existing: Dict[str, Any] = {}
        old_data = r.get(key)
        if old_data:
            try:
                existing = json.loads(old_data)
                # 兼容双重编码: 外层是 JSON 字符串，再解一层
                if isinstance(existing, str):
                    existing = json.loads(existing)
                if not isinstance(existing, dict):
                    logging.warning("[alarm] Redis 已有值不是 JSON 对象，重置: %s -> %s", key, old_data)
                    existing = {}
            except (json.JSONDecodeError, Exception):
                existing = {}

        # 更新阈值
        for field in _THRESHOLD_KEYS:
            existing[field] = str(params[field])
        existing["service_name"] = service_name
        existing["alarmType"] = "2"  # 告警类型，固定为 2

        r.set(key, json.dumps(existing, ensure_ascii=False))
        logging.info("[alarm] 阈值已写入 Redis: %s", key)
    except Exception as e:
        logging.error("[alarm] 写入 Redis 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"保存告警阈值失败: {str(e)}")

    return {
        "code": 200,
        "message": "保存成功",
        "data": {
            "service_name": service_name,
            "redis_key": key,
            "params": params,
        }
    }


@router.post("/k8s/trigger")
async def trigger_k8s_alarm(request: K8sAlarmRequest):
    """
    Kubernetes 容器告警阈值配置接口。

    为指定 namespace 下的 labels 设置 cpu / memory / io 告警阈值，
    持久化到 {download}/kubernetes/{name_space}/{labels}.txt (JSON格式)。
    """
    if not _DOWNLOAD_BASE:
        raise HTTPException(status_code=500, detail="download 目录未配置")

    labels = request.labels.strip()
    name_space = request.name_space.strip()

    if not labels:
        raise HTTPException(status_code=400, detail="labels 不能为空")
    if not name_space:
        raise HTTPException(status_code=400, detail="name_space 不能为空")

    # 构建目标目录
    target_dir = os.path.join(_DOWNLOAD_BASE, "kubernetes", name_space)
    os.makedirs(target_dir, exist_ok=True)

    # labels 中可能含 /（如 app.kubernetes.io/instance=nacos），
    # 文件名需替换 / 为 _ 避免被解析为目录层级
    safe_labels = labels.replace("/", "_")

    # 构建 JSON 数据
    alarm_data: Dict[str, Any] = {
        "labels": labels,
        "name_space": name_space,
        "cpu": request.cpu,
        "memory": request.memory,
        "io": request.io,
        "alarmType": "2",  # 告警类型，固定为 2
    }

    # 合并已有配置（如果存在）
    alarm_file = os.path.join(target_dir, f"{safe_labels}.txt")
    existing: Dict[str, Any] = {}
    if os.path.isfile(alarm_file):
        try:
            with open(alarm_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    existing.update(alarm_data)

    with open(alarm_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    logging.info("[alarm-k8s] 阈值已写入: %s", alarm_file)

    return {
        "code": 200,
        "message": "保存成功",
        "data": {
            "labels": labels,
            "name_space": name_space,
            "alarm_file": alarm_file,
            "thresholds": {"cpu": request.cpu, "memory": request.memory, "io": request.io},
        },
    }


@router.get("/k8s/trigger")
async def get_k8s_alarm(
    labels: str = Query(..., description="K8s 标签选择器"),
    name_space: str = Query(..., description="命名空间"),
):
    """
    查询指定 namespace + labels 的 K8s 容器告警阈值配置。
    用于编辑时回填已有数据。
    """
    if not _DOWNLOAD_BASE:
        raise HTTPException(status_code=500, detail="download 目录未配置")

    labels = labels.strip()
    name_space = name_space.strip()

    if not labels or not name_space:
        raise HTTPException(status_code=400, detail="labels 和 name_space 不能为空")

    safe_labels = labels.replace("/", "_")
    alarm_file = os.path.join(_DOWNLOAD_BASE, "kubernetes", name_space, f"{safe_labels}.txt")

    if not os.path.isfile(alarm_file):
        raise HTTPException(status_code=404, detail=f"告警配置不存在: {alarm_file}")

    try:
        with open(alarm_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        raise HTTPException(status_code=500, detail="读取告警配置失败")

    return {
        "code": 200,
        "message": "查询成功",
        "data": data,
    }


@router.delete("/k8s/trigger")
async def delete_k8s_alarm(
    labels: str = Query(..., description="K8s 标签选择器"),
    name_space: str = Query(..., description="命名空间"),
):
    """
    删除指定 namespace + labels 的 K8s 容器告警阈值配置文件。

    文件路径: {download}/kubernetes/{name_space}/{safe_labels}.txt
    """
    if not _DOWNLOAD_BASE:
        raise HTTPException(status_code=500, detail="download 目录未配置")

    labels = labels.strip()
    name_space = name_space.strip()

    if not labels or not name_space:
        raise HTTPException(status_code=400, detail="labels 和 name_space 不能为空")

    safe_labels = labels.replace("/", "_")
    alarm_file = os.path.join(_DOWNLOAD_BASE, "kubernetes", name_space, f"{safe_labels}.txt")

    if not os.path.isfile(alarm_file):
        raise HTTPException(status_code=404, detail=f"告警配置不存在: {alarm_file}")

    try:
        os.remove(alarm_file)
        logging.info("[alarm-k8s] 配置已删除: %s", alarm_file)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"删除文件失败: {str(e)}")

    return {
        "code": 200,
        "message": "删除成功",
        "data": {
            "labels": labels,
            "name_space": name_space,
            "alarm_file": alarm_file,
        },
    }


@router.get("/trigger")
async def get_alarm(service_name: str = Query(..., description="服务名称")):
    """
    查询指定服务的告警阈值配置（从 Redis）。

    Redis Key: alarm:{ip}:{service_name}
    """
    service_name = service_name.strip()
    if not service_name:
        raise HTTPException(status_code=400, detail="service_name 不能为空")

    try:
        r = get_redis()
        local_ip = get_local_ip()
        key = f"alarm:{local_ip}:{service_name}"
        data = r.get(key)
        if data is None:
            raise HTTPException(status_code=404, detail=f"告警配置不存在: {key}")

        alarm_data = json.loads(data)
    except HTTPException:
        raise
    except Exception as e:
        logging.error("[alarm] 读取 Redis 告警配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"读取告警配置失败: {str(e)}")

    return {
        "code": 200,
        "message": "查询成功",
        "data": alarm_data,
    }


@router.delete("/trigger")
async def delete_alarm(service_name: str = Query(..., description="服务名称")):
    """
    删除指定服务的告警阈值配置（从 Redis）。

    Redis Key: alarm:{ip}:{service_name}
    """
    service_name = service_name.strip()
    if not service_name:
        raise HTTPException(status_code=400, detail="service_name 不能为空")

    try:
        r = get_redis()
        local_ip = get_local_ip()
        key = f"alarm:{local_ip}:{service_name}"

        if r.get(key) is None:
            raise HTTPException(status_code=404, detail=f"告警配置不存在: {key}")

        r.delete(key)
        logging.info("[alarm] Redis 告警配置已删除: %s", key)
    except HTTPException:
        raise
    except Exception as e:
        logging.error("[alarm] 删除 Redis 告警配置失败: %s", e)
        raise HTTPException(status_code=500, detail=f"删除告警配置失败: {str(e)}")

    return {
        "code": 200,
        "message": "删除成功",
        "data": {
            "service_name": service_name,
            "redis_key": key,
        },
    }


@router.get("/resources")
async def get_resources(service_name: str = Query(..., description="服务名称")):
    """
    根据服务名返回 runtime/resources.txt 的内容。

    改造后查找路径（运行区）:
        {apps}/{service_name}/runtime/resources.txt

    原路径 {download}/{service_name}/app/{version}/runtime/resources.txt 已废弃：
    版本号不再来自 version 文件，resources.txt 也改由 process_info.py 写入运行区。
    """
    if not _APPS_BASE:
        raise HTTPException(status_code=500, detail="server.apps 运行区目录未配置")

    # 显控台/插件服务在运行区多一层（displayConsole/plugin），先探测所在层
    sub_dir = resolve_sub_dir(service_name, roots=[_APPS_BASE])
    service_dir = os.path.join(_APPS_BASE, sub_dir, service_name) if sub_dir \
        else os.path.join(_APPS_BASE, service_name)
    if not os.path.isdir(service_dir):
        raise HTTPException(status_code=404, detail=f"运行区服务目录不存在: {service_dir}")

    resources_file = os.path.join(service_dir, "runtime", "resources.txt")
    if not os.path.isfile(resources_file):
        raise HTTPException(status_code=404, detail=f"resources.txt 不存在: {resources_file}")

    try:
        with open(resources_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="resources.txt 解析失败")
    except Exception:
        raise HTTPException(status_code=500, detail="读取 resources.txt 失败")

    return {
        "code": 200,
        "message": "查询成功",
        "data": data,
    }
