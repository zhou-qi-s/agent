import getpass
import socket
import time
import uuid

import requests

from utils.config_loader import load_config
from utils.logger import logger
from utils.util import get_ip

# ================== 读取配置 ==================
CONFIG = load_config()

server = CONFIG['server']
interface = CONFIG['interface']
agent_cfg = CONFIG.get('agent', {}).get('retry', {})

RETRY_COUNT = agent_cfg.get('count', 3)  # 默认重试3次
RETRY_INTERVAL = agent_cfg.get('interval', 3)  # 默认间隔3秒
version = CONFIG.get('version', '1.0.0')
server_config = CONFIG.get('server', {}).get('fastapi', {})
fastapi_port = server_config.get('port', 8000)
compartment = server['compartment']
# ================== 组装URL ==================
REGISTER_URL = f"{server['iP']}:{server['port']}{interface.get('register')}"


# ================== Agent 注册函数 ==================
def register_agent():
    username = getpass.getuser()
    hostname = socket.gethostname()

    # 使用改进的IP获取方法
    ip = get_ip()
    if ip == '127.0.0.1':
        logger.warning(f"获取到的IP地址是回环地址: {ip}")
        # 尝试获取所有IP地址
        from utils.util import get_all_ips
        all_ips = get_all_ips()
        if all_ips:
            logger.info(f"所有网络接口IP: {all_ips}")
            # 选择第一个非回环地址
            for ip_info in all_ips:
                if not ip_info['ip'].startswith('127.'):
                    ip = ip_info['ip']
                    break
    
    agent_id = str(uuid.uuid4())

    # 节点类型（config.yaml server.type，逗号分隔，如 VIRTUAL_MACHINE, DISPLAY_CONSOLE）
    node_type = (server.get('type') or '').strip()

    data = {
        "username": username,
        "ip": ip,
        "create_time": int(time.time()),
        "version": version,
        "port": fastapi_port,
        "compartment": compartment,
        "type": node_type,
    }

    retry = 0

    while retry < RETRY_COUNT:
        try:
            logger.info(f"第 {retry + 1} 次尝试注册 -> {REGISTER_URL}")

            resp = requests.post(REGISTER_URL, json=data, timeout=5)

            # HTTP 成功
            if resp.status_code == 200:
                logger.info(f"Agent注册完成，服务器返回：{resp}")
                return resp.text

            # HTTP 失败
            else:
                logger.warning(f"HTTP状态码：{resp.status_code}")

        except Exception as e:
            logger.error(f"注册异常：{e}")

        retry += 1
        if retry < RETRY_COUNT:
            logger.info(f"{RETRY_INTERVAL} 秒后进行第 {retry + 1} 次重试...")
            time.sleep(RETRY_INTERVAL)

    logger.error("已达到最大重试次数，Agent注册失败")
    return None
