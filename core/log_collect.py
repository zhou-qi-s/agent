"""
日志采集模块

读取 config.yaml 的 log_collect 配置，采集本地日志文件推送到 Loki。
支持：
- 多文件/通配符路径
- tail 方式增量读取（断点续传，重启后从上次位置继续）
- 批量推送（每 5 秒）
"""

import glob
import json
import logging
import os
import time
from typing import Dict, List

import requests

from utils.config_loader import load_config

logger = logging.getLogger(__name__)

_CONFIG = load_config()
_LOG_COLLECT = _CONFIG.get("log_collect", {})
_LOKI_URL = _LOG_COLLECT.get("loki_url", "")
_JOB = _LOG_COLLECT.get("job", "app")
_PATHS = _LOG_COLLECT.get("paths", [])
_POSITIONS_FILE = "/var/cache/agent/log_positions.json"

# 批量推送参数
_BATCH_INTERVAL = 5  # 每 5 秒推送一次


def _get_host():
    """获取本机 IP 作为 host 标签"""
    try:
        from utils.util import get_ip
        return get_ip()
    except Exception:
        return "unknown"


def _load_positions() -> Dict[str, int]:
    """加载已读位置（断点续传）"""
    try:
        if os.path.exists(_POSITIONS_FILE):
            with open(_POSITIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"加载日志位置文件失败: {e}")
    return {}


def _save_positions(positions: Dict[str, int]):
    """保存已读位置"""
    try:
        os.makedirs(os.path.dirname(_POSITIONS_FILE), exist_ok=True)
        with open(_POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(positions, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"保存日志位置文件失败: {e}")


def _resolve_paths() -> List[str]:
    """解析配置的日志路径（支持通配符）"""
    result = []
    for pattern in _PATHS:
        matched = glob.glob(pattern)
        if matched:
            result.extend(matched)
        else:
            # 路径不存在时也记录，等待文件出现
            result.append(pattern)
    return result


def _read_new_lines(path: str, position: int):
    """
    读取文件新增内容（tail 方式）
    返回 (新内容列表, 新位置)
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            # 文件被截断/轮转时，从 0 开始
            file_size = os.path.getsize(path)
            if position > file_size:
                position = 0
            f.seek(position)
            lines = f.readlines()
            new_position = f.tell()
            return lines, new_position
    except FileNotFoundError:
        return [], position
    except Exception as e:
        logger.warning(f"读取日志文件失败 {path}: {e}")
        return [], position


def _push_to_loki(streams: List[Dict]):
    """推送日志到 Loki"""
    if not _LOKI_URL:
        logger.warning("未配置 loki_url，跳过推送")
        return
    payload = {"streams": streams}
    try:
        resp = requests.post(
            _LOKI_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=5,
        )
        if resp.status_code != 204:
            logger.warning(f"Loki 推送失败: HTTP {resp.status_code}, {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Loki 推送异常: {e}")


def collect_and_push_once():
    """
    单轮采集：读取所有日志文件的新增内容，批量推送到 Loki
    """
    if not _LOG_COLLECT.get("enabled", False):
        return

    positions = _load_positions()
    host = _get_host()
    # 按文件分组，每个文件一个 stream
    stream_map = {}

    for path in _resolve_paths():
        pos = positions.get(path, 0)
        lines, new_pos = _read_new_lines(path, pos)
        if new_pos != pos:
            positions[path] = new_pos
        if not lines:
            continue

        stream = {
            "job": _JOB,
            "host": host,
            "filename": path,
        }
        key = json.dumps(stream, sort_keys=True)
        if key not in stream_map:
            stream_map[key] = {"stream": stream, "values": []}

        now_ns = str(int(time.time() * 1e9))
        for line in lines:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            stream_map[key]["values"].append([now_ns, line])

    if stream_map:
        _save_positions(positions)
        _push_to_loki(list(stream_map.values()))


def log_collect_loop(interval: int = 5):
    """
    日志采集循环线程
    """
    logger.info(f"启动日志采集线程（每 {interval} 秒）...")
    while True:
        try:
            collect_and_push_once()
        except Exception as e:
            logger.warning(f"日志采集异常: {e}")
        time.sleep(interval)