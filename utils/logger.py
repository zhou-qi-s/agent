import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler

# 日志保留天数（只保留最近 1 天）
KEEP_DAYS = 1

_LOG_FMT = logging.Formatter(
    "[%(asctime)s] %(levelname)-7s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


def _cleanup_old_logs(log_dir, log_file, keep_days=KEEP_DAYS):
    """
    清理超期历史日志（agent.log.20261026 这类轮转文件）

    backupCount 只管轮转次数，程序长期停机后残留的旧文件仍会占盘，
    所以启动时按 mtime 再扫一遍。
    """
    if not log_dir or not os.path.isdir(log_dir):
        return
    deadline = time.time() - keep_days * 86400
    prefix = log_file + "."
    try:
        for name in os.listdir(log_dir):
            if not name.startswith(prefix):
                continue
            path = os.path.join(log_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < deadline:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def get_log_dir():
    """日志目录：打包后是 exe 所在目录，源码运行时是项目根目录"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_log_path(log_file="agent.log", log_dir=None):
    """日志文件的绝对路径（供日志查看接口复用）"""
    return os.path.join(log_dir or get_log_dir(), log_file)


def setup_logger(log_file="agent.log", log_dir=None, keep_days=KEEP_DAYS, console=None):
    """
    初始化日志系统，同时输出到文件和（可选）控制台

    :param log_file: 日志文件名
    :param log_dir: 日志目录（默认为 exe 同目录或项目根目录）
    :param keep_days: 日志保留天数，默认 1 天
    :param console: 是否输出到控制台；None 表示自动判断
                    （stdout 被重定向到文件/管道时自动关闭，避免和文件 handler 重复写）
    """
    log_dir = log_dir or get_log_dir()
    log_path = get_log_path(log_file, log_dir)

    logger = logging.getLogger("agent")
    logger.setLevel(logging.DEBUG)

    # 清除已有 handler，防止重复
    logger.handlers.clear()

    # ★ 不再向 root 传播：否则 root 上的 handler 会把同一条日志再打一遍
    #   （之前 main.bin.log 里每条都是两行，一行带 module:lineno 一行带 agent |）
    logger.propagate = False

    # 文件 handler：每天 0 点切分，只保留 keep_days 份历史
    fh = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=keep_days,
        encoding="utf-8",
    )
    fh.suffix = "%Y%m%d"
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(_LOG_FMT)
    logger.addHandler(fh)

    # 控制台 handler
    if console is None:
        env = os.environ.get("AGENT_LOG_CONSOLE")
        if env is not None:
            console = env.strip() not in ("0", "false", "False")
        else:
            # 被 nohup/重定向接管时 stdout 不是终端，再打一遍只会让同一个日志文件翻倍
            try:
                console = sys.stdout.isatty()
            except (AttributeError, ValueError):
                console = False
    if console:
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(_LOG_FMT)
        logger.addHandler(ch)

    # 启动时顺手清掉超期文件
    _cleanup_old_logs(log_dir, log_file, keep_days)

    return logger


# 默认全局 logger（首次调用 setup_logger 后可用）
logger = logging.getLogger("agent")
