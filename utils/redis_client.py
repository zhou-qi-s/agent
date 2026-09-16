import redis

from utils.config_loader import load_config
from utils.logger import logger

# ================== 全局变量（只初始化一次） ==================
_redis_client = None


def init_redis():
    """
    初始化 Redis 连接池（只执行一次）
    在 main 启动时调用
    """
    global _redis_client

    if _redis_client is not None:
        return _redis_client

    config = load_config()
    redis_cfg = config.get("server", {}).get("redis", {})
    pool_cfg = redis_cfg.get("lettuce", {}).get("pool", {})

    host = redis_cfg.get("host", "127.0.0.1")
    port = redis_cfg.get("port", 6379)
    db = redis_cfg.get("db", 0)
    password = redis_cfg.get("password")

    max_connections = pool_cfg.get("max_connections", 8)
    socket_timeout = pool_cfg.get("socket_timeout", 5)
    socket_connect_timeout = pool_cfg.get("socket_connect_timeout", 5)
    # ================== 构建连接池 ==================
    pool = redis.ConnectionPool(
        host=host,
        port=port,
        db=db,
        password=password,
        max_connections=max_connections,  # 最大连接数
        socket_timeout=socket_timeout,  # 读写超时
        socket_connect_timeout=socket_connect_timeout,  # 连接超时
        retry_on_timeout=True,  # 超时自动重试
        decode_responses=True  # 自动解码为字符串
    )

    _redis_client = redis.Redis(connection_pool=pool)


    # 测试连接
    try:
        _redis_client.ping()
        logger.info("Redis 连接成功")
    except Exception as e:
        logger.error(f"Redis 连接失败: {e}")
        raise RuntimeError("Redis 初始化失败")
    return _redis_client
def get_redis():

    """
    获取 Redis 客户端（全局单例）
    """
    if _redis_client is None:
        return init_redis()
    return _redis_client

