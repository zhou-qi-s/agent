"""
Promtail 日志采集模块

读取 config.yaml 的 log_collect 配置，生成 promtail 配置文件并启动 promtail 进程，
将本地应用日志推送到 Loki。

支持：
- 根据 log_collect.paths 生成 promtail scrape_configs
- 自动生成 promtail.yaml 配置文件
- 启动/停止 promtail 进程
"""

import logging
import os
import re
import signal
import subprocess
import time

import yaml

from utils.config_loader import load_config

logger = logging.getLogger(__name__)

_CONFIG = load_config()
_LOG_COLLECT = _CONFIG.get("log_collect", {})
_LOKI_URL = _LOG_COLLECT.get("loki_url", "")
_JOB = _LOG_COLLECT.get("job", "app")
_NAMESPACE = _LOG_COLLECT.get("namespace", "")
_PATHS = _LOG_COLLECT.get("paths", [])
_PROMTAIL_PATH = _LOG_COLLECT.get("promtail", {}).get("path", "/usr/local/bin/promtail")
_PROMTAIL_CONFIG = _LOG_COLLECT.get("promtail", {}).get("config", "/var/cache/agent/promtail.yaml")
_PROMTAIL_POSITIONS = _LOG_COLLECT.get("promtail", {}).get("positions", "/var/cache/agent/promtail-positions.yaml")

# promtail 进程引用
_promtail_proc = None


def _get_host():
    """获取本机 IP 作为 host 标签"""
    try:
        from utils.util import get_ip
        return get_ip()
    except Exception:
        return "unknown"


def _build_app_regex(path):
    """
    根据日志路径生成提取应用名的正则表达式

    提取路径前缀后的第一段（目录名或文件名去掉 .log 后缀）作为应用名：
    - /var/log/ruoyi.log      -> ruoyi
    - /var/log/harbor.log     -> harbor
    - /var/log/ruoyi/xxx.log  -> ruoyi（子目录，取第一级目录名）

    例如 /var/log/*.log -> /var/log/(?P<app_name>[^/]+?)(?:\.log)?(?:/.*)?$
    """
    # 取通配符前的目录前缀，如 /var/log/*.log -> /var/log/
    prefix = path.split("*")[0]
    if not prefix.endswith("/"):
        prefix = prefix.rsplit("/", 1)[0] + "/"
    return re.escape(prefix) + r"(?P<app_name>[^/]+?)(?:\.log)?(?:/.*)?$"


def generate_promtail_config():
    """
    根据 log_collect 配置生成 promtail.yaml 配置文件内容

    namespace 支持两种模式：
    - 固定值（如 "app"）：所有日志统一打该 namespace 标签
    - "{app}" 占位符：从日志文件名自动提取应用名作为 namespace（按应用区分）
      例如 /var/log/ruoyi.log -> namespace: ruoyi
           /var/log/harbor.log -> namespace: harbor
           /var/log/ruoyi/xxx.log -> namespace: ruoyi（子目录也支持）
    """
    host = _get_host()
    scrape_configs = []
    for path_idx, path in enumerate(_PATHS):
        labels = {
            "job": _JOB,
            "host": host,
            "__path__": path,
        }
        pipeline_stages = []
        # namespace 配置为 {app} 时，从日志文件名自动提取应用名作为 namespace
        if _NAMESPACE == "{app}":
            pipeline_stages = [
                {
                    "regex": {
                        "source": "filename",
                        "expression": _build_app_regex(path),
                    }
                },
                {
                    "labels": {
                        "namespace": "app_name",
                    }
                },
            ]
        elif _NAMESPACE:
            labels["namespace"] = _NAMESPACE

        # ⚠️ job_name 必须唯一：promtail 不允许两个 scrape_config 用同一个 job_name，
        #    否则启动即报 "found multiple scrape configs with job name ..." 并拒绝运行
        #    （而这里是【每个 path 生成一个 scrape_config】，所以 log_collect.paths 配多个时必然撞）。
        #    单路径时保持原名 "app-logs" 以兼容旧行为；多路径时加序号。
        #    注意：对外可见的 job 标签仍是 static_configs.labels.job（= log_collect.job），不受这里影响。
        scrape_config = {
            "job_name": "app-logs" if len(_PATHS) <= 1 else "app-logs-%d" % (path_idx + 1),
            "static_configs": [{
                "targets": ["localhost"],
                "labels": labels,
            }],
        }
        if pipeline_stages:
            scrape_config["pipeline_stages"] = pipeline_stages
        scrape_configs.append(scrape_config)

    config = {
        "server": {
            "http_listen_port": 9081,
            "grpc_listen_port": 0,
        },
        "positions": {
            "filename": _PROMTAIL_POSITIONS,
        },
        "clients": [{
            "url": _LOKI_URL,
        }],
        "scrape_configs": scrape_configs,
    }
    return yaml.dump(config, default_flow_style=False, allow_unicode=True)


def write_promtail_config():
    """
    生成并写入 promtail.yaml 配置文件
    """
    try:
        os.makedirs(os.path.dirname(_PROMTAIL_CONFIG), exist_ok=True)
        content = generate_promtail_config()
        with open(_PROMTAIL_CONFIG, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("promtail 配置文件已生成: %s", _PROMTAIL_CONFIG)
        return True
    except Exception as e:
        logger.error("生成 promtail 配置文件失败: %s", e)
        return False


def _find_running_promtail_pids():
    """
    扫描 /proc，找出「用本 Agent 的二进制/配置启动」的 promtail 进程 PID。

    为什么要自己扫 /proc：
        Agent 用 start_new_session=True 启动 promtail（独立会话/进程组），
        Agent 自身被重启（升级包、重新纳管）后旧 promtail 不会被带走 ——
        它会继续用同一份配置【重复推送同一批日志】，还会占着 9081 端口，
        导致新起的 promtail 绑不上端口而直接退出。

    只认 _PROMTAIL_CONFIG / _PROMTAIL_PATH，避免误杀平台「应用日志」功能部署的 promtail
    （那个用的是 {脚本目录}/{业务名}/promtail/ 下的二进制与 yaml）。
    """
    pids = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == os.getpid():
            continue
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                raw = f.read()
        except Exception:
            continue
        if not raw:
            continue
        parts = [p for p in raw.decode("utf-8", "replace").split("\0") if p]
        if not parts:
            continue
        # 只看 promtail 进程本身
        if "promtail" not in os.path.basename(parts[0]):
            continue
        if _PROMTAIL_CONFIG in " ".join(parts) or parts[0] == _PROMTAIL_PATH:
            pids.append(pid)
    return pids


def cleanup_stale_promtail():
    """
    清理残留的旧 promtail 进程（返回清理个数）。

    场景：重新纳管 / Agent 升级后，上一代 Agent 拉起的 promtail 既不会被带走、
    也不会被 _promtail_proc 引用到 —— 不清理就会越积越多（同一份日志被重复推送多次，
    且新进程可能因 9081 端口被占而起不来）。
    """
    global _promtail_proc
    try:
        pids = _find_running_promtail_pids()
    except Exception as e:
        logger.warning("扫描残留 promtail 失败: %s", e)
        return 0

    # 当前 Agent 自己拉起、且还活着的那一个不算残留
    mine = _promtail_proc.pid if (_promtail_proc and _promtail_proc.poll() is None) else None
    killed = 0
    for pid in pids:
        if pid == mine:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("清理残留 promtail 进程 PID=%s（旧实例，重复推送源）", pid)
            killed += 1
        except Exception as e:
            logger.warning("清理 promtail PID=%s 失败: %s", pid, e)
    if killed:
        # 等端口(9081)与 positions 文件句柄释放，否则新进程可能绑定失败直接退出
        time.sleep(2)
    return killed


def start_promtail():
    """
    启动 promtail 进程（如果已运行则跳过）
    """
    global _promtail_proc

    if not _LOG_COLLECT.get("enabled", False):
        logger.info("log_collect 未启用，跳过 promtail 启动")
        return False

    if not os.path.exists(_PROMTAIL_PATH):
        logger.error("promtail 可执行文件不存在: %s", _PROMTAIL_PATH)
        return False

    # 如果已运行则跳过
    if _promtail_proc and _promtail_proc.poll() is None:
        logger.info("promtail 已在运行，跳过启动")
        return True

    # ⚠️ 启动前先清理上一次 Agent 运行留下的 promtail：
    #    Agent 重启不会带走独立会话里的子进程，不清理会重复推送同一批日志、并占用 9081 端口
    cleanup_stale_promtail()

    # 生成配置文件
    if not write_promtail_config():
        return False

    try:
        _promtail_proc = subprocess.Popen(
            [_PROMTAIL_PATH, "-config.file=" + _PROMTAIL_CONFIG],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("promtail 已启动, PID=%s, 配置=%s", _promtail_proc.pid, _PROMTAIL_CONFIG)
        return True
    except Exception as e:
        logger.error("启动 promtail 失败: %s", e)
        return False


def stop_promtail():
    """
    停止 promtail 进程
    """
    global _promtail_proc
    if _promtail_proc and _promtail_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_promtail_proc.pid), signal.SIGTERM)
            _promtail_proc.wait(timeout=5)
            logger.info("promtail 已停止")
        except Exception as e:
            logger.warning("停止 promtail 异常: %s", e)
    _promtail_proc = None
    # 顺带清掉上一次 Agent 运行留下的 promtail —— 否则「取消纳管」后它仍会继续往 Loki 推日志
    cleanup_stale_promtail()


def promtail_loop(interval: int = 30):
    """
    promtail 守护循环：定期检查 promtail 是否存活，异常时自动重启
    """
    logger.info("启动 promtail 守护线程（每 %s 秒检查）...", interval)
    while True:
        try:
            if _LOG_COLLECT.get("enabled", False):
                if _promtail_proc is None or _promtail_proc.poll() is not None:
                    logger.warning("promtail 未运行，尝试重启...")
                    start_promtail()
        except Exception as e:
            logger.warning("promtail 守护检查异常: %s", e)
        time.sleep(interval)