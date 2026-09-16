import os
import sys

import yaml

# 模块级缓存
_CONFIG_CACHE = None


def _get_project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_runtime_base_dirs():
    """
    获取运行时基础目录，兼容开发环境、PyInstaller 和 Nuitka。
    优先级：exe 同目录 -> PyInstaller 临时目录 -> 启动命令所在目录 -> 项目根目录。
    """
    base_dirs = []

    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        base_dirs.append(exe_dir)

        meipass_dir = getattr(sys, '_MEIPASS', None)
        if meipass_dir and meipass_dir not in base_dirs:
            base_dirs.append(meipass_dir)

        argv0 = sys.argv[0] if sys.argv else ""
        if argv0:
            argv_dir = os.path.dirname(os.path.abspath(argv0))
            if argv_dir and argv_dir not in base_dirs:
                base_dirs.append(argv_dir)

    project_root = _get_project_root()
    if project_root not in base_dirs:
        base_dirs.append(project_root)

    return base_dirs


def _resolve_config_path(path_or_name):
    if os.path.isabs(path_or_name):
        return path_or_name

    for base_path in _get_runtime_base_dirs():
        full_path = os.path.join(base_path, path_or_name)
        if os.path.exists(full_path):
            return full_path

    return os.path.join(_get_runtime_base_dirs()[0], path_or_name)


def load_config(config_path=None):
    """
    加载配置文件
    :param config_path: 配置文件路径（可选，默认自动查找 config.yaml）
    :return: dict
    """
    global _CONFIG_CACHE

    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    config_path = _resolve_config_path(config_path or "config.yaml")

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            if config is None:
                raise ValueError("配置文件为空或格式错误")

            _CONFIG_CACHE = config
            return _CONFIG_CACHE

    except Exception as e:
        raise RuntimeError(f"加载配置文件失败: {e}")
