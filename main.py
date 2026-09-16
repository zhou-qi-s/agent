import sys
import os

# 在所有其他 import 之前初始化日志，确保任何阶段的错误都能被记录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils.logger import setup_logger
setup_logger()
from utils.logger import logger
logger.info("=== Agent 启动 ===")

import json
import threading
import time
import traceback

from core.heartbeat import heartbeat_loop
from core.helm import init_kubeconfig, sync_helm_list
from core.register import register_agent
from core.task_utils import task_loop
from core.k8s_agent import k8s_task_loop
from core.process import collect_and_upload
from core.nacos.nacos_register import get_nacos_register
from core.nacos.nacos_heartbeat import get_nacos_heartbeat
from core.nacos.k8s_nacos_register import k8s_nacos_register_loop
from core.nacos.k8s_nacos_heartbeat import k8s_nacos_heartbeat_loop
from utils.redis_client import init_redis



def start_api_in_thread():
    """
    使用独立线程启动 FastAPI 服务
    """
    def _run_server():
        try:
            import uvicorn
            from utils.config_loader import load_config
            from api.app import app

            config = load_config()
            server_config = config.get('server', {}).get('fastapi', {})

            host = server_config.get('host', '0.0.0.0')
            port = server_config.get('port', 8000)
            workers = server_config.get('workers', 1)

            # host 为特定 IP 时，绑定 0.0.0.0 + 中间件做 IP 白名单
            if host not in ("0.0.0.0", ""):
                app.state.allowed_host = host
                bind_host = "0.0.0.0"
                logger.info(f"API 访问控制: 仅允许 127.0.0.1, {host}")
            else:
                app.state.allowed_host = None
                bind_host = host

            logger.info(f"API 地址: http://{host}:{port}")
            logger.info(f"Swagger:  http://{host}:{port}/docs")
            logger.info(f"Worker 进程数: {workers}")

            # 注意：FastAPI 在独立线程中启动，必须禁用 uvicorn 的 signal handler
            # （signal 只能在主线程注册，否则抛 "signal only works in main thread of the main interpreter"）
            # 且线程内无法 spawn 多进程，只能使用单 worker
            uvicorn_config = uvicorn.Config(
                "api.app:app",
                host=bind_host,
                port=port,
                workers=1,
                log_level="info",
            )
            server = uvicorn.Server(uvicorn_config)
            server.install_signal_handlers = lambda: None  # 禁用信号处理
            server.run()
        except Exception as e:
            logger.error(f"FastAPI 服务线程异常: {e}")
            traceback.print_exc()


    api_thread = threading.Thread(target=_run_server, daemon=True)
    api_thread.start()
    return api_thread


def main():
    try:
        logger.info("=" * 50)
        logger.info("Agent 系统启动中...")
        logger.info("=" * 50)

        # ========== 启动 FastAPI API 服务 ==========
        logger.info("启动 FastAPI 管理接口...")
        api_thread = start_api_in_thread()

        time.sleep(3)
        logger.info("FastAPI 服务状态: 线程运行中")

        # ========== 连接 Redis（带重试） ==========
        logger.info("连接 Redis...")
        from utils.config_loader import load_config
        _cfg = load_config()
        _retry_cfg = _cfg.get('agent', {}).get('retry', {})
        _max_retry = _retry_cfg.get('count', 5)
        _retry_interval = _retry_cfg.get('interval', 3)

        for attempt in range(1, _max_retry + 1):
            try:
                init_redis()
                break
            except Exception as e:
                logger.warning(f"Redis 连接失败（第 {attempt}/{_max_retry} 次）: {e}")
                if attempt < _max_retry:
                    logger.info(f"{_retry_interval} 秒后重试...")
                    time.sleep(_retry_interval)
                else:
                    logger.error(f"Redis 连接失败，已达最大重试次数 {_max_retry}，Agent 退出")
                    return

        # ========== 注册阶段 ==========
        logger.info("开始注册节点信息...")
        data = register_agent()

        if not data:
            logger.error("注册失败，Agent 退出运行")
            return

        try:
            response = json.loads(data)
        except json.JSONDecodeError as e:
            logger.error(f"注册响应 JSON 解析失败: {e}, 原始数据: {data[:500]}")
            return

        # 兼容两种返回格式: {"code": 200, "data": "xxx"} 或 {"data": "xxx"}
        agent_id = response.get("data")
        if not agent_id:
            logger.error(f"注册响应中缺少 'data' 字段, 完整响应: {response}")
            return

        logger.info(f"注册/上线成功, ID: {agent_id}")

        # ========== 心跳线程 ==========
        logger.info("启动心跳线程...")
        hb_thread = threading.Thread(target=heartbeat_loop, kwargs={"agent_id": agent_id}, daemon=True)
        hb_thread.start()

        # ========== 读取任务开关类型（仅支持复合类型） ==========
        _types_raw = (_cfg.get("server", {}) or {}).get("type", "") or ""
        _ENABLED_TYPES = set(t.strip().upper() for t in _types_raw.split(",") if t.strip())
        logger.info(f"启用的任务类型: {_ENABLED_TYPES}")

        # ========== 任务处理线程（始终启用，从 Redis 拉取任务） ==========
        # 传入启用的类型，用于过滤显控台（DISPLAY_CONSOLE）/插件（PLUGIN）任务
        logger.info("启动任务处理线程...")
        task_thread = threading.Thread(
            target=task_loop, kwargs={"timeout": 10, "enabled_types": _ENABLED_TYPES}, daemon=True
        )
        task_thread.start()

        # ========== 节点资源采集线程（始终启用，写入 download/node/runtime/resources.txt） ==========
        logger.info("启动节点资源采集线程...")
        def _node_resource_loop():
            """每 10 秒采集节点 CPU/内存/IO 资源"""
            from core.alarm.node_alarm import collect_and_write_node_resources
            while True:
                try:
                    collect_and_write_node_resources()
                except Exception as e:
                    logger.warning(f"节点资源采集异常: {e}")
                time.sleep(10)

        node_resource_thread = threading.Thread(target=_node_resource_loop, name="NodeResource", daemon=True)
        node_resource_thread.start()

        # ========== 节点告警检测线程（始终启用，Redis key: alarm:node:{ip}） ==========
        logger.info("启动节点告警检测线程...")
        def _node_alarm_loop():
            """每 30 秒检测节点资源是否超过阈值"""
            from core.alarm.node_alarm import check_and_report_node_alarms
            while True:
                try:
                    result = check_and_report_node_alarms()
                    if result:
                        logger.warning(f"节点告警检测完成: 触发 {len(result)} 条告警")
                        for r in result:
                            logger.warning(f"  节点告警: type={r['type']}, value={r['value']}, threshold={r['threshold']}")
                    else:
                        logger.info("节点告警检测完成: 无告警")
                except Exception as e:
                    logger.warning(f"节点告警检测异常: {e}")
                time.sleep(30)

        node_alarm_thread = threading.Thread(target=_node_alarm_loop, name="NodeAlarm", daemon=True)
        node_alarm_thread.start()

        # ========== Promtail 日志采集（启动时启动 promtail 采集本地日志推送到 Loki） ==========
        _log_cfg = _cfg.get("log_collect", {})
        if _log_cfg.get("enabled", False):
            logger.info("启动 promtail 日志采集...")
            from core.promtail import start_promtail, promtail_loop
            if start_promtail():
                logger.info("promtail 启动成功")
            else:
                logger.warning("promtail 启动失败，守护线程将自动重试")
            # 守护线程：定期检查 promtail 存活，异常时自动重启
            promtail_thread = threading.Thread(
                target=promtail_loop, kwargs={"interval": 30}, name="PromtailGuard", daemon=True
            )
            promtail_thread.start()

        # ========== K8s 容器告警检测（CONTAINER 使用） ==========
        def _k8s_alarm_loop():
            """每 30 秒遍历 kubernetes 目录，kubectl top 采集 Pod 资源，匹配阈值并上报告警"""
            from core.alarm.k8s_alarm import check_and_report_k8s_alarms
            while True:
                try:
                    result = check_and_report_k8s_alarms()
                    if result:
                        logger.warning(f"K8s 告警检测完成: 触发 {len(result)} 条告警")
                        for r in result:
                            logger.warning(f"  K8s告警: type={r['type']}, pod={r['pod']}, "
                                           f"ns={r['namespace']}, value={r['value']}, threshold={r['threshold']}")
                    else:
                        logger.debug("K8s 告警检测完成: 无告警")
                except Exception as e:
                    logger.warning(f"K8s 告警检测异常: {e}")
                time.sleep(30)

        # ========== 进程监控服务（进程上报 + Nacos 注册/心跳 + 进程资源告警） ==========
        # VIRTUAL_MACHINE 与 DISPLAY_CONSOLE 共用，任一类型启用即启动一份
        def _start_process_monitor_services():
            """启动进程上报、Nacos 注册/心跳、进程资源告警线程"""
            def _process_report_loop():
                """每 10 秒采集进程信息并上传到 Redis"""
                _report_interval = 10
                while True:
                    t0 = time.time()
                    try:
                        collect_and_upload(expire=300)
                        logger.debug("进程信息已上报至 Redis")
                    except Exception as e:
                        logger.warning(f"进程信息上报异常: {e}")
                    elapsed = time.time() - t0
                    if elapsed > _report_interval:
                        logger.warning(
                            "[进程上报] ⚠ 单轮耗时 %.1fs 超过间隔 %ds, 跳过等待直接进入下一轮",
                            elapsed, _report_interval
                        )
                        # 不 sleep，直接下一轮
                    else:
                        time.sleep(max(1, _report_interval - elapsed))

            logger.info("启动进程信息上报线程（每 10 秒）...")
            report_thread = threading.Thread(target=_process_report_loop, daemon=True)
            report_thread.start()

            def _nacos_register_loop():
                """每 30 秒执行 Nacos 服务发现并注册"""
                register = get_nacos_register()
                while True:
                    try:
                        result = register.register_all()
                        if result.get("failed", 0) > 0:
                            logger.warning(f"Nacos 注册完成: 服务数={result.get('total', 0)}, "
                                           f"成功={result.get('successful', 0)}, "
                                           f"失败={result['failed']}")
                            for err in result.get("errors", []):
                                logger.warning(f"  Nacos 注册异常: {err.get('folder')} -> {err.get('error')}")
                        else:
                            logger.debug(f"Nacos 注册完成: 服务数={result.get('total', 0)}, "
                                         f"成功={result.get('successful', 0)}")
                    except Exception as e:
                        logger.warning(f"Nacos 注册循环异常: {e}")
                    time.sleep(30)

            logger.info("启动 Nacos 服务注册线程（每 30 秒）...")
            nacos_reg_thread = threading.Thread(target=_nacos_register_loop, daemon=True)
            nacos_reg_thread.start()

            def _nacos_heartbeat_loop():
                """每 10 秒执行 Nacos 心跳发送"""
                heartbeat = get_nacos_heartbeat()
                while True:
                    try:
                        result = heartbeat.discover_and_heartbeat()
                        if result is None:
                            logger.warning("Nacos 心跳调用失败，返回 null")
                        elif result.get("heartbeat", {}).get("failed", 0) > 0:
                            logger.warning(f"Nacos 心跳完成: 服务数={result.get('service_count', 0)}, "
                                           f"成功={result['heartbeat'].get('successful', 0)}, "
                                           f"跳过={result['heartbeat'].get('skipped', 0)}, "
                                           f"失败={result['heartbeat']['failed']}")
                            for err in result['heartbeat'].get('errors', []):
                                logger.warning(f"  Nacos 心跳异常: {err.get('folder')} -> {err.get('error')}")
                        else:
                            logger.debug(f"Nacos 心跳完成: 服务数={result.get('service_count', 0)}, "
                                         f"成功={result['heartbeat'].get('successful', 0)}")
                    except Exception as e:
                        logger.warning(f"Nacos 心跳循环异常: {e}")
                    time.sleep(10)

            logger.info("启动 Nacos 心跳线程（每 10 秒）...")
            nacos_hb_thread = threading.Thread(target=_nacos_heartbeat_loop, daemon=True)
            nacos_hb_thread.start()

            def _alarm_loop():
                """每 30 秒遍历服务目录，采集进程资源，匹配阈值并上报告警"""
                from core.alarm.alarm import check_and_report_alarms
                while True:
                    try:
                        result = check_and_report_alarms()
                        if result:
                            logger.warning(f"告警检测完成: 触发 {len(result)} 条告警")
                            for r in result:
                                logger.warning(f"  告警: type={r['type']}, pid={r['pid']}, "
                                               f"value={r['value']}, service={r['service_name']}")
                        else:
                            logger.debug("告警检测完成: 无告警")
                    except Exception as e:
                        logger.warning(f"告警检测异常: {e}")
                    time.sleep(30)

            logger.info("启动告警检测线程（每 30 秒）...")
            alarm_thread = threading.Thread(target=_alarm_loop, daemon=True)
            alarm_thread.start()

        # ========== VIRTUAL_MACHINE / DISPLAY_CONSOLE：进程上报 + Nacos 注册 + 心跳 + 告警 ==========
        # 两个类型共用一份进程监控服务（任一类型启用即启动，避免重复启动）
        if "VIRTUAL_MACHINE" in _ENABLED_TYPES or "DISPLAY_CONSOLE" in _ENABLED_TYPES:
            _start_process_monitor_services()

        # ========== DISPLAY_CONSOLE：显控台进程巡检（每 10 秒写入 PID 文件） ==========
        if "DISPLAY_CONSOLE" in _ENABLED_TYPES:
            from core.xkt.process_check import xkt_check_loop

            logger.info("启动显控台进程巡检线程（每 10 秒）...")
            xkt_check_thread = threading.Thread(
                target=xkt_check_loop, kwargs={"interval": 10}, daemon=True
            )
            xkt_check_thread.start()

            # 说明：显控台资源巡检（xkt_resource_monitor_loop）已按要求移除，不再启动。

        # ========== HARBOR：检测本机 Harbor 仓库 + Helm 同步 ==========
        if "HARBOR" in _ENABLED_TYPES:
            # 检测本机 Harbor 仓库是否运行（检查配置端口是否监听）
            def _check_harbor_running() -> bool:
                import socket as _socket
                try:
                    from api.utils.harbor_util import get_harbor_connection_info
                    harbor_info = get_harbor_connection_info(require_auth=False)
                    port_str = harbor_info.get("port_str", "")
                    if not port_str:
                        logger.error("Harbor 端口未配置，无法检测 Harbor 仓库")
                        return False
                    port = int(port_str)
                    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                    sock.settimeout(3)
                    try:
                        result = sock.connect_ex(("127.0.0.1", port))
                        return result == 0
                    finally:
                        sock.close()
                except Exception as e:
                    logger.error(f"检测 Harbor 仓库异常: {e}")
                    return False

            if not _check_harbor_running():
                logger.error("没有检测到 harbor 仓库，HARBOR 类型功能不可用，Agent 退出")
                return

            def _helm_sync_loop():
                """每 30 秒同步 Helm release 列表到 Redis"""
                init_kubeconfig()
                while True:
                    try:
                        releases = sync_helm_list()
                        if releases:
                            logger.debug(f"Helm 列表同步完成: {len(releases)} 条 release")
                    except Exception as e:
                        logger.warning(f"Helm 列表同步异常: {e}")
                    time.sleep(30)

            logger.info("启动 Helm release 同步线程（每 30 秒）...")
            helm_thread = threading.Thread(target=_helm_sync_loop, daemon=True)
            helm_thread.start()

        # ========== KUBERNETES：K8s 任务处理 ==========
        if "KUBERNETES" in _ENABLED_TYPES:
            logger.info("启动 K8s 任务处理线程...")
            k8s_thread = threading.Thread(target=k8s_task_loop, kwargs={"timeout": 10}, daemon=True)
            k8s_thread.start()

        # ========== CONTAINER：K8s 容器 Nacos 注册 + 心跳 + 告警 ==========
        if "CONTAINER" in _ENABLED_TYPES:
            logger.info("启动 K8s 容器 Nacos 注册线程（每 30 秒）...")
            k8s_nacos_thread = threading.Thread(
                target=k8s_nacos_register_loop, kwargs={"interval": 30}, daemon=True,
            )
            k8s_nacos_thread.start()

            logger.info("启动 K8s 容器 Nacos 心跳线程（每 10 秒）...")
            k8s_nacos_hb_thread = threading.Thread(
                target=k8s_nacos_heartbeat_loop, kwargs={"interval": 10}, daemon=True,
            )
            k8s_nacos_hb_thread.start()

            logger.info("启动 K8s 容器告警检测线程（每 30 秒）...")
            k8s_alarm_thread = threading.Thread(target=_k8s_alarm_loop, daemon=True)
            k8s_alarm_thread.start()

        logger.info("=" * 50)
        logger.info("Agent 系统启动完成")
        logger.info("=" * 50)
        logger.info(f"  启用的类型: {_ENABLED_TYPES}")
        logger.info(f"  FastAPI API 服务（始终启用）")
        logger.info(f"  心跳上报（始终启用）")
        logger.info(f"  任务处理（始终启用）")
        if "VIRTUAL_MACHINE" in _ENABLED_TYPES or "DISPLAY_CONSOLE" in _ENABLED_TYPES:
            logger.info(f"  进程信息上报（每 10 秒）")
            logger.info(f"  Nacos 服务注册（每 30 秒）")
            logger.info(f"  Nacos 心跳发送（每 10 秒）")
            logger.info(f"  进程资源告警（每 30 秒）")
        if "HARBOR" in _ENABLED_TYPES:
            logger.info(f"  Helm release 同步（每 30 秒）")
        if "KUBERNETES" in _ENABLED_TYPES:
            logger.info(f"  K8s 任务处理")
        if "CONTAINER" in _ENABLED_TYPES:
            logger.info(f"  K8s 容器 Nacos 注册（每 30 秒）")
            logger.info(f"  K8s 容器 Nacos 心跳（每 10 秒）")
            logger.info(f"  K8s 容器资源告警（每 30 秒）")
        if "DISPLAY_CONSOLE" in _ENABLED_TYPES:
            logger.info(f"  显控台进程巡检（每 10 秒）")
            logger.info(f"  显控台任务处理（xkt_*，仅本类型节点执行）")
        if "PLUGIN" in _ENABLED_TYPES:
            logger.info(f"  插件任务处理（plugin_*，仅本类型节点执行）")
        logger.info(f"  Agent ID: {agent_id}")
        logger.info("=" * 50)

        # ========== 主线程保持存活 ==========
        while True:
            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("收到退出信号，正在安全关闭所有服务...")

    except Exception as e:
        logger.error(f"Agent 系统启动失败: {e}", exc_info=True)



if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--xkt-check":
        # 手动测试显控台进程巡检
        from core.xkt.process_check import check_all_xkt_services
        logger.info("=== 手动运行显控台进程巡检 ===")
        results = check_all_xkt_services()
        if results:
            for r in results:
                status = "运行中" if r["running"] else "已停止"
                logger.info(f"  [{status}] 服务={r['service_name']}, 进程={r['process_name']}, PID={r['pids']}")
        else:
            logger.info("  未发现 xkt 服务")
        logger.info("=== 巡检结束 ===")
    else:
        main()
