import getpass
import platform
import socket
import subprocess
import time
from datetime import datetime

import psutil

from utils.config_loader import load_config
from utils.logger import logger
from utils.redis_store import redisUtils
from utils.util import get_local_ip

# ================== 读取配置 ==================
CONFIG = load_config("config.yaml")

agent_cfg = CONFIG.get('agent', {})

HB_INTERVAL = agent_cfg.get('heartbeat', {}).get('interval', 5)  # 心跳间隔（秒）
VERSION = CONFIG.get('version', '1.0.0')
agent = "agent:info:"


# ================== 心跳线程函数 ==================
def heartbeat_loop(agent_id):
    logger.info("心跳线程启动成功")

    username = getpass.getuser()
    hostname = socket.gethostname()
    ip = "unknown"
    timer = 0
    key = f"{agent}{agent_id}"
    while True:
        timer = timer % 3
        # 只在需要时刷新 IP（优先使用 config.yaml 指定网卡，避免取到 127.0.1.1）
        if ip == "unknown":
            try:
                ip = get_local_ip()
                logger.info(f"IP 获取成功：{ip}")
            except Exception as e:
                logger.error(f"IP 获取失败：{e}")
        try:
            data = {}
            if redisUtils.exists(key):
                data = redisUtils.get(key)
                if timer == 0:
                    data["host_info"] = get_host_info()
                    old_ip = ip
                    try:
                        ip = get_local_ip()
                    except Exception as e:
                        ip = old_ip
                # 始终用最新获取的 IP 覆盖旧值，避免历史缓存(如 127.0.1.1)被持续上报
                data["ip"] = ip
                data["agent_id"] = agent_id
                data["update_time"] = int(time.time())
                data["status"] = 1
                data["username"] = username
                data["version"] = VERSION
            else:
                data = {
                    "agent_id": agent_id,
                    "update_time": int(time.time()),
                    "status": 1,
                    "ip": ip,
                    "username": username,
                    "version": VERSION
                }
            redisUtils.set(key, data, ttl=60)
        except Exception as e:
            logger.error(f"心跳失败: {e}")
        timer += 1
        time.sleep(HB_INTERVAL)


# ================== 获取 GPU 信息（兼容 NVIDIA / AMD / Intel） ==================
def get_gpu_info():
    """
    兼容获取 GPU 信息，支持以下厂商：
    - NVIDIA  （通过 nvidia-smi / GPUtil）
    - AMD     （通过 rocm-smi / pyamdgpuinfo）
    - Intel   （通过 intel_gpu_top / lspci）
    如果没有任何 GPU 或获取失败，返回对应提示。
    """
    gpu_list = []

    # ---------- NVIDIA ----------
    nvidia_gpus = _get_nvidia_gpus()
    if nvidia_gpus is not None:
        gpu_list.extend(nvidia_gpus)

    # ---------- AMD ----------
    amd_gpus = _get_amd_gpus()
    if amd_gpus is not None:
        gpu_list.extend(amd_gpus)

    # ---------- Intel ----------
    intel_gpus = _get_intel_gpus()
    if intel_gpus is not None:
        gpu_list.extend(intel_gpus)

    # 如果三种方式都没有检测到，返回未检测到信息
    if not gpu_list:
        return [{'status': 'not_detected', 'message': '未检测到 GPU 或无法获取 GPU 信息'}]

    return gpu_list


def _get_nvidia_gpus():
    """通过 nvidia-smi 获取 NVIDIA GPU 信息，失败时回退到 GPUtil"""
    try:
        result = subprocess.run(
            ['nvidia-smi',
             '--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_list = []
            for line in result.stdout.strip().split('\n'):
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 7:
                    gpu_list.append({
                        'vendor': 'NVIDIA',
                        'id': int(parts[0]),
                        'name': parts[1],
                        'total_memory': float(parts[2]),  # MB
                        'used_memory': float(parts[3]),   # MB
                        'free_memory': float(parts[4]),   # MB
                        'utilization': float(parts[5]) / 100,  # 转为 0~1
                        'temperature': float(parts[6])    # 摄氏度
                    })
            return gpu_list
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # 回退：尝试 GPUtil
    try:
        import GPUtil
        gpus = GPUtil.getGPUs()
        if gpus:
            gpu_list = []
            for gpu in gpus:
                gpu_list.append({
                    'vendor': 'NVIDIA',
                    'id': gpu.id,
                    'name': gpu.name,
                    'total_memory': gpu.memoryTotal,
                    'used_memory': gpu.memoryUsed,
                    'free_memory': gpu.memoryFree,
                    'utilization': gpu.load,
                    'temperature': gpu.temperature
                })
            return gpu_list
    except ImportError:
        pass
    except Exception:
        pass

    return None  # 表示没有 NVIDIA GPU


def _get_amd_gpus():
    """通过 rocm-smi 获取 AMD GPU 信息"""
    try:
        result = subprocess.run(
            ['rocm-smi', '--showallinfo', '--json'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            import json
            data = json.loads(result.stdout)
            gpu_list = []
            # rocm-smi JSON 格式：card#N 结构
            for card_key, card_data in data.items():
                if not card_key.startswith('card'):
                    continue
                try:
                    vram_info = card_data.get('VRAM', {})
                    total_mem = float(vram_info.get('VRAM Total Memory (B)', 0)) / (1024 * 1024)  # 转为 MB
                    used_mem = float(vram_info.get('VRAM Total Used Memory (B)', 0)) / (1024 * 1024)
                    gpu_list.append({
                        'vendor': 'AMD',
                        'id': card_key.replace('card', ''),
                        'name': card_data.get('GPU ID', 'Unknown AMD GPU'),
                        'total_memory': round(total_mem, 1),
                        'used_memory': round(used_mem, 1),
                        'free_memory': round(total_mem - used_mem, 1),
                        'utilization': float(card_data.get('GPU use (%)', {}).get('GPU use', 0)) / 100 if isinstance(card_data.get('GPU use (%)'), dict) else 0,
                        'temperature': float(card_data.get('Temperature', {}).get('Sensor Edge (Temp)', 0)) if isinstance(card_data.get('Temperature'), dict) else 0
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            return gpu_list if gpu_list else None
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # 尝试通过 lspci 检测 AMD GPU（Linux）
    try:
        result = subprocess.run(
            ['lspci'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            amd_lines = [l for l in result.stdout.strip().split('\n') if 'AMD' in l.upper() and 'VGA' in l.upper()]
            if amd_lines:
                gpu_list = []
                for i, line in enumerate(amd_lines):
                    name = line.split(': ')[-1] if ': ' in line else f'AMD GPU {i}'
                    gpu_list.append({
                        'vendor': 'AMD',
                        'id': str(i),
                        'name': name,
                        'total_memory': 0,
                        'used_memory': 0,
                        'free_memory': 0,
                        'utilization': 0,
                        'temperature': 0,
                        'note': '仅检测到设备，详细信息需要安装 rocm-smi'
                    })
                return gpu_list
    except FileNotFoundError:
        pass
    except Exception:
        pass

    return None


def _get_intel_gpus():
    """通过 intel_gpu_top 或 lspci 检测 Intel GPU"""
    # 尝试通过 intel_gpu_top 获取信息
    try:
        result = subprocess.run(
            ['intel_gpu_top', '-l', '-s', '500'],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            gpu_list = [{
                'vendor': 'Intel',
                'id': '0',
                'name': 'Intel Integrated GPU',
                'total_memory': 0,
                'used_memory': 0,
                'free_memory': 0,
                'utilization': 0,
                'temperature': 0,
                'note': 'Intel GPU 已检测到，详细信息请使用 intel_gpu_monitor'
            }]
            return gpu_list
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # 尝试通过 lspci 检测 Intel GPU（Linux）
    try:
        result = subprocess.run(
            ['lspci'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            intel_lines = [l for l in result.stdout.strip().split('\n') if 'INTEL' in l.upper() and 'VGA' in l.upper()]
            if intel_lines:
                gpu_list = []
                for i, line in enumerate(intel_lines):
                    name = line.split(': ')[-1] if ': ' in line else f'Intel GPU {i}'
                    gpu_list.append({
                        'vendor': 'Intel',
                        'id': str(i),
                        'name': name,
                        'total_memory': 0,
                        'used_memory': 0,
                        'free_memory': 0,
                        'utilization': 0,
                        'temperature': 0,
                        'note': '仅检测到设备，详细信息需要安装 intel-gpu-tools'
                    })
                return gpu_list
    except FileNotFoundError:
        pass
    except Exception:
        pass

    # Windows 下尝试通过 WMIC 检测 Intel GPU
    if platform.system() == 'Windows':
        try:
            result = subprocess.run(
                ['wmic', 'path', 'win32_VideoController', 'get', 'Name,AdapterRAM,DriverVersion', '/format:csv'],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                gpu_list = []
                for line in result.stdout.strip().split('\n'):
                    line = line.strip()
                    if not line or 'Name' in line:
                        continue
                    parts = [p.strip() for p in line.split(',') if p.strip()]
                    if len(parts) >= 2:
                        gpu_name = parts[-2] if len(parts) >= 2 else 'Unknown'
                        if 'Intel' in gpu_name:
                            adapter_ram = 0
                            try:
                                adapter_ram = int(parts[-1]) / (1024 * 1024)  # 转为 MB
                            except (ValueError, IndexError):
                                pass
                            gpu_list.append({
                                'vendor': 'Intel',
                                'id': str(len(gpu_list)),
                                'name': gpu_name,
                                'total_memory': round(adapter_ram, 1),
                                'used_memory': 0,
                                'free_memory': round(adapter_ram, 1),
                                'utilization': 0,
                                'temperature': 0,
                                'note': 'Intel GPU 使用率/温度信息需要安装 Intel GPU Tools'
                            })
                return gpu_list if gpu_list else None
        except FileNotFoundError:
            pass
        except Exception:
            pass

    return None


# ================== 获取主机信息 ==================
def get_host_info():
    info = {}

    # ================= CPU =================
    info['cpu'] = {
        'physical_cores': psutil.cpu_count(logical=False),  # 物理核心数
        'total_cores': psutil.cpu_count(logical=True),  # 逻辑核心数（超线程后）
        'usage_percent_per_core': psutil.cpu_percent(percpu=True),  # 每个核心的使用率 %
        'total_usage_percent': psutil.cpu_percent()  # CPU 总使用率 %
    }

    # ================= 内存 =================
    mem = psutil.virtual_memory()
    info['memory'] = {
        'total': mem.total,  # 内存总量（字节）
        'available': mem.available,  # 可用内存（字节）
        'used': mem.used,  # 已用内存（字节）
        'percent': mem.percent  # 内存使用率 %
    }

    # ================= 存储 =================
    disk = psutil.disk_usage('/')
    info['disk'] = {
        'total': disk.total,  # 总容量（字节）
        'used': disk.used,  # 已用（字节）
        'free': disk.free,  # 空闲（字节）
        'percent': disk.percent  # 使用率 %
    }

    # ================= 网络 =================
    net = psutil.net_io_counters()
    info['network'] = {
        'bytes_sent': net.bytes_sent,  # 发送字节数
        'bytes_recv': net.bytes_recv,  # 接收字节数
        'packets_sent': net.packets_sent,  # 发送数据包数量
        'packets_recv': net.packets_recv  # 接收数据包数量
    }

    # ================= GPU =================
    info['gpu'] = get_gpu_info()

    # ================= 系统信息 =================
    info['system'] = {
        'hostname': platform.node(),  # 主机名
        'os': platform.system(),  # 系统名称，如 Windows、Linux
        'os_version': platform.version(),  # 系统版本
        'platform': platform.platform(),  # 系统平台信息
        'boot_time': datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M:%S"),  # 开机时间
        'process_count': len(list(psutil.process_iter()))  # 当前运行进程数量
    }

    return info
