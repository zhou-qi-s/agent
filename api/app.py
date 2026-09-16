"""
FastAPI 应用程序主文件
提供 Agent 的管理 API 接口
"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routes import agent, alarm, task, system, process, version, harbor, helm

app = FastAPI(
    title="Agent API",
    description="Agent接口",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# —— IP 白名单中间件 ——
@app.middleware("http")
async def ip_whitelist_middleware(request: Request, call_next):
    """仅允许本地回环 + 配置的 host IP 访问"""
    allowed_host = getattr(app.state, "allowed_host", None)
    if allowed_host is not None:
        client_ip = request.client.host if request.client else None
        if client_ip and client_ip not in ("127.0.0.1", "::1", allowed_host):
            return JSONResponse(
                status_code=403,
                content={"detail": f"禁止访问: IP {client_ip} 不在白名单中"},
            )
    return await call_next(request)

# 配置 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该指定具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(agent.router, prefix="/api/agent", tags=["agent"])
app.include_router(task.router, prefix="/api/task_utils", tags=["task_utils"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(alarm.router, prefix="/api/alarm", tags=["alarm"])
app.include_router(process.router, prefix="/api/process", tags=["process"])
app.include_router(version.router, prefix="/api/version", tags=["version"])
app.include_router(harbor.router, prefix="/api/harbor", tags=["harbor"])
app.include_router(helm.router, prefix="/api/helm", tags=["helm"])

# 健康检查接口
@app.get("/")
async def root():
    """根路径，返回 API 基本信息"""
    return {
        "name": "Agent Management API",
        "version": "1.0.0",
        "docs": "/docs",
        "redoc": "/redoc"
    }

@app.get("/health")
async def health_check():
    """健康检查接口"""
    return {"status": "healthy", "timestamp": "2026-03-09T12:00:00Z"}