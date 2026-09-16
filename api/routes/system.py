"""
系统管理相关的 API 路由
"""
from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()

class SystemInfoResponse(BaseModel):
    """系统信息响应模型"""
    version: str
    uptime: str
    timestamp: str
    services: list

@router.get("/info")
async def get_system_info():
    """
    获取系统整体信息
    """
    import psutil
    import time
    
    # 计算系统运行时间
    boot_time = psutil.boot_time()
    uptime_seconds = time.time() - boot_time
    uptime_str = str(datetime.fromtimestamp(uptime_seconds).strftime("%H:%M:%S"))
    
    # 获取系统负载信息
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    # 模拟服务状态
    services = [
        {"name": "Agent API", "status": "running", "version": "1.0.0"},
        {"name": "Redis", "status": "running", "version": "7.0.0"},
        {"name": "Database", "status": "running", "version": "1.0.0"},
    ]
    
    return {
        "code": 200,
        "message": "获取成功",
        "data": {
            "version": "1.0.0",
            "uptime": uptime_str,
            "timestamp": datetime.now().isoformat(),
            "system_load": {
                "cpu_percent": cpu_percent,
                "memory_percent": memory.percent,
                "disk_percent": disk.percent,
                "process_count": len(list(psutil.process_iter()))
            },
            "services": services
        }
    }

@router.get("/stats")
async def get_system_stats():
    """
    获取系统统计数据
    """
    from utils.redis_store import redisUtils
    import time
    
    # 统计在线 Agent 数量
    redis_client = redisUtils.redis
    agent_keys = redis_client.keys("agent:info:*")
    
    online_count = 0
    offline_count = 0
    current_time = time.time()
    
    for key in agent_keys:
        agent_info = redisUtils.get(key)
        if agent_info:
            last_update = agent_info.get("update_time", 0)
            if (current_time - last_update) < 60:
                online_count += 1
            else:
                offline_count += 1
    
    agent_count = len(agent_keys)
    
    # 统计任务数量（从任务队列获取）
    task_keys = redis_client.keys("task:*:*")
    task_counts = {
        "pending": 0,
        "processing": 0,
        "completed": 0,
        "failed": 0,
        "total": len(task_keys)
    }
    
    # 简单分类（实际项目中可能需要更精确的分类）
    for key in task_keys:
        task_info = redisUtils.get(key)
        if task_info:
            status = task_info.get("status", "pending")
            if status == "processing":
                task_counts["processing"] += 1
            elif status == "completed":
                task_counts["completed"] += 1
            elif status == "failed":
                task_counts["failed"] += 1
            else:
                task_counts["pending"] += 1
    
    return {
        "code": 200,
        "message": "获取成功",
        "data": {
            "agents": {
                "total": agent_count,
                "online": online_count,
                "offline": offline_count
            },
            "tasks": task_counts,
            "timestamp": datetime.now().isoformat()
        }
    }

@router.get("/logs")
async def get_system_logs(limit: int = 100):
    """
    获取系统日志
    """
    import os
    import json
    from datetime import datetime
    
    logs = []
    
    # 从 Redis 获取日志（如果存在）
    try:
        from utils.redis_store import redisUtils
        redis_client = redisUtils.redis
        
        # 获取最新的日志条目
        log_keys = redis_client.keys("log:*")
        log_keys.sort(reverse=True)  # 从新到旧排序
        
        for key in log_keys[:limit]:
            log_entry = redisUtils.get(key)
            if log_entry:
                logs.append(log_entry)
    except Exception as e:
        # 如果 Redis 不可用，从本地日志文件读取
        log_file = "agent.log"
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    # 取最后 limit 行
                    for line in lines[-limit:]:
                        logs.append({
                            "timestamp": datetime.now().isoformat(),
                            "level": "INFO",
                            "message": line.strip(),
                            "source": "file"
                        })
            except Exception as file_error:
                logs.append({
                    "timestamp": datetime.now().isoformat(),
                    "level": "ERROR",
                    "message": f"读取日志文件失败: {str(file_error)}",
                    "source": "system"
                })
        else:
            # 返回模拟日志
            logs = [
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "INFO",
                    "message": "系统启动完成",
                    "source": "system"
                },
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "INFO",
                    "message": "Redis 连接成功",
                    "source": "system"
                },
                {
                    "timestamp": datetime.now().isoformat(),
                    "level": "INFO",
                    "message": "API 服务已启动",
                    "source": "api"
                }
            ]
    
    # 如果日志为空，添加默认信息
    if not logs:
        logs.append({
            "timestamp": datetime.now().isoformat(),
            "level": "INFO",
            "message": "暂无日志数据",
            "source": "system"
        })
    
    return {
        "code": 200,
        "message": "获取成功",
        "data": {
            "total": len(logs),
            "logs": logs[:limit]
        }
    }

@router.get("/health")
async def system_health_check():
    """
    系统健康检查
    """
    import psutil
    
    checks = []
    
    # 检查 Redis 连接
    try:
        from utils.redis_store import redisUtils
        redisUtils.ping()
        checks.append({"service": "Redis", "status": "healthy", "message": "连接正常"})
    except Exception as e:
        checks.append({"service": "Redis", "status": "unhealthy", "message": str(e)})
    
    # 检查系统资源
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        
        checks.append({
            "service": "CPU",
            "status": "healthy" if cpu_percent < 90 else "warning",
            "message": f"使用率: {cpu_percent}%",
            "value": cpu_percent
        })
        
        checks.append({
            "service": "Memory",
            "status": "healthy" if memory.percent < 90 else "warning",
            "message": f"使用率: {memory.percent}%",
            "value": memory.percent
        })
    except Exception as e:
        checks.append({"service": "System Resources", "status": "unhealthy", "message": str(e)})
    
    # 总体状态
    unhealthy_count = sum(1 for check in checks if check["status"] != "healthy")
    overall_status = "healthy" if unhealthy_count == 0 else "warning" if unhealthy_count < 2 else "unhealthy"
    
    return {
        "code": 200,
        "message": "健康检查完成",
        "data": {
            "status": overall_status,
            "timestamp": datetime.now().isoformat(),
            "checks": checks
        }
    }