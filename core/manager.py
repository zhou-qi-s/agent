import json
import logging
import time

import psutil
import requests

import utils.util
from utils.config_loader import load_config
from utils.logger import logger
from utils.redis_store import RedisStore

redisUtils = RedisStore()

CONFIG = load_config("config.yaml")

server = CONFIG['server']
interface = CONFIG['interface']
upload_interval = CONFIG.get('upload_interval', 10)  # 默认10秒

# ================== 组装URL ==================
# interface.process/version 可能未在 config.yaml 中配置，提供兜底路径
_process_api = interface.get('process') or '/api/agent/process'
_version_api = interface.get('version') or '/api/agent/process/version'
PROCESS_URL = f"{server['iP']}:{server['port']}{_process_api}"
VERSION_URL = f"{server['iP']}:{server['port']}{_version_api}"
key_head = "agent:process:"
process_data = {}
version = int(time.time())


# ================== 1. 获取被管理进程 ==================
def get_processes():
    global process_data
    try:
        ip = utils.util.get_ip()
    except Exception as e:
        logging.error(f"get_ip error: {e}")
        return None

    if not process_data:
        parameter = {
            "username": utils.util.username,
            "ip": ip
        }
        try:
            resp = requests.post(PROCESS_URL, json=parameter, timeout=5)
            if resp.status_code != 200:
                logging.warning(f"Process list request failed: {resp.status_code}")
                return None
            process_data = json.loads(resp.text)
        except Exception as e:
            logging.error(f"requests.post PROCESS_URL error: {e}")
            return None

    return process_data


def update_process_list():
    global version
    global process_data
    try:
        parameter = {
            "username": utils.util.username,
            "ip": utils.util.get_ip()
        }
        resp = requests.post(VERSION_URL, json=parameter, timeout=5)
        if resp.status_code == 200:
            new_version = int(resp.text)
            if new_version != version:
                version = new_version
                resp = requests.post(PROCESS_URL, json=parameter, timeout=5)
                if resp.status_code == 200:
                    process_data = json.loads(resp.text)
    except Exception as e:
        logging.error(f"update_process_list error: {e}")


# ================== 2. 采集进程资源 ==================
def collect_process_info(pid_list):
    """
    仅采集指定 PID 列表的进程信息（CPU、内存、IO、线程），不采集 GPU。
    返回一个字典，key 是 PID，value 是进程信息或错误。
    """
    process_results = {}

    for pid in pid_list:
        try:
            p = psutil.Process(pid)
            # 先调用一次 cpu_percent 获取基线，再间隔 0.1 秒重新采样
            p.cpu_percent(interval=None)
            time.sleep(0.1)
            process_results[pid] = {
                "name": p.name(),
                "status": p.status(),
                "cpu_percent": p.cpu_percent(interval=None),
                "memory_info": p.memory_info()._asdict(),
                "io_counters": p.io_counters()._asdict() if p.io_counters() else {},
                "threads": p.num_threads()
            }
        except psutil.NoSuchProcess:
            process_results[pid] = {"error": "No such process"}
        except Exception as e:
            process_results[pid] = {"error": str(e)}

    return process_results


# ================== 3. 上报 ==================
def report():
    try:
        # 1 获取管理进程
        plist = get_processes()
        if not plist:
            logging.info("没有需要监控的进程")
            return

        # 2 提取 PID 并去重
        pid_list = list({item['pid'] for item in plist if 'pid' in item})
        if not pid_list:
            logging.info("进程列表为空")
            return

        # 3 采集进程信息
        results = collect_process_info(pid_list)
        if not results:
            logging.info("没有采集到进程信息")
            return

        # 4 获取主机信息
        username = utils.util.username
        ip = utils.util.get_ip()

        # 5 上报到 Redis（保持 JSON 数组，最多 15 条）
        max_length = 15
        for pid, data in results.items():
            key = f"{key_head}{pid}:{ip}:{username}"
            try:
                # 读取已有数据
                existing = redisUtils.get(key)
                arr = json.loads(existing) if existing else []

                # 插入新数据到开头
                arr.insert(0, {
                    "pid": pid,
                    "ip": ip,
                    "username": username,
                    "time": int(time.time()),
                    "data": data
                })

                # 保持长度不超过 max_length
                if len(arr) > max_length:
                    arr = arr[:max_length]

                # 写回 Redis
                redisUtils.set(key, json.dumps(arr))

                logging.info(f"上报进程 pid={pid}, total_records={len(arr)}")

            except Exception as e:
                logging.error(f"redis push error pid={pid} {e}")

    except Exception as e:
        logging.error(f"report error: {e}")


def report_loop():
    interval = upload_interval
    while True:
        try:
            report()
        except Exception as e:
            logger.error(f"进程上报错误: {e}")
        time.sleep(interval)