"""
任务相关的 API 路由
"""
from datetime import datetime
import os
import uuid
from typing import Any, Dict, List, Optional, cast


import psutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from utils.logger import logger
from utils.redis_store import redisUtils


router = APIRouter()


class TaskCreateRequest(BaseModel):
    """任务创建请求模型"""
    type: str  # download, install, start, stop, execute_command, uninstall
    parameters: Dict[str, Any]
    priority: Optional[int] = 1
    retry: Optional[int] = 3
    timeout: Optional[int] = 300


class TaskResponse(BaseModel):
    """任务响应模型"""
    task_id: str
    type: str
    status: str  # pending, processing, completed, failed
    parameters: Dict[str, Any]
    priority: int
    retry: int
    timeout: int
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]
    result: Optional[Dict[str, Any]]



@router.post("/create")
async def create_task(request: TaskCreateRequest):
    """
    创建新任务
    """
    task_id = f"task_utils-{uuid.uuid4()}"
    created_at = datetime.now().isoformat()

    task_data = {
        "task_id": task_id,
        "type": request.type,
        "parameters": request.parameters,
        "priority": request.priority,
        "retry": request.retry,
        "timeout": request.timeout,
        "created_at": created_at,
        "status": "pending",
        "assigned_agent": None,
        "result": None
    }

    queue_name = "agent:task_utils:queue"
    redisUtils.l_push(queue_name, task_data)


    return {
        "code": 200,
        "message": "任务创建成功",
        "data": {
            "task_id": task_id,
            "queue_name": queue_name
        }
    }


@router.get("/list")
async def list_tasks(status: Optional[str] = None, limit: int = 100):
    """
    获取任务列表，可按状态过滤
    """
    tasks = []

    if status:
        tasks = [task for task in tasks if task.get("status") == status]

    return {
        "code": 200,
        "message": "获取成功",
        "data": {
            "total": len(tasks),
            "tasks": tasks[:limit]
        }
    }


@router.get("/process/{pid}")
async def get_process_info(pid: int):
    """
    根据进程 ID 获取进程信息
    """
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail=f"进程 PID={pid} 不存在")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail=f"无权限访问进程 PID={pid}")

    def safe_call(func, default=None):
        try:
            return func()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return default

    def format_address(addr):
        if not addr:
            return None
        if hasattr(addr, "ip") and hasattr(addr, "port"):
            return f"{addr.ip}:{addr.port}"
        if isinstance(addr, (tuple, list)) and len(addr) >= 2:
            return f"{addr[0]}:{addr[1]}"
        return str(addr)

    try:
        with proc.oneshot():
            parent_pid = safe_call(proc.ppid, 0) or 0
            parent_name = safe_call(lambda: psutil.Process(parent_pid).name(), None) if parent_pid else None
            memory_info = safe_call(proc.memory_info)
            connection_getter = getattr(proc, "net_connections", None)
            if callable(connection_getter):
                net_connections = cast(List[Any], safe_call(lambda: connection_getter(kind="inet"), []) or [])
            else:
                net_connections = cast(List[Any], safe_call(lambda: proc.connections(kind="inet"), []) or [])
            child_processes = cast(List[Any], safe_call(lambda: proc.children(recursive=False), []) or [])



            connections = [
                {
                    "family": conn.family.name if hasattr(conn.family, "name") else str(conn.family),
                    "type": conn.type.name if hasattr(conn.type, "name") else str(conn.type),
                    "local_address": format_address(conn.laddr),
                    "remote_address": format_address(conn.raddr),
                    "status": conn.status
                }
                for conn in net_connections
            ]

            children = [
                {
                    "pid": child.pid,
                    "name": safe_call(child.name, "")
                }
                for child in child_processes
            ]

            process_info = {
                "pid": proc.pid,
                "name": safe_call(proc.name, ""),
                "status": safe_call(proc.status, "unknown"),
                "create_time": safe_call(proc.create_time),
                "username": safe_call(proc.username, ""),
                "exe": safe_call(proc.exe, ""),
                "cwd": safe_call(proc.cwd, ""),
                "ppid": parent_pid,
                "cmdline": safe_call(proc.cmdline, []) or [],
                "cpu_percent": safe_call(lambda: proc.cpu_percent(interval=0.1), 0.0),
                "memory_info": {
                    "rss_bytes": getattr(memory_info, "rss", 0) if memory_info else 0,
                    "vms_bytes": getattr(memory_info, "vms", 0) if memory_info else 0,
                    "percent": safe_call(proc.memory_percent, 0.0)
                },
                "num_threads": safe_call(proc.num_threads, 0),
                "connections": connections,
                "children": children,
                "parent": {
                    "pid": parent_pid,
                    "name": parent_name
                } if parent_pid else None
            }

        return {
            "code": 200,
            "message": "获取进程信息成功",
            "data": {
                "pid": pid,
                "timestamp": datetime.now().isoformat(),
                "process": process_info
            }
        }
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail=f"进程 PID={pid} 不存在")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail=f"无权限访问进程 PID={pid}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取进程信息失败: {str(e)}")


@router.get("/{task_id}")
async def get_task_status(task_id: str):
    """
    获取任务详细状态
    """
    task_key = f"agent:task_utils:{task_id}"
    task_info = redisUtils.get(task_key)

    if not task_info:
        raise HTTPException(status_code=404, detail="任务不存在")

    return {
        "code": 200,
        "message": "获取成功",
        "data": task_info
    }


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str):
    """
    取消指定任务
    """
    task_key = f"agent:task_utils:{task_id}"
    task_info = cast(Optional[Dict[str, Any]], redisUtils.get(task_key))

    if not task_info:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task_info.get("status") in ["completed", "failed"]:

        raise HTTPException(status_code=400, detail="任务已结束，无法取消")

    task_info["status"] = "cancelled"
    task_info["completed_at"] = datetime.now().isoformat()
    redisUtils.set(task_key, task_info)

    return {
        "code": 200,
        "message": f"任务 {task_id} 已取消"
    }


@router.post("/result")
async def upload_task_result():
    """
    接收任务执行结果（由 Agent 调用）
    """
    return {
        "code": 200,
        "message": "结果接收成功"
    }
