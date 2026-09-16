"""
Agent 相关的 API 路由
"""
import json
import time
from typing import List, Dict, Any

import psutil
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from utils.redis_store import redisUtils

router = APIRouter()

class AgentRegisterRequest(BaseModel):
    """Agent 注册请求模型"""
    username: str
    ip: str
    version: str
    create_time: int

class AgentStatusResponse(BaseModel):
    """Agent 状态响应模型"""
    agent_id: str
    status: int  # 0=离线, 1=在线
    last_heartbeat: int
    version: str
    system_info: dict

@router.post("/register")
async def register_agent(request: AgentRegisterRequest):
    """
    注册新的 Agent
    """
    import uuid
    import time
    
    # 1. 生成唯一的 agent_id
    agent_id = f"agent_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    
    # 2. 将 Agent 信息存储到 Redis
    agent_info = {
        "agent_id": agent_id,
        "username": request.username,
        "ip": request.ip,
        "version": request.version,
        "create_time": request.create_time,
        "update_time": int(time.time()),
        "status": 1,  # 在线状态
        "host_info": {
            "hostname": psutil.os.uname().nodename,
            "system": psutil.os.uname().system,
            "release": psutil.os.uname().release,
            "machine": psutil.os.uname().machine,
            "cpu_count": psutil.cpu_count(),
            "memory_total": psutil.virtual_memory().total
        }
    }
    
    # 存储到 Redis，设置过期时间为 24 小时
    redisUtils.set(f"agent:info:{agent_id}", agent_info, ttl=86400)
    
    return {
        "code": 200,
        "message": "注册成功",
        "data": agent_id
    }

@router.get("/list")
async def list_agents():
    """
    获取所有在线的 Agent 列表
    """
    import time
    
    # 从 Redis 获取所有 agent:info:* 的键
    redis_client = redisUtils.redis
    agent_keys = redis_client.keys("agent:info:*")
    
    agents = []
    current_time = time.time()
    
    for key in agent_keys:
        agent_info = redisUtils.get(key)
        if agent_info:
            # 检查是否在线（最后更新时间在 60 秒内）
            last_update = agent_info.get("update_time", 0)
            is_online = (current_time - last_update) < 60
            
            agents.append({
                "agent_id": agent_info.get("agent_id"),
                "username": agent_info.get("username"),
                "ip": agent_info.get("ip"),
                "version": agent_info.get("version"),
                "status": 1 if is_online else 0,
                "last_heartbeat": last_update,
                "create_time": agent_info.get("create_time"),
                "host_info": agent_info.get("host_info", {})
            })
    
    return {
        "code": 200,
        "message": "获取成功",
        "data": {
            "total": len(agents),
            "agents": agents
        }
    }

@router.get("/{agent_id}/status")
async def get_agent_status(agent_id: str):
    """
    获取指定 Agent 的详细状态
    """
    # 从 Redis 获取 Agent 信息
    agent_info = redisUtils.get(f"agent:info:{agent_id}")
    
    if not agent_info:
        raise HTTPException(status_code=404, detail="Agent 不存在或已离线")
    
    return {
        "code": 200,
        "message": "获取成功",
        "data": {
            "agent_id": agent_id,
            "status": agent_info.get("status", 0),
            "last_heartbeat": agent_info.get("update_time", 0),
            "version": agent_info.get("version", "unknown"),
            "system_info": agent_info.get("host_info", {})
        }
    }

@router.post("/{agent_id}/restart")
async def restart_agent(agent_id: str):
    """
    重启指定 Agent
    """
    import time
    
    # 检查 Agent 是否存在
    agent_info = redisUtils.get(f"agent:info:{agent_id}")
    if not agent_info:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    
    # 创建重启指令到 Redis 队列
    restart_command = {
        "command": "restart",
        "agent_id": agent_id,
        "timestamp": int(time.time()),
        "issued_by": "api"
    }
    
    # 将重启指令放入 Redis 队列，Agent 会定期检查并执行
    redisUtils.set(f"agent:command:{agent_id}:restart", restart_command, ttl=300)
    
    return {
        "code": 200,
        "message": f"已向 Agent {agent_id} 发送重启指令",
        "data": {
            "agent_id": agent_id,
            "timestamp": restart_command["timestamp"],
            "expires_in": 300  # 5 分钟后过期
        }
    }

@router.delete("/{agent_id}")
async def delete_agent(agent_id: str):
    """
    注销 Agent
    """
    # 从 Redis 删除 Agent 信息
    redisUtils.delete(f"agent:info:{agent_id}")
    
    return {
        "code": 200,
        "message": f"Agent {agent_id} 已注销"
    }


class ProcessInfo(BaseModel):
    """进程信息模型"""
    pid: int
    name: str
    status: str
    cpu_percent: float
    memory_rss: int  # 内存使用量 (RSS)
    memory_vms: int  # 虚拟内存大小 (VMS)
    create_time: float
    username: str
    cmdline: List[str]
    exe: str
    num_threads: int
    io_read_bytes: int = 0
    io_write_bytes: int = 0
    connections: List[Dict[str, Any]] = []




@router.get("/{agent_id}/processes")
async def get_agent_processes(
        agent_id: str,
        page: int = 1,
        page_size: int = 20,
        keyword: str = None
):
    """
    获取进程信息列表（分页）
    支持通过 keyword 模糊查询进程名或 PID
    """
    start_time = time.perf_counter()

    if page < 1 or page_size < 1:
        raise HTTPException(
            status_code=400,
            detail="page 和 page_size 必须大于 0"
        )

    try:
        # 有 keyword 则模糊匹配进程名或 PID，没有则不过滤
        kw = keyword.lower() if keyword else None

        all_processes = []
        for pid in psutil.pids():
            if pid == 0:
                continue
            try:
                p = psutil.Process(pid)
                name = p.name()

                if kw and kw not in name.lower() and kw not in str(pid):
                    continue

                all_processes.append({
                    "pid": pid,
                    "name": name
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        total_processes = len(all_processes)

        # 分页
        start = (page - 1) * page_size
        end = start + page_size
        paged_processes = all_processes[start:end]

        cost = time.perf_counter() - start_time
        print(f"get_agent_processes 耗时: {cost:.4f}s")

        return {
            "code": 200,
            "message": "获取进程信息成功",
            "data": {
                "agent_id": agent_id,
                "timestamp": int(time.time()),
                "keyword": keyword,
                "total_processes": total_processes,
                "page": page,
                "page_size": page_size,
                "total_pages": (total_processes + page_size - 1) // page_size,
                "processes": paged_processes,
                "cost_seconds": round(cost, 4)
            }
        }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"获取进程信息失败: {str(e)}"
        )


@router.get("/{agent_id}/processes/{pid}")
async def get_process_detail(
    agent_id: str,
    pid: int
):
    """
    通过 PID 查询进程的详细信息
    包含 CPU、内存、磁盘、线程、连接等完整信息
    """
    try:
        # 尝试获取指定 PID 的进程
        proc = psutil.Process(pid)
        
        # 获取基础信息
        with proc.oneshot():
            # CPU 信息
            cpu_percent = proc.cpu_percent(interval=0.1)  # 获取 CPU 使用率
            
            # 内存信息
            memory_info = proc.memory_info()
            memory_percent = proc.memory_percent()
            
            # 磁盘 IO 信息
            io_counters = proc.io_counters() if hasattr(proc, 'io_counters') else None
            
            # 线程信息
            threads = []
            try:
                for thread in proc.threads():
                    threads.append({
                        "id": thread.id,
                        "user_time": thread.user_time,
                        "system_time": thread.system_time
                    })
            except:
                threads = []
            
            # 连接信息
            connections = []
            try:
                for conn in proc.net_connections(kind='all'):
                    connections.append({
                        "fd": conn.fd,
                        "family": conn.family.name if hasattr(conn.family, 'name') else str(conn.family),
                        "type": conn.type.name if hasattr(conn.type, 'name') else str(conn.type),
                        "local_address": f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else None,
                        "remote_address": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else None,
                        "status": conn.status
                    })
            except:
                connections = []
            
            # 进程环境
            environ = {}
            try:
                environ = dict(proc.environ())
                # 保护敏感信息，只显示部分环境变量
                sensitive_keys = ['PASSWORD', 'SECRET', 'KEY', 'TOKEN', 'AUTH']
                for key in list(environ.keys()):
                    if any(sensitive in key.upper() for sensitive in sensitive_keys):
                        environ[key] = "***HIDDEN***"
            except:
                environ = {}
            
            # 命令行参数
            cmdline = proc.cmdline()
            
            # 创建详细信息字典
            process_detail = {
                "pid": proc.pid,
                "name": proc.name(),
                "status": proc.status(),
                "create_time": proc.create_time(),
                "username": proc.username(),
                "exe": proc.exe(),
                "cwd": proc.cwd(),
                "ppid": proc.ppid(),
                
                # CPU 信息
                "cpu_info": {
                    "cpu_percent": cpu_percent,
                    "cpu_affinity": proc.cpu_affinity(),
                    "cpu_num": proc.cpu_num() if hasattr(proc, 'cpu_num') else None,
                    "nice": proc.nice()
                },
                
                # 内存信息
                "memory_info": {
                    "rss_bytes": memory_info.rss,
                    "vms_bytes": memory_info.vms,
                    "percent": memory_percent,
                    "data": memory_info.data if hasattr(memory_info, 'data') else None,
                    "text": memory_info.text if hasattr(memory_info, 'text') else None,
                    "lib": memory_info.lib if hasattr(memory_info, 'lib') else None
                },
                
                # 磁盘 IO 信息
                "io_info": {
                    "read_count": io_counters.read_count if io_counters else 0,
                    "write_count": io_counters.write_count if io_counters else 0,
                    "read_bytes": io_counters.read_bytes if io_counters else 0,
                    "write_bytes": io_counters.write_bytes if io_counters else 0,
                    "read_chars": getattr(io_counters, 'read_chars', 0) if io_counters else 0,
                    "write_chars": getattr(io_counters, 'write_chars', 0) if io_counters else 0
                },
                
                # 线程信息
                "thread_info": {
                    "num_threads": proc.num_threads(),
                    "threads": threads
                },
                
                # 连接信息
                "connection_info": {
                    "num_fds": proc.num_fds() if hasattr(proc, 'num_fds') else None,
                    "connections": connections
                },
                
                # 其他信息
                "cmdline": cmdline,
                "environ_count": len(environ),
                "environ_sample": dict(list(environ.items())[:5]),  # 只显示前5个环境变量
                
                # 进程树信息
                "children": [
                    {"pid": child.pid, "name": child.name()}
                    for child in proc.children(recursive=False)
                ],
                "parent": {
                    "pid": proc.ppid(),
                    "name": psutil.Process(proc.ppid()).name() if proc.ppid() else None
                } if proc.ppid() else None
            }
        
        return {
            "code": 200,
            "message": "获取进程详细信息成功",
            "data": {
                "agent_id": agent_id,
                "pid": pid,
                "timestamp": int(time.time()),
                "process": process_detail
            }
        }
        
    except psutil.NoSuchProcess:
        raise HTTPException(
            status_code=404,
            detail=f"进程 PID={pid} 不存在"
        )
    except psutil.AccessDenied:
        raise HTTPException(
            status_code=403,
            detail=f"无权限访问进程 PID={pid}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"获取进程详细信息失败: {str(e)}"
        )


