"""
core/task_utils.py - 任务执行核心模块

本模块是Agent的任务处理核心，负责任务的读取、执行和管理。
主要功能包括：
1. 从Redis队列读取任务
2. 执行各类任务（下载、安装、启动、停止、卸载、命令执行）
3. Nacos服务注册与心跳管理
4. 任务结果上报

任务类型：
- download: 下载文件
- install: 安装服务
- start: 启动服务（含Nacos注册）
- stop: 停止服务（含Nacos注销）
- uninstall: 卸载服务
- execute_command: 执行命令
- container_start: 从 Harbor 拉取 Helm Chart 并部署到 k8s/k3s 集群

Nacos心跳管理：
本模块实现了统一的心跳管理机制，通过 _start_nacos_heartbeats 函数
为服务实例维护与Nacos的心跳连接，确保服务注册信息不过期。
"""

import json
import logging
import subprocess
import threading
import time
import traceback
from datetime import datetime
from typing import Any, cast

import requests

import utils.util
from core.executor.shell import Shell
from core.task_utils.rollback import rollback_task
from core.utils import normalize_task_result
from utils.config_loader import load_config
from utils.redis_store import RedisStore
from core.task_utils.download import download_task
from core.task_utils.install import install_task
from core.task_utils.xkt_download import xkt_download_task
from core.task_utils.xkt_Install import xkt_install_task
from core.task_utils.xkt_start import xkt_start_task
from core.task_utils.xkt_stop import xkt_stop_task
from core.task_utils.xkt_upgrade import xkt_upgrade_task
from core.task_utils.xkt_rollback import xkt_rollback_task
from core.task_utils.xkt_uninstall import xkt_uninstall_task
from core.task_utils.plugin_download import plugin_download_task
from core.task_utils.plugin_install import plugin_install_task
from core.task_utils.plugin_start import plugin_start_task
from core.task_utils.plugin_stop import plugin_stop_task
from core.task_utils.plugin_upgrade import plugin_upgrade_task
from core.task_utils.plugin_rollback import plugin_rollback_task
from core.task_utils.plugin_uninstall import plugin_uninstall_task
from core.task_utils.start import start_task
from core.task_utils.stop import stop_task
from core.task_utils.uninstall import uninstall_task
from core.task_utils.upgrade import upgrade_task
from core.task_utils.containerStart import container_start_task
from core.task_utils.containerStop import container_stop_task
from core.task_utils.containerRestart import container_restart_task


# =============================================================================
# 全局变量与配置
# =============================================================================

# Redis连接工具
redisUtils = RedisStore()

# 任务队列Redis键前缀，格式: agent:task_utils:queue:{ip}
TASK_QUEUE_KEY = "agent:task:queue:"

# 加载配置文件
config = load_config()

# 任务上报接口路径
task_report = config['interface']['task_report']


# =============================================================================
# 任务读取与上报
# =============================================================================

def read_task(timeout=0):
    """
    从Redis阻塞队列读取任务
    
    使用Redis的BRPOP命令阻塞等待任务，超时后返回None
    
    参数:
        timeout: 阻塞超时时间（秒），0表示无限等待
    
    返回:
        dict: 任务字典，超时或出错返回None
    """
    try:
        # 获取本机IP作为队列标识
        ip = utils.util.get_ip()
        result = cast(Any, redisUtils.redis.brpop([TASK_QUEUE_KEY + ip], timeout=timeout))

        if not result:
            return None

        _, task_str = result

        # 处理字节类型
        if isinstance(task_str, bytes):
            task_str = task_str.decode("utf-8")

        task = json.loads(task_str)
        task_id = task.get("task_id", "unknown")
        logging.info(f"[任务管理] 收到任务: {task_id}")

        return task

    except json.JSONDecodeError as e:
        logging.error(f"[任务管理] JSON解析失败: {e}")
    except Exception as e:
        logging.error(f"[任务管理] 读取任务异常: {e}")

    return None


def upload_task_result(result):
    """
    上传任务执行结果到服务器
    
    通过HTTP POST将任务结果上报到配置的任务上报接口
    
    参数:
        result: 任务执行结果字典
    """
    try:
        server_ip = config['server']['iP']
        server_port = config['server']['port']
        upload_url = f"{server_ip}:{server_port}{task_report}"

        payload = dict(result)
        payload['timestamp'] = datetime.now().isoformat()

        response = requests.post(upload_url, json=payload, timeout=30)

        if response.status_code == 200:
            logging.info(f"[任务管理] 任务结果上传成功: {payload.get('task_type', 'unknown')}")
        else:
            logging.error(f"[任务管理] 任务结果上传失败: {response.status_code}, {response.text}")

    except Exception as e:
        logging.error(f"[任务管理] 上传任务结果异常: {e}")


# =============================================================================
# 任务执行函数
# =============================================================================

def execute_command_task(parameters, retry=0, timeout=300):
    """
    执行命令任务
    
    执行系统命令并返回结果
    
    参数:
        parameters: 参数字典，包含 'command'
        retry: 重试次数（当前未使用）
        timeout: 命令执行超时（秒）
    
    返回:
        tuple: (success, extra_data)
            - success: 命令是否成功执行（returncode == 0）
            - extra_data: 包含command、return_code、output、error的字典
    """
    command = parameters.get('command', '')
    logging.info(f"[任务管理] 执行命令: {command}, 重试: {retry}, 超时: {timeout}秒")

    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)

        extra_data = {
            "command": command,
            "return_code": result.returncode,
            "output": result.stdout,
            "error": result.stderr
        }

        return result.returncode == 0, extra_data
    except Exception as e:
        return False, {"error": str(e), "command": command}


# =============================================================================
# 任务处理器注册与主循环
# =============================================================================

# 任务类型 -> 所需节点类型（None 表示所有节点均可处理）
# 显控台任务（xkt_*）仅在 DISPLAY_CONSOLE 类型节点上处理
# 插件任务（plugin_*）仅在 PLUGIN 类型节点上处理
TASK_TYPE_REQUIREMENT = {
    "xkt_download": "DISPLAY_CONSOLE",
    "xkt_install": "DISPLAY_CONSOLE",
    "xkt_start": "DISPLAY_CONSOLE",
    "xkt_stop": "DISPLAY_CONSOLE",
    "xkt_upgrade": "DISPLAY_CONSOLE",
    "xkt_rollback": "DISPLAY_CONSOLE",
    "xkt_uninstall": "DISPLAY_CONSOLE",
    "plugin_download": "PLUGIN",
    "plugin_install": "PLUGIN",
    "plugin_start": "PLUGIN",
    "plugin_stop": "PLUGIN",
    "plugin_upgrade": "PLUGIN",
    "plugin_rollback": "PLUGIN",
    "plugin_uninstall": "PLUGIN",
}


def kill_script_task(parameters, retry=0, timeout=30):
    """
    控制类任务：强制结束某个「挂起中」任务的脚本（平台「强制结束」按钮下发）。

    与业务任务的本质区别：
        · 它不是业务动作，**不上报 task_report**（平台侧也不建任务记录，
          否则任务表里会多出一条没有意义的管理记录）；
        · 被杀脚本的终态由原任务的观察线程补报（失败），任务状态照常闭环。

    参数:
        parameters: 必须包含 target_task_id（要结束的原任务 ID）
    """
    target = str(parameters.get("target_task_id") or "").strip()
    if not target:
        return False, {"error_type": "BadParameter", "error_message": "缺少 target_task_id"}

    info = Shell.kill_hanging(target, reason="平台强制结束")
    logging.warning("[任务管理] 强制结束挂起脚本: target=%s found=%s killed=%s",
                    target, info.get("found"), info.get("killed"))
    return bool(info.get("found")), info


# 任务处理器注册表：任务类型 -> 处理函数
TASK_HANDLERS = {
    "download": download_task,
    "install": install_task,
    "start": start_task,
    "stop": stop_task,
    "execute_command": execute_command_task,
    "uninstall": uninstall_task,
    "upgrade": upgrade_task,
    "xkt_download": xkt_download_task,
    "xkt_install": xkt_install_task,
    "xkt_start": xkt_start_task,
    "xkt_stop": xkt_stop_task,
    "xkt_upgrade": xkt_upgrade_task,
    "xkt_rollback": xkt_rollback_task,
    "xkt_uninstall": xkt_uninstall_task,
    "plugin_download": plugin_download_task,
    "plugin_install": plugin_install_task,
    "plugin_start": plugin_start_task,
    "plugin_stop": plugin_stop_task,
    "plugin_upgrade": plugin_upgrade_task,
    "plugin_rollback": plugin_rollback_task,
    "plugin_uninstall": plugin_uninstall_task,
    "rollback": rollback_task,
    "container_start": container_start_task,
    "container_stop": container_stop_task,
    "container_restart": container_restart_task,
    "kill_script": kill_script_task,
}


def handle_task(task, enabled_types=None):
    """
    处理单个任务
    
    流程：
        1. 解析任务参数（task_id, task_type, parameters, retry, timeout）
        2. 校验节点类型是否支持该任务（显控台/插件任务按节点类型过滤）
        3. 查找对应的任务处理器
        4. 执行任务
        5. 规范化结果
        6. 上报结果
    
    参数:
        task: 任务字典
        enabled_types: 节点启用的类型集合（如 {"VIRTUAL_MACHINE", "DISPLAY_CONSOLE"}），
                       为 None 时不做类型过滤（兼容旧调用）
    
    返回:
        dict: 任务执行结果
    """
    if not task:
        logging.warning("[任务管理] 收到空任务，跳过处理")
        return

    try:
        task_id = task.get('task_id', 'unknown')
        task_type = task.get('task_type', 'unknown')
        parameters = dict(task.get('parameters', {}))
        parameters['task_id'] = task_id

        # 节点类型过滤：显控台任务需 DISPLAY_CONSOLE，插件任务需 PLUGIN
        if enabled_types is not None:
            required_type = TASK_TYPE_REQUIREMENT.get(task_type)
            if required_type and required_type not in enabled_types:
                agent_ip = utils.util.get_ip() or "unknown"
                logging.warning(
                    f"[任务管理] 任务 {task_id} 类型 {task_type} 需要节点类型 {required_type}，"
                    f"当前节点未启用，跳过处理。启用的类型: {enabled_types}")
                result = {
                    "ip": agent_ip,
                    "task_id": task_id,
                    "result": False,
                    "status": 4,
                    "task_type": task_type,
                    "message": f"节点未启用 {required_type} 类型，无法处理 {task_type} 任务",
                    "data": {
                        "error_type": "NodeTypeNotSupported",
                        "error_message": f"节点未启用 {required_type} 类型，无法处理 {task_type} 任务",
                        "traceback": ""
                    }
                }
                upload_task_result(result)
                return result

        # 解析重试次数
        raw_retry = task.get('retry', 1)
        try:
            retry = int(raw_retry)
        except (TypeError, ValueError):
            logging.warning(f"[任务管理] retry 参数无效，使用默认值 1: {raw_retry}")
            retry = 1

        # 解析超时时间
        raw_timeout = task.get('timeout', 10000)
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            logging.warning(f"[任务管理] timeout 参数无效，使用默认值 10000: {raw_timeout}")
            timeout = 10000

        # ★单位归一化：平台下发的是**毫秒**（如 90000），Agent 内部按**秒**使用。
        # 不换算会把脚本超时放大 1000 倍（90000 秒 ≈ 25 小时），后果是：
        # 脚本写错/挂住时任务线程一直阻塞在 communicate()，既不进入「挂起观察」，
        # 也消费不到后续任务（包括平台的「强制结束」指令）。
        # 约定：> 3600 的值一律按毫秒换算；正常的秒级超时（≤3600）不受影响。
        if timeout > 3600:
            timeout = max(1, timeout // 1000)

        created_at = task.get('created_at', datetime.now())
        agent_ip = utils.util.get_ip() or "unknown"

        logging.info(
            f"[任务管理] 开始处理任务: {task_id}, 类型: {task_type}, 重试: {retry}, 超时: {timeout}秒, 创建时间: {created_at}")

        # ── 控制类任务：强制结束挂起脚本 ──
        # 静默执行、不上报（平台不建任务记录）；被杀脚本的终态由原任务观察线程补报。
        if task_type == "kill_script":
            ok, info = kill_script_task(parameters, retry=retry, timeout=timeout)
            logging.warning("[任务管理] 控制指令 kill_script 执行完毕: ok=%s target=%s",
                            ok, parameters.get("target_task_id"))
            return {"control": True, "ok": ok, "detail": info}

        # 查找任务处理器
        handler = TASK_HANDLERS.get(task_type)
        if handler is None:
            result = {
                "ip": agent_ip,
                "task_id": task_id,
                "result": False,
                "status": 4,
                "task_type": task_type,
                "message": f"未知任务类型: {task_type}",
                "data": {
                    "error_type": "UnknownTaskType",
                    "error_message": f"不支持的任务类型: {task_type}",
                    "traceback": ""
                }
            }
        else:
            result = normalize_task_result(handler(parameters, retry=retry, timeout=timeout), task_id, task_type, agent_ip)

        # ── 挂起脚本处理（信任脚本模式）──
        # install/start 等脚本可能在前台常驻（脚本不退出 = 应用在跑）。
        # Shell 层超时不杀，只登记挂起；这里把任务标记为「执行中」上报，
        # 并起观察线程等脚本真正退出后再补报终态（成功/失败）。
        hanging = Shell.pop_hanging(task_id)
        if hanging:
            proc = hanging["popen"]
            elapsed_start = hanging.get("started", 0)
            # ★放回登记表：观察期间平台可以按 task_id 下发「强制结束」，
            # 观察线程补报终态后再清理（见 _watch_hanging_script 末尾 drop_hanging）。
            Shell.restore_hanging(task_id, hanging)
            result["result"] = 2  # 2 = 执行中（平台 resolveTaskStatus 据此置 PENDING）
            result["message"] = (result.get("message") or "") + "（脚本仍在执行中，已转后台观察）"
            # ★挂起时不能带实例状态码：平台 taskReport 会照抄 payload 的 status 写实例，
            # 带着会把手头任务的成功/失败码（如 8/9）提前写进实例。挂起期间实例状态不动，
            # 等 watcher 补报终态时再更新。
            result.pop("status", None)
            result.setdefault("data", {})
            result["data"]["script_still_running"] = True
            result["data"]["script_path"] = hanging.get("script_path", "")
            result["data"]["script_started_at"] = elapsed_start

            max_hang = int(hanging.get("max_hang") or 1800)

            def _watch_hanging_script():
                """等挂起脚本真正退出，补报终态（平台外启动/脚本自治不在此列——只补报本任务）"""
                out = err = ""
                timeout_hit = False
                try:
                    try:
                        out, err = proc.communicate(timeout=max_hang)
                    except subprocess.TimeoutExpired:
                        # ★挂起上限兜底：脚本写错（死循环/卡住）不能永远挂着
                        timeout_hit = True
                        Shell.kill_hanging(task_id, reason=f"挂起超过上限 {max_hang}s，自动结束")
                        try:
                            out, err = proc.communicate(timeout=10)
                        except Exception:
                            pass
                    code = proc.returncode
                    ok = (code == 0) and not timeout_hit
                    final = dict(result)
                    final["result"] = 1 if ok else 0
                    if timeout_hit:
                        final["message"] = f"脚本挂起超过 {max_hang}s 上限，已自动强制结束"
                    else:
                        final["message"] = (
                            f"挂起脚本执行完成（{int(time.time() - elapsed_start)}s）: "
                            + ("成功" if ok else f"退出码 {code}")
                        )
                    final["data"] = dict(result.get("data") or {})
                    final["data"]["script_still_running"] = False
                    final["data"]["hang_timeout"] = timeout_hit
                    final["data"]["stdout"] = (out or "").strip()
                    final["data"]["stderr"] = (err or "").strip()
                    final["data"]["exit_code"] = code
                    upload_task_result(final)
                    logging.info(f"[任务管理] 挂起脚本终态已补报: {task_id} code={code} timeout={timeout_hit}")
                except Exception as e:
                    logging.error(f"[任务管理] 挂起脚本观察异常: {task_id} -> {e}")
                finally:
                    Shell.drop_hanging(task_id)

            threading.Thread(target=_watch_hanging_script, daemon=True).start()
            logging.info(f"[任务管理] 任务 {task_id} 脚本挂起执行中，观察线程已启动")

        # 上报结果
        upload_task_result(result)
        return result

    except Exception as e:
        logging.error(f"[任务管理] 处理任务失败: {e}")

        error_result = {
            "ip": utils.util.get_ip() or "unknown",
            "result": False,
            "status": 4,
            "task_type": task.get("task_type", "unknown"),
            "task_id": task.get("task_id", "unknown"),
            "message": "任务执行异常",
            "data": {
                "error_type": type(e).__name__,
                "error_message": str(e),
                "traceback": traceback.format_exc()
            }
        }
        upload_task_result(error_result)
        return error_result


def task_loop(timeout=0, enabled_types=None):
    """
    任务主循环
    
    持续从Redis队列读取任务并处理
    
    参数:
        timeout: 阻塞读取超时时间（秒），默认10秒
        enabled_types: 节点启用的类型集合，用于过滤显控台/插件任务
    """
    logging.info(f"[任务管理] 任务线程启动，阻塞超时: {timeout}秒, 启用的类型: {enabled_types}")

    while True:
        try:
            # 阻塞读取任务
            task = read_task(timeout=timeout)

            # 处理任务
            if task:
                handle_task(task, enabled_types=enabled_types)

        except Exception as e:
            logging.error(f"[任务管理] 任务循环异常: {e}")
            time.sleep(3)
