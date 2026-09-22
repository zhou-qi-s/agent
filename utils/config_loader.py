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


# =============================================================================
# 路径配置（缓存区 / 运行区）
# =============================================================================
#
# 两个目录的职责划分：
#
#   server.download  缓存区：应用包下载、解压后的原始内容存放处。
#                    结构 {download}/{service}/{version}/{app,bin,config}
#                    可随时清理，用于重新下载。不生成 version / application.yml。
#
#   server.apps      运行区：install 阶段在此建立指向缓存区的软链接结构：
#                        {apps}/{service}/
#                        ├── current -> {download}/{service}/{version}   版本指针
#                        ├── app     -> current/app                      软链接
#                        ├── bin     -> current/bin
#                        ├── config  -> current/config
#                        ├── state/config.yaml                           运行状态
#                        └── runtime/                                    运行态产物
#                    切换版本只需重建 current 一条链接，其余共用。
#
#                    注意：运行区**刻意放在缓存区之外** —— 缓存目录可被随时清理，
#                    若运行区在其内部，清缓存会误删正在运行的应用。
#
# 两个键都允许相对路径（相对 Agent 根目录），推荐使用绝对路径。

DEFAULT_DOWNLOAD_DIR = "/var/cache/agent/download"
DEFAULT_APPS_DIR = "/var/cache/agent/apps"


def _is_posix_abs(path: str) -> bool:
    """
    判断是否为 POSIX 绝对路径（以 / 开头）。

    不能只用 os.path.isabs：在 Windows 上它会把 '/var/cache' 判为相对路径，
    导致本机调试时被错误地拼上盘符（D:\\var\\cache）。
    Agent 的部署目标是 Linux，配置里写的 /var/... 应始终按绝对路径处理。
    """
    return path.startswith("/") or path.startswith("\\")


def _normalize_dir(raw: str) -> str:
    """
    规范化目录路径：展开 ~、转为绝对路径、去掉尾部斜杠。
    POSIX 绝对路径原样保留；相对路径按 Agent 项目根目录解析。
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    raw = os.path.expanduser(raw)

    # POSIX 绝对路径：保留原始分隔符，不做 normpath
    #（Windows 上 normpath 会把 '/' 转成 '\\'，破坏目标环境路径）
    if _is_posix_abs(raw):
        return raw.rstrip("/") or "/"

    if not os.path.isabs(raw):
        raw = os.path.join(_get_project_root(), raw)
    return os.path.normpath(raw)


def get_download_dir() -> str:
    """
    获取「缓存区」根目录，对应 config.yaml 的 server.download。

    未配置时回退到 DEFAULT_DOWNLOAD_DIR。
    """
    cfg = load_config()
    raw = cfg.get("server", {}).get("download", "")
    return _normalize_dir(raw) or DEFAULT_DOWNLOAD_DIR


def get_apps_dir() -> str:
    """
    获取「运行区」根目录，对应 config.yaml 的 server.apps。

    未配置时回退到 DEFAULT_APPS_DIR。
    """
    cfg = load_config()
    raw = cfg.get("server", {}).get("apps", "")
    return _normalize_dir(raw) or DEFAULT_APPS_DIR
