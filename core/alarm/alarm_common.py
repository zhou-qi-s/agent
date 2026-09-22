"""
告警公共模块

提供 alarm.py 和 k8s_alarm.py 共用的常量、缓存和上报能力。

包含:
- 告警类型常量
- 告警冷却时间
- 告警时间缓存
- get_local_ip()
- _build_alarm_url()
- report_alarm()
"""

import time
from typing import Dict

import requests

from utils.config_loader import load_config
from utils.logger import logger


# =========================================================
# 告警类型
# =========================================================
ALARM_TYPE_CPU = 1
ALARM_TYPE_MEMORY = 2
ALARM_TYPE_IO = 3
ALARM_TYPE_PROCESS_NOT_FOUND = 4


# =========================================================
# 告警冷却时间（秒）
# =========================================================
ALARM_COOLDOWN = 300


# =========================================================
# 告警缓存  {key: last_time}
# alarm.py 使用 f"{pid}_{alarm_type}" 作为 key
# k8s_alarm.py 使用 f"k8s_{namespace}_{pod_name}_{alarm_type}" 作为 key
# =========================================================
last_alarm_time: Dict[str, float] = {}


# =========================================================
# 工具方法
# =========================================================

def get_local_ip() -> str:
    """获取本机 IP（统一使用 utils/util.py 的方法）"""
    from utils.util import get_ip
    return get_ip() or "unknown"


def _build_alarm_url() -> str:
    """从 config.yaml 读取告警上报接口地址"""
    config = load_config()
    server_cfg = config.get("server", {})
    alarm_api = config.get("interface", {}).get("alarm", "/api/agent/alarm")
    server_ip = server_cfg.get("iP", "http://127.0.0.1")
    server_port = server_cfg.get("port", 30000)
    # server.iP 可能已经包含 http:// 前缀
    if not server_ip.startswith("http"):
        server_ip = f"http://{server_ip}"
    return f"{server_ip}:{server_port}{alarm_api}"


def report_alarm(
        alarm_type: int,
        service_name: str,
        alarm_name: str,
        content: str,
) -> bool:
    """
    上报告警到服务端。

    参数格式对应 Java AlarmForm:
        alarmName  - 告警名称
        alarmType  - 告警类型 (1:CPU, 2:内存, 3:IO, 4:进程不存在)
        ip         - 本机 IP
        content    - 告警内容
        alarmTime  - 告警时间(毫秒时间戳)
    """
    try:
        url = _build_alarm_url()
        current_time_ms = int(time.time() * 1000)

        payload = {
            "alarmName": alarm_name,
            "alarmType": alarm_type,
            "ip": get_local_ip(),
            "content": content,
            "alarmTime": current_time_ms,
        }

        # === 诊断日志：确认实际上报的 alarmType ===
        logger.info(
            "[ALARM-SEND] alarmType=%s alarmName=%s ip=%s url=%s",
            alarm_type, alarm_name, get_local_ip(), url,
        )

        response = requests.post(url, json=payload, timeout=5)
        success = response.status_code == 200

        if success:
            logger.info("[ALARM] 上报成功: %s", payload)
        else:
            logger.warning("[ALARM] 上报失败: %s", response.text)

        return success

    except Exception as e:
        logger.warning("[ALARM] 上报异常: %s", e)
        return False
