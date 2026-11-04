# -*- coding: utf-8 -*-
"""Agent 回调平台接口的签名头（2026-11-04 安全整改）

平台侧对以下免登录写接口做了 HMAC 校验：
    /api/agent/register、/task_report、/process、/process/version、
    /alarm、/tcp/snapshot、/k8s_report、/k8s_log、/gateway/receive

签名算法（与后端 jnpf.config.AgentSignatureFilter 一致）：
    X-Agent-Timestamp = 秒级时间戳
    X-Agent-Signature = hex( HMAC_SHA256(server.secret, timestamp + "\\n" + url_path) )

用法（所有回调平台的地方都加上 headers）：
    from utils.agent_sign import sign_headers
    resp = requests.post(url, json=payload, timeout=5, headers=sign_headers(url))

未在 config.yaml 配 server.secret 时返回空 dict —— 与平台配置 secret 为空的行为一致，
即「两边都没配就不校验」，不会把老环境打死；要开严格校验必须两边一起配。
"""
import hashlib
import hmac
import time
from urllib.parse import urlparse

_SECRET = None


def _get_secret():
    """惰性读取 server.secret（配置加载失败时按未配置处理）"""
    global _SECRET
    if _SECRET is None:
        try:
            from utils.config_loader import load_config
            cfg = load_config("config.yaml") or {}
            _SECRET = str((cfg.get("server") or {}).get("secret") or "").strip()
        except Exception:
            _SECRET = ""
    return _SECRET


def reset_secret_cache():
    """配置热更新后调用，强制重新读取 secret"""
    global _SECRET
    _SECRET = None


def sign_headers(url):
    """返回签名头 dict；未配置 secret 时返回空 dict"""
    secret = _get_secret()
    if not secret:
        return {}
    path = urlparse(str(url)).path or "/"
    ts = str(int(time.time()))
    msg = (ts + "\n" + path).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {"X-Agent-Timestamp": ts, "X-Agent-Signature": sig}
