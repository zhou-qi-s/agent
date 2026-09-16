# FastAPI API 服务

## 概述

本项目添加了基于 FastAPI 的 RESTful API 服务，用于管理 Agent 系统。提供 Agent 注册、任务管理、系统监控等功能。

## 目录结构

```
api/
├── app.py              # FastAPI 应用主文件
├── routes/             # 路由模块
│   ├── __init__.py
│   ├── agent.py       # Agent 相关接口
│   ├── task.py        # 任务管理接口
│   └── system.py      # 系统管理接口
└── __init__.py
```

## 启动服务

### 方式一：使用启动脚本
```bash
python start_api.py
```

### 方式二：直接使用 Uvicorn
```bash
uvicorn api.app:app --host 0.0.0.0 --port 8000 --reload
```

## API 接口文档

启动服务后，访问以下地址：
- **Swagger UI**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

## 主要接口

### 1. Agent 管理

#### 注册 Agent
```
POST /api/agent/register
Content-Type: application/json

{
    "username": "user123",
    "ip": "192.168.1.100",
    "version": "1.0.0",
    "create_time": 1741584000
}
```

#### 获取 Agent 列表
```
GET /api/agent/list
```

#### 获取 Agent 状态
```
GET /api/agent/{agent_id}/status
```

#### 重启 Agent
```
POST /api/agent/{agent_id}/restart
```

#### 注销 Agent
```
DELETE /api/agent/{agent_id}
```

#### 获取 Agent 所有进程信息
```
GET /api/agent/{agent_id}/processes
```

**响应示例:**
```json
{
    "code": 200,
    "message": "获取进程信息成功",
    "data": {
        "agent_id": "agent_123",
        "timestamp": 1741584000,
        "total_processes": 85,
        "processes": [
            {
                "pid": 1234,
                "name": "python",
                "status": "running",
                "cpu_percent": 2.5,
                "memory_rss": 52428800,
                "memory_vms": 104857600,
                "create_time": 1741583000.5,
                "username": "admin",
                "cmdline": ["python", "main.py"],
                "exe": "/usr/bin/python3.9",
                "num_threads": 4,
                "io_read_bytes": 102400,
                "io_write_bytes": 51200,
                "connections": []
            }
        ]
    }
}
```

#### 获取 Agent 进程统计摘要
```
GET /api/agent/{agent_id}/processes/summary
```

**响应示例:**
```json
{
    "code": 200,
    "message": "获取进程摘要成功",
    "data": {
        "agent_id": "agent_123",
        "timestamp": 1741584000,
        "statistics": {
            "total_processes": 85,
            "total_cpu_usage": 15.5,
            "total_memory_bytes": 2147483648,
            "total_memory_mb": 2048.0,
            "total_threads": 420
        },
        "processes": [...]
    }
}
```

### 2. 任务管理

#### 创建任务
```
POST /api/task/create
Content-Type: application/json

{
    "type": "install",
    "parameters": {
        "file_path": "/tmp/app.zip",
        "install_dir": "/opt/my_app"
    },
    "priority": 1,
    "retry": 3,
    "timeout": 300
}
```

#### 获取任务列表
```
GET /api/task/list?status=pending&limit=100
```

#### 获取任务详情
```
GET /api/task/{task_id}
```

#### 取消任务
```
POST /api/task/{task_id}/cancel
```

### 3. 系统管理

#### 系统信息
```
GET /api/system/info
```

#### 系统统计
```
GET /api/system/stats
```

#### 系统日志
```
GET /api/system/logs?limit=100
```

#### 健康检查
```
GET /api/system/health
```

## 配置

在 `config.yaml` 中添加了 FastAPI 配置：

```yaml
server:
  fastapi:
    host: 0.0.0.0      # 监听地址
    port: 8000         # 端口号
    workers: 4         # 工作进程数
    reload: true       # 开发模式自动重载
```

## 依赖包

需要安装以下依赖：

```bash
pip install fastapi uvicorn pydantic
```

或使用项目 requirements.txt：

```bash
pip install -r requirements.txt
```

## 开发说明

### 1. 添加新路由
1. 在 `api/routes/` 目录下创建新的路由文件
2. 在 `app.py` 中导入并注册路由

### 2. 数据验证
- 使用 Pydantic 模型进行数据验证
- 在路由中使用 `BaseModel` 定义请求/响应模型

### 3. 错误处理
- 使用 FastAPI 的 `HTTPException` 处理错误
- 统一错误响应格式

### 4. 日志记录
- 所有 API 调用会自动记录日志
- 可在 `app.py` 中配置日志级别

## 部署建议

### 开发环境
```bash
uvicorn api.app:app --reload --host 0.0.0.0 --port 8000
```

### 生产环境
```bash
uvicorn api.app:app --host 0.0.0.0 --port 8000 --workers 4
```

### Docker 部署
```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
```

## 与现有系统的集成

### 1. Agent 注册
- Agent 仍使用原有的注册接口 `POST /api/agent/register`
- 新的 FastAPI 服务也提供相同的接口，可用于测试和管理

### 2. 任务队列
- 任务创建后仍存储到 Redis 队列
- Agent 从 Redis 队列中获取任务并执行

### 3. 心跳机制
- Agent 心跳信息仍存储到 Redis
- API 服务从 Redis 读取 Agent 状态信息

## 监控和告警

### 健康检查接口
```
GET /health          # 根路径健康检查
GET /api/system/health  # 详细健康检查
```

### 监控指标
- Agent 在线率
- 任务成功率
- 系统资源使用率
- API 响应时间

## 安全建议

1. **生产环境配置**：
   - 修改 CORS 配置，限制允许的域名
   - 启用 HTTPS
   - 添加身份认证

2. **API 认证**：
   ```python
   # 可以在 app.py 中添加全局认证中间件
   from fastapi import Security
   from fastapi.security import APIKeyHeader
   
   api_key_header = APIKeyHeader(name="X-API-Key")
   
   async def verify_api_key(api_key: str = Security(api_key_header)):
       if api_key != "your-secret-key":
           raise HTTPException(status_code=403, detail="Invalid API Key")
   ```

3. **速率限制**：
   - 使用中间件限制 API 调用频率
   - 防止恶意请求