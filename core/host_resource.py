"""
宿主资源独立采集器

按 `core/process/process_info.py`（进程资源）那套思路来做：
**独立采集 → 写自己的 Redis 键（带 TTL）→ 自己的循环**，不再把资源数据塞在心跳负载里。

为什么改：
- 原来 CPU/内存/磁盘/网络/GPU 是塞在【心跳负载】里的，而心跳只在 `timer == 0`（每 3 跳 = 15 秒）才采一次，
  且首次注册走的是 else 分支、压根不带 host_info → 刚纳管的机器前 15 秒节点监控抽屉下半截是空白；
- 心跳里的 `psutil.cpu_percent()` 没传 interval，首次调用恒为 0.0。

现在：
- 每 10 秒采一次（与 `core/alarm/node_alarm.py` 的 NodeResource 同节奏），**启动即采一次**，纳管后立刻有数据；
- 写入独立键 `agent:metrics:{agent_id}`（`utils/redis_store.py` 里已定义好的约定），TTL 60 秒；
- 采集结果在本地缓存一份，心跳负载仍可复用，保持对旧消费方的兼容。
"""

import threading
import time

from utils.logger import logger
from utils.redis_store import redisUtils

# 资源数据在 Redis 里的存活时间（秒）：比采集间隔宽裕，容忍偶发采集失败
METRICS_TTL = 60
# 采集间隔（秒）
COLLECT_INTERVAL = 10

# 最近一次采集结果（心跳负载复用它，避免重复采集）
_cached_host_info = None
_cache_lock = threading.Lock()

# 上一次网络累计值采样：(时间戳, 发送字节, 接收字节) —— 用于算实时速率
_last_net_sample = None


def get_cached_host_info():
    """取最近一次采集到的宿主信息；还没采过则返回 None"""
    return _cached_host_info


def _with_network_rate(info):
    """
    给 network 段补上【实时速率】（字节/秒）。

    psutil.net_io_counters() 给的是"自开机累计"量、不是速率，所以要两次采样做差：
        (本次累计 - 上次累计) / 两次实际间隔秒数

    首次采集没有上一次样本 → 不加 rate 字段（前端显示"—"），10 秒后的下一次就有了；
    计数器在机器重启后会归零 → 出现负差时同样不加 rate 字段，避免显示离谱数字。
    """
    global _last_net_sample

    net = (info or {}).get("network") or {}
    sent = net.get("bytes_sent")
    recv = net.get("bytes_recv")
    if sent is None or recv is None:
        return info

    now = time.time()
    prev = _last_net_sample
    if prev:
        prev_ts, prev_sent, prev_recv = prev
        elapsed = now - prev_ts
        if elapsed > 0 and sent >= prev_sent and recv >= prev_recv:
            net["send_rate"] = round((sent - prev_sent) / elapsed, 1)
            net["recv_rate"] = round((recv - prev_recv) / elapsed, 1)
            net["rate_interval"] = round(elapsed, 1)
    _last_net_sample = (now, sent, recv)
    return info


def collect_once(agent_id) -> dict:
    """采集一次宿主资源并写入 Redis（agent:metrics:{agent_id}）"""
    global _cached_host_info
    # 延迟导入，避免与 core.heartbeat 形成循环依赖
    from core.heartbeat import get_host_info

    info = _with_network_rate(get_host_info())
    with _cache_lock:
        _cached_host_info = info

    try:
        redisUtils.save_metrics(agent_id, info, ttl=METRICS_TTL)
        logger.info(
            "[HOST-RESOURCE] 已上报: cpu=%.1f%% mem=%.1f%% disk=%.1f%% process=%s",
            (info.get("cpu") or {}).get("total_usage_percent", -1),
            (info.get("memory") or {}).get("percent", -1),
            (info.get("disk") or {}).get("percent", -1),
            (info.get("system") or {}).get("process_count"),
        )
    except Exception as e:
        logger.warning("[HOST-RESOURCE] 上报失败: %s", e)
    return info


def host_resource_loop(agent_id, interval: int = COLLECT_INTERVAL):
    """宿主资源采集循环：启动先采一次，之后每 interval 秒一次"""
    logger.info("启动宿主资源采集线程（每 %s 秒，Redis key: agent:metrics:%s）...", interval, agent_id)
    while True:
        try:
            collect_once(agent_id)
        except Exception as e:
            logger.warning("[HOST-RESOURCE] 采集异常: %s", e)
        time.sleep(interval)
