"""
独立的 FastAPI 服务器模块
"""
import uvicorn
from utils.config_loader import load_config
from api.app import app

def start_fastapi_server():
    """启动 FastAPI 服务器"""
    # 加载配置
    config = load_config("config.yaml")
    server_config = config.get('server', {}).get('fastapi', {})
    
    host = server_config.get('host', '0.0.0.0')
    port = server_config.get('port', 8000)
    reload_flag = server_config.get('reload', True)
    
    print("=" * 50)
    print(f"🚀 启动 FastAPI API 服务")
    print(f"📡 地址: http://{host}:{port}")
    print(f"📖 文档: http://{host}:{port}/docs")
    print(f"📚 ReDoc: http://{host}:{port}/redoc")
    print("=" * 50)
    
    # 配置 Uvicorn
    uvicorn_config = {
        "app": "api.app:app",
        "host": host,
        "port": port,
        "reload": reload_flag,
        "log_level": "info",
    }
    
    # 启动 FastAPI 服务
    uvicorn.run(**uvicorn_config)

if __name__ == "__main__":
    start_fastapi_server()