import logging
import os
import sys


def setup_logger(log_file="agent.log", log_dir=None):
    """
    初始化日志系统，同时输出到文件和控制台
    :param log_file: 日志文件名
    :param log_dir: 日志目录（默认为 exe 同目录或项目根目录）
    """
    if log_dir is None:
        if getattr(sys, 'frozen', False):
            log_dir = os.path.dirname(sys.executable)
        else:
            log_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    log_path = os.path.join(log_dir, log_file)

    logger = logging.getLogger("agent")
    logger.setLevel(logging.DEBUG)

    # 清除已有 handler，防止重复
    logger.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 文件 handler
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 控制台 handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# 默认全局 logger（首次调用 setup_logger 后可用）
logger = logging.getLogger("agent")
