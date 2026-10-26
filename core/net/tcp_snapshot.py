# -*- coding: utf-8 -*-
"""TCP 连接快照采集与上报

为什么需要：
    Nacos 只有"注册表"（哪个服务注册在哪个 ip:port），没有"谁在调谁"。
    平台的「服务上下游关系」要真实流量，所以由每台纳管机器上的 Agent
    周期性抓本机 ESTABLISHED 的 TCP 连接，把 (本地 ip:port -> 远端 ip:port, 进程)
    上报给后端；后端再把 ip:port 对回 Nacos 服务名，就得到调用边。

约定：
    - 只采 ESTABLISHED，跳过回环地址（否则全是本机自连接，噪声大）。
    - 上报是"整机快照"语义，后端按节点 IP 全量覆盖，不做增量。
    - 采不到（非 root 只能看到本进程的连接）就照常上报空快照，
      平台侧会显示"该节点暂无连接数据"，不会沿用上一次的旧数据。
"""
import logging
import socket
import time

import requests

try:
    import psutil
except ImportError:  # 理论上 requirements 里已有，兜底不影响 Agent 启动
    psutil = None

from utils.config_loader import load_config

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL = 15      # 采集/上报间隔（秒）
MAX_CONNS = 800            # 单次上报上限，防止一台机器几万连接把后端打爆
REPORT_TIMEOUT = 5         # 上报超时（秒），与告警上报保持一致


def _build_url(config):
    """按 server.iP + server.port + interface.tcp_snapshot 拼上报地址"""
    server = config.get("server") or {}
    api = (config.get("interface") or {}).get("tcp_snapshot") or "/api/agent/tcp/snapshot"
    host = str(server.get("iP") or "http://127.0.0.1")
    if not host.startswith("http"):
        host = "http://" + host
    port = server.get("port") or 30000
    return "%s:%s%s" % (host, port, api)


def _get_local_ip():
    try:
        from utils.util import get_ip
        return get_ip() or ""
    except Exception:
        return ""


def _is_loopback(ip):
    if not ip:
        return True
    if ip.startswith("127.") or ip == "::1" or ip == "0.0.0.0":
        return True
    return ip.startswith("::ffff:127.")


def _norm_ip(ip):
    """把 IPv6 映射地址还原成 IPv4

    Linux 上 psutil 常返回 ::ffff:192.168.0.5 这种形式，
    不还原的话跟 Nacos 里的 192.168.0.5 对不上，前端就永远匹配不到服务名。
    """
    if not ip:
        return ""
    if ip.startswith("::ffff:"):
        return ip[7:]
    return ip


def _pid_name(pid, cache):
    """pid -> 进程名，带缓存（一次采集里同一个 pid 只查一次）"""
    if not pid:
        return ""
    if pid in cache:
        return cache[pid]
    name = ""
    try:
        proc = psutil.Process(pid)
        name = proc.name() or ""
    except Exception:
        name = ""
    cache[pid] = name
    return name


def collect_connections():
    """采一次本机 ESTABLISHED 连接，按 (本地,远端,pid) 聚合计数

    返回: [{"localIp","localPort","remoteIp","remotePort","pid","processName","count"}]
    """
    if psutil is None:
        logger.warning("[TCP-SNAPSHOT] psutil 不可用，跳过采集")
        return []

    try:
        conns = psutil.net_connections(kind="tcp")
    except Exception as e:
        logger.warning("[TCP-SNAPSHOT] 读取 TCP 连接失败（非 root 权限受限？）: %s", e)
        return []

    cache = {}
    agg = {}
    denied = 0
    for conn in conns:
        try:
            if getattr(conn, "status", "") != psutil.CONN_ESTABLISHED:
                continue
            laddr = getattr(conn, "laddr", None)
            raddr = getattr(conn, "raddr", None)
            if not laddr or not raddr:
                continue
            lip, lport = _norm_ip(laddr[0]), laddr[1]
            rip, rport = _norm_ip(raddr[0]), raddr[1]
            if _is_loopback(rip):
                continue
            pid = getattr(conn, "pid", None)
            if pid is None:
                denied += 1
            key = (lip, lport, rip, rport, pid)
            if key in agg:
                agg[key]["count"] += 1
            else:
                agg[key] = {
                    "localIp": lip,
                    "localPort": int(lport),
                    "remoteIp": rip,
                    "remotePort": int(rport),
                    "pid": pid,
                    "processName": _pid_name(pid, cache),
                    "count": 1,
                }
        except Exception:
            continue

    result = sorted(agg.values(), key=lambda x: x["count"], reverse=True)
    if len(result) > MAX_CONNS:
        result = result[:MAX_CONNS]
    if denied:
        logger.debug("[TCP-SNAPSHOT] 有 %s 条连接的 pid 拿不到（非 root 运行）", denied)
    return result


def report_once():
    """采一次并上报；任何异常都吞掉，绝不打断常驻线程"""
    cfg = load_config() or {}
    connections = collect_connections()
    payload = {
        "ip": _get_local_ip(),
        "hostname": socket.gethostname(),
        "collectTime": int(time.time() * 1000),
        "connections": connections,
    }
    try:
        url = _build_url(cfg)
        resp = requests.post(url, json=payload, timeout=REPORT_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("[TCP-SNAPSHOT] 上报失败 HTTP %s", resp.status_code)
            return False
        return True
    except Exception as e:
        logger.warning("[TCP-SNAPSHOT] 上报异常: %s", e)
        return False


def tcp_snapshot_loop(interval=None):
    """常驻采集线程（仿 core/host_resource.py 的写法）"""
    cfg = (load_config() or {}).get("agent") or {}
    tcp_cfg = cfg.get("tcp_collect") or {}
    sec = int(interval or tcp_cfg.get("interval") or DEFAULT_INTERVAL)
    if sec < 5:
        sec = 5
    logger.info("[TCP-SNAPSHOT] 启动，每 %s 秒采集上报一次", sec)
    while True:
        try:
            report_once()
        except Exception as e:
            logger.warning("[TCP-SNAPSHOT] 采集循环异常: %s", e)
        time.sleep(sec)
