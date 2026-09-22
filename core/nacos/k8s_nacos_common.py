"""
K8s Nacos 共享模块 — heartbeat 和 register 共用的基础能力。

提供：
  - 配置读取（kubeconfig, Nacos 地址）
  - kubectl JSON 执行封装
  - Pod 发现（discover_k8s_containers）
  - serviceName 解析
  - Nacos 认证/登录（带缓存、线程安全、失败冷却）
  - HTTP Session 连接池复用
"""

import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from utils.config_loader import load_config

_CONFIG = load_config()

# ── Timeout ──
LOGIN_TIMEOUT = (3, 5)          # 登录：3s 连接, 5s 读取

# ── Kubeconfig fallback ──
_DEFAULT_KUBECONFIG = "/etc/kubernetes/admin.conf"

# ── 缓存的 accessToken ──
_TOKEN: Optional[str] = None
_TOKEN_TTL: float = 0.0

# ── 登录失败冷却（防止凭证错误时多 worker 同时撞墙）──
_LOGIN_FAILURE_COUNT: int = 0
_LAST_LOGIN_FAILURE_TIME: float = 0.0
LOGIN_FAILURE_COOLDOWN = 120            # 登录连续失败后的冷却期（秒）

# ── Token 刷新锁 ──
_TOKEN_LOCK = threading.Lock()

# ── HTTP Session 复用 ──
_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()

# ── ReplicaSet → Deployment 缓存锁（heartbeat/register 两个后台线程共享）──
_RS_DEPLOYMENT_LOCK = threading.Lock()


# =============================================================================
# HTTP Session
# =============================================================================

def get_session(pool_size: int = 20) -> requests.Session:
    """获取持久化 HTTP Session，复用 TCP 连接。pool_size 应 ≥ 并发 worker 数。"""
    global _SESSION
    # 快速路径：无锁读取（Session 对象赋值在 CPython 中是原子的，最坏读到 None 走慢路径）
    session = _SESSION
    if session is not None:
        return session

    with _SESSION_LOCK:
        # 双重检查：锁内再次确认未被其他线程初始化
        if _SESSION is not None:
            return _SESSION
        _SESSION = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_size + 5,
            pool_maxsize=pool_size + 5,
            max_retries=0,
        )
        _SESSION.mount("http://", adapter)
        _SESSION.mount("https://", adapter)
    return _SESSION


# =============================================================================
# 配置读取
# =============================================================================

def get_kubeconfig() -> str:
    """获取 kubeconfig 路径"""
    k8s_cfg = _CONFIG.get("k8s", {})
    helm_cfg = _CONFIG.get("helm", {})
    return (
        k8s_cfg.get("download", "")
        or helm_cfg.get("config", "")
        or _DEFAULT_KUBECONFIG
    )


def get_nacos_address() -> str:
    """获取 Nacos 服务地址"""
    nacos_cfg = _CONFIG.get("nacos", {})
    address = str(nacos_cfg.get("address", "") or "").strip()
    if not address:
        ip = str(nacos_cfg.get("ip", "") or "").strip()
        port = str(nacos_cfg.get("port", "") or "").strip()
        if ip:
            base = ip if ip.startswith(("http://", "https://")) else f"http://{ip}"
            address = f"{base}:{port}" if port else base
    return address


# =============================================================================
# kubectl 封装
# =============================================================================

def run_kubectl(log_tag: str, args: list, timeout: int = 30) -> Optional[dict]:
    """执行 kubectl 命令并返回 JSON 结果，失败返回 None。"""
    kubeconfig = get_kubeconfig()
    env = os.environ.copy()
    env["KUBECONFIG"] = kubeconfig

    cmd = ["kubectl"] + args
    logging.debug("[%s] 执行: %s", log_tag, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            logging.error("[%s] kubectl 失败: %s", log_tag, result.stderr.strip())
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logging.error("[%s] kubectl 超时", log_tag)
        return None
    except json.JSONDecodeError as e:
        logging.error("[%s] kubectl 输出 JSON 解析失败: %s", log_tag, e)
        return None
    except Exception as e:
        logging.error("[%s] kubectl 异常: %s", log_tag, e)
        return None


# =============================================================================
# Pod 发现
# =============================================================================

# ── ReplicaSet → Deployment 缓存 ──
_RS_DEPLOYMENT_CACHE: Dict[str, str] = {}


def _resolve_rs_deployment(log_tag: str, rs_name: str, namespace: str) -> str:
    """通过 kubectl get rs 查询 ReplicaSet 的 ownerReferences 获取 Deployment 名。

    线程安全：_RS_DEPLOYMENT_CACHE 可能被 heartbeat 和 register 两个后台线程
    同时访问，使用 _RS_DEPLOYMENT_LOCK 保护。
    """
    cache_key = f"{namespace}/{rs_name}"

    # 快速路径：无锁读取（dict.get 在 CPython GIL 下是原子的）
    with _RS_DEPLOYMENT_LOCK:
        cached = _RS_DEPLOYMENT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    data = run_kubectl(log_tag, ["get", "rs", rs_name, "-n", namespace, "-o", "json"])
    if not data:
        return rs_name

    rs_owner_refs = data.get("metadata", {}).get("ownerReferences", [])
    result = rs_name
    for ref in rs_owner_refs:
        if ref.get("kind") == "Deployment":
            result = str(ref.get("name", ""))
            break

    with _RS_DEPLOYMENT_LOCK:
        _RS_DEPLOYMENT_CACHE[cache_key] = result
    return result


def extract_owner_name(metadata: Dict[str, Any], log_tag: str = "",
                       namespace: str = "") -> str:
    """从 Pod metadata 提取所属工作负载名称。

    对 Deployment Pod：通过 ReplicaSet 的 ownerReferences 精确解析 Deployment 名。
    对 StatefulSet / DaemonSet / Job 等：直接取 ownerReferences 名称。
    """
    owner_refs = metadata.get("ownerReferences", [])
    if not owner_refs:
        return ""

    rs_name = ""
    for ref in owner_refs:
        kind = ref.get("kind", "")
        if kind == "ReplicaSet":
            rs_name = str(ref.get("name", ""))
            continue
        if kind:
            return str(ref.get("name", ""))

    if rs_name:
        return _resolve_rs_deployment(log_tag, rs_name, namespace)

    return ""


def discover_k8s_containers(log_tag: str, skip_host_network: bool = False) -> List[Dict[str, Any]]:
    """发现 k8s 所有命名空间中 Running 状态的 Pod。

    参数:
        log_tag: 日志标签
        skip_host_network: 是否跳过 hostNetwork 模式的 Pod（注册场景应跳过）

    返回:
        [{namespace, pod_name, pod_ip, labels, owner_name, containers: [{name, ports, image}]}, ...]
    """
    data = run_kubectl(log_tag, ["get", "pods", "-A", "-o", "json"])
    if not data:
        return []

    items = data.get("items", [])
    containers_list: List[Dict[str, Any]] = []

    for item in items:
        status = item.get("status", {})
        phase = status.get("phase", "")
        pod_ip = status.get("podIP", "")

        if phase != "Running" or not pod_ip:
            continue

        metadata = item.get("metadata", {})
        namespace = metadata.get("namespace", "")
        pod_name = metadata.get("name", "")
        labels = metadata.get("labels", {})

        spec = item.get("spec", {})
        if skip_host_network and spec.get("hostNetwork", False):
            logging.debug("[%s] 跳过 hostNetwork Pod: %s/%s", log_tag, namespace, pod_name)
            continue

        owner_name = extract_owner_name(metadata, log_tag, namespace)
        pod_containers = spec.get("containers", [])

        container_infos: List[Dict[str, Any]] = []
        for c in pod_containers:
            ports = [
                p.get("containerPort")
                for p in c.get("ports", [])
                if p.get("containerPort")
            ]
            container_infos.append({
                "name": c.get("name", ""),
                "ports": ports,
                "image": c.get("image", ""),
            })

        if not container_infos:
            continue

        containers_list.append({
            "namespace": namespace,
            "pod_name": pod_name,
            "pod_ip": pod_ip,
            "labels": labels,
            "owner_name": owner_name,
            "containers": container_infos,
        })

    return containers_list


def resolve_service_name(pod_info: Dict[str, Any]) -> str:
    """从 Pod 信息解析 Nacos serviceName。

    优先级：app 标签 > owner_name > pod_name 前缀
    """
    labels = pod_info.get("labels", {})

    for key in ("app", "app.kubernetes.io/name", "name", "component"):
        val = labels.get(key, "")
        if val:
            return str(val)

    owner = pod_info.get("owner_name", "")
    if owner:
        return owner

    pod_name = pod_info.get("pod_name", "unknown")
    parts = pod_name.rsplit("-", 2)
    if len(parts) >= 3 and len(parts[-1]) >= 5 and len(parts[-2]) >= 5:
        return parts[0]
    return pod_name


# =============================================================================
# Nacos 认证
# =============================================================================

def is_token_invalid(body_text: str) -> bool:
    """根据返回体判断 token 是否已失效"""
    lower = body_text.lower()
    token_keywords = [
        "token", "expired", "invalid", "unauthorized",
        "access token", "login", "unauthenticated",
    ]
    return any(kw in lower for kw in token_keywords)


def nacos_login(log_tag: str, username: Optional[str], password: Optional[str],
                address: str, force_refresh: bool = False) -> str:
    """登录 Nacos 获取 accessToken。带缓存，线程安全，自动兼容 2.x/3.x。

    连续登录失败会进入冷却期，防止凭证错误时多 worker 同时撞墙。

    参数:
        log_tag: 日志标签
        username / password: Nacos 凭证（None 或空则跳过登录）
        address: Nacos 服务地址
        force_refresh: 强制重新登录（用于 token 失效场景）

    返回:
        accessToken 字符串，失败返回 ""
    """
    global _TOKEN, _TOKEN_TTL, _LOGIN_FAILURE_COUNT, _LAST_LOGIN_FAILURE_TIME

    # 快速路径：无锁检查缓存（CPython GIL 保证 str/float 读取原子性，
    # 最坏情况读到过期 token → 多一次 HTTP 调用，不影响正确性）
    if not force_refresh:
        token = _TOKEN
        if token and time.time() < _TOKEN_TTL:
            return token

    with _TOKEN_LOCK:
        # ── 强制刷新冷却期 ──
        if force_refresh:
            if _LOGIN_FAILURE_COUNT > 0:
                elapsed = time.time() - _LAST_LOGIN_FAILURE_TIME
                if elapsed < LOGIN_FAILURE_COOLDOWN:
                    _TOKEN = None
                    _TOKEN_TTL = 0.0
                    return ""
                _LOGIN_FAILURE_COUNT = 0  # 冷却期过，允许重试
            _TOKEN = None
            _TOKEN_TTL = 0.0
        else:
            token = _TOKEN
            if token and time.time() < _TOKEN_TTL:
                return token

        if not username or not password:
            return ""
        if not address:
            return ""

        login_data = {"username": str(username), "password": str(password)}
        base_url = address.rstrip("/")

        login_paths = [
            ("/nacos/v1/auth/login",       "Nacos 2.x"),
            ("/nacos/v3/auth/user/login",  "Nacos 3.x"),
        ]

        for path, version_label in login_paths:
            login_url = f"{base_url}{path}"
            try:
                resp = get_session(20).post(login_url, data=login_data, timeout=LOGIN_TIMEOUT)
                body = (resp.text or "")[:300]
                if resp.status_code == 200:
                    data = resp.json()
                    token = str(data.get("accessToken", ""))
                    if not token:
                        logging.warning("[%s] %s 返回 200 但无 accessToken: %s", log_tag, version_label, body)
                        continue
                    ttl = int(data.get("tokenTtl", 18000))
                    _TOKEN = token
                    _TOKEN_TTL = time.time() + ttl - 60
                    _LOGIN_FAILURE_COUNT = 0
                    logging.info("[%s] Nacos 登录成功(%s), tokenTTL=%ds", log_tag, version_label, ttl)
                    return _TOKEN
                else:
                    _LOGIN_FAILURE_COUNT += 1
                    _LAST_LOGIN_FAILURE_TIME = time.time()
                    if _LOGIN_FAILURE_COUNT == 1 or _LOGIN_FAILURE_COUNT % 10 == 0:
                        logging.warning(
                            "[%s] %s 登录失败(%s): %s (连续失败 %d 次, 进入 %d 秒冷却)",
                            log_tag, version_label, resp.status_code, body,
                            _LOGIN_FAILURE_COUNT, LOGIN_FAILURE_COOLDOWN,
                        )
                    if "No static resource" in body or resp.status_code == 404:
                        continue
                    return ""
            except Exception as e:
                _LOGIN_FAILURE_COUNT += 1
                _LAST_LOGIN_FAILURE_TIME = time.time()
                if _LOGIN_FAILURE_COUNT == 1 or _LOGIN_FAILURE_COUNT % 10 == 0:
                    logging.warning(
                        "[%s] %s 登录异常: %s (连续失败 %d 次, 进入 %d 秒冷却)",
                        log_tag, version_label, e, _LOGIN_FAILURE_COUNT, LOGIN_FAILURE_COOLDOWN,
                    )
                continue

        logging.warning("[%s] Nacos 登录失败: 所有路径均不可用 (连续失败 %d 次)", log_tag, _LOGIN_FAILURE_COUNT)
        return ""


# =============================================================================
# 提交限速器（防 1000+ Pod 瞬时涌入 Nacos）
# =============================================================================

class RateLimiter:
    """令牌桶限速器 — 控制 ThreadPoolExecutor 的任务提交速率。

    用法::

        limiter = RateLimiter(rate=200)   # 每秒最多提交 200 个任务
        for task in tasks:
            limiter.acquire()
            executor.submit(fn, task)

    原理：每次 acquire() 确保距上次调用至少间隔 1/rate 秒。
    """

    def __init__(self, rate: float):
        if rate <= 0:
            raise ValueError(f"rate 必须 > 0, 实际: {rate}")
        self._interval = 1.0 / rate
        self._last: float = 0.0

    def acquire(self) -> None:
        now = time.time()
        elapsed = now - self._last
        if elapsed < self._interval:
            time.sleep(self._interval - elapsed)
        self._last = time.time()
