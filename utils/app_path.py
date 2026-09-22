"""
应用目录定位工具（公共）

下载区（缓存）与运行区的目录查找逻辑统一放这里，供各任务模块复用，
避免 download / install / start / stop / upgrade 等模块各写一份。

目录约定
--------
缓存区（config.yaml → server.download）：应用包下载、解压后的原始内容
    {download}/{app_name}/
        ├── {version}/          版本目录，内含 app/ bin/ config/
        └── {version}/ ...

运行区（config.yaml → server.apps）：软链接结构，实际运行位置
    {apps}/{app_name}/
        ├── state/config.yaml      运行状态（pid / name / runtime / version）
        ├── current -> {download}/{app_name}/{version}
        ├── app     -> current/app
        ├── bin     -> current/bin
        └── config  -> current/config
"""

import logging
import os
from typing import Any, Dict, List, Optional

import yaml

from utils.config_loader import get_apps_dir, get_download_dir

# 运行区状态目录与状态文件名
STATE_DIR_NAME = "state"
STATE_FILE_NAME = "config.yaml"

# 运行区 pid 文件名（位于 state/ 目录内，与状态文件一起管理）
PID_FILE_NAME = "pid"

# 运行区 Nacos 配置目录与文件名
# 目录结构：{apps}/{app_name}/nacos/config.yaml
# 原位置为 {download}/{app_name}/{version}/runtime/config.yaml，改造后归入运行区
NACOS_DIR_NAME = "nacos"

# 版本指针链接名
CURRENT_LINK_NAME = "current"

# 需要以软链接方式挂到运行区的子目录
LINK_DIR_NAMES = ("app", "bin", "config")

# ── sub_dir：应用类别分层 ──
# 虚拟机应用（含通用服务）：无分层，直接用 {根}/{服务名}
# 插件应用：{根}/plugin/{服务名}
# 显控台应用：{根}/displayConsole/{服务名}
SUB_DIR_PLUGIN = "plugin"
SUB_DIR_XKT = "displayConsole"


def _join_sub_dir(root: str, sub_dir: str = "") -> str:
    """
    按应用类别拼接路径前缀。

    参数:
        root:    根目录（缓存区或运行区）
        sub_dir: 类别子目录，空表示虚拟机应用（无分层）

    返回:
        拼接后的路径
    """
    sub = (sub_dir or "").strip().strip("/\\")
    return os.path.join(root, sub) if sub else root


# 类别层探测顺序：虚拟机（无分层）→ 显控台 → 插件
SUB_DIR_CANDIDATES = ("", SUB_DIR_XKT, SUB_DIR_PLUGIN)


def resolve_sub_dir(app_name: str, roots: Optional[List[str]] = None) -> str:
    """
    探测应用属于哪个类别层（供「只拿到服务名」的调用方定位目录）。

    平台不少链路只传服务名（例如告警扫描 alarm:{ip}:{service}、进程查询接口、
    版本列表接口），而显控台/插件服务在缓存区与运行区都多一层，
    必须先探测出层名，否则整类服务会被误判为"目录不存在"而跳过。

    参数:
        app_name: 应用名称
        roots:    探测的根目录列表，默认先运行区、再缓存区

    返回:
        类别子目录名（"" / "displayConsole" / "plugin"）；
        都匹配不到时返回 ""（由调用方按"不存在"处理）
    """
    app_name = (app_name or "").strip()
    if not app_name:
        return ""
    if roots is None:
        roots = [get_apps_dir(), get_download_dir()]
    for sub_dir in SUB_DIR_CANDIDATES:
        for root in roots:
            if not root:
                continue
            candidate = os.path.join(_join_sub_dir(root, sub_dir), app_name)
            if os.path.isdir(candidate):
                return sub_dir
    return ""


def find_cache_component_dir(app_name: str, sub_dir: str = "") -> Optional[str]:
    """
    在缓存区中查找应用的组件目录。

    对应目录：{server.download}/[{sub_dir}/]{app_name}/

    参数:
        app_name: 应用名称（平台下发的 file_name）
        sub_dir:  类别子目录（"plugin" / "displayConsole"，留空为虚拟机应用）

    返回:
        目录绝对路径；不存在返回 None
    """
    app_name = (app_name or "").strip()
    if not app_name:
        return None

    download_dir = get_download_dir()
    if not download_dir:
        return None

    component_dir = os.path.join(_join_sub_dir(download_dir, sub_dir), app_name)
    return component_dir if os.path.isdir(component_dir) else None


def list_cache_versions(app_name: str, sub_dir: str = "") -> List[str]:
    """
    列出缓存区中该应用已下载的全部版本号。

    只识别「真实目录」，忽略软链接与普通文件，
    避免把 current 之类的指针误当成版本号。

    参数:
        app_name: 应用名称
        sub_dir:  类别子目录（"plugin" / "displayConsole"，留空为虚拟机应用）

    返回:
        版本号列表（按名称升序）；无版本时返回空列表
    """
    component_dir = find_cache_component_dir(app_name, sub_dir)
    if not component_dir:
        return []

    versions = []
    try:
        for name in os.listdir(component_dir):
            path = os.path.join(component_dir, name)
            if os.path.isdir(path) and not os.path.islink(path):
                versions.append(name)
    except OSError as e:
        logging.warning("[路径] 读取组件目录失败: %s -> %s", component_dir, e)
        return []

    return sorted(versions)


def find_cache_version_dir(app_name: str, version: str = "", sub_dir: str = "") -> Optional[str]:
    """
    在缓存区中查找指定版本的目录。

    对应目录：{server.download}/[{sub_dir}/]{app_name}/{version}/

    参数:
        app_name: 应用名称
        version:  版本号；留空时取「修改时间最新」的版本
        sub_dir:  类别子目录（"plugin" / "displayConsole"，留空为虚拟机应用）

    返回:
        目录绝对路径；不存在返回 None
    """
    component_dir = find_cache_component_dir(app_name, sub_dir)
    if not component_dir:
        return None

    # 指定版本：直接校验目录是否存在
    if version:
        version_dir = os.path.join(component_dir, version.strip())
        return version_dir if os.path.isdir(version_dir) else None

    # 未指定版本：取最新的一个
    versions = list_cache_versions(app_name, sub_dir)
    if not versions:
        return None

    def _mtime(ver: str) -> float:
        try:
            return os.path.getmtime(os.path.join(component_dir, ver))
        except OSError:
            return 0.0

    latest = max(versions, key=_mtime)
    if len(versions) > 1:
        logging.info("[路径] 存在多个版本 %s，选用最新的: %s", versions, latest)
    return os.path.join(component_dir, latest)


def find_apps_component_dir(app_name: str, sub_dir: str = "") -> Optional[str]:
    """
    在运行区中查找应用的组件目录。

    对应目录：{server.apps}/[{sub_dir}/]{app_name}/

    参数:
        app_name: 应用名称
        sub_dir:  类别子目录（"plugin" / "displayConsole"，留空为虚拟机应用）

    返回:
        目录绝对路径；不存在返回 None
    """
    app_name = (app_name or "").strip()
    if not app_name:
        return None

    apps_dir = get_apps_dir()
    if not apps_dir:
        return None

    component_dir = os.path.join(_join_sub_dir(apps_dir, sub_dir), app_name)
    return component_dir if os.path.isdir(component_dir) else None


def find_apps_version_dir(app_name: str, sub_dir: str = "") -> Optional[str]:
    """
    解析运行区中 current 指向的真实目录（即当前生效的版本目录）。

    对应关系：{server.apps}/[{sub_dir}/]{app_name}/current
              -> {server.download}/[{sub_dir}/]{app_name}/{version}

    参数:
        app_name: 应用名称
        sub_dir:  类别子目录（"plugin" / "displayConsole"，留空为虚拟机应用）

    返回:
        current 解析后的真实路径；不存在或断链返回 None
    """
    component_dir = find_apps_component_dir(app_name, sub_dir)
    if not component_dir:
        return None

    current_link = os.path.join(component_dir, CURRENT_LINK_NAME)
    if not os.path.exists(current_link):
        return None

    return os.path.realpath(current_link)


# =============================================================================
# 运行状态文件（{apps}/{app_name}/state/config.yaml）
# =============================================================================
#
# 字段:
#   pids      进程号数组，如 [12345, 67890]（未运行时为空数组）
#   processes 进程名数组，与 pids 下标一一对应，如 [java, agent]
#   name      应用名
#   runtime   是否正在运行（true / false，pids 为空时必为 false）
#   version   当前部署/运行的版本号
#
# 示例:
#   pids: [12345, 67890]
#   processes: [java, agent]
#   name: ruoyi
#   runtime: true
#   version: 3.9.2
#
# 用途:
#   1. install 前据此判断该应用是否正在运行 —— runtime=true 则拒绝安装；
#   2. 未运行时把残留的 pids/processes 清空；
#   3. 记录当前生效版本，供 start/stop 等模块读取。


def get_state_dir(app_name: str, sub_dir: str = "") -> Optional[str]:
    """
    获取运行区状态目录：{server.apps}/[{sub_dir}/]{app_name}/state

    参数:
        app_name: 应用名称
        sub_dir:  类别子目录（"plugin" / "displayConsole"，留空为虚拟机应用）

    返回:
        目录绝对路径；apps 未配置或 app_name 为空返回 None
    """
    app_name = (app_name or "").strip()
    if not app_name:
        return None

    apps_dir = get_apps_dir()
    if not apps_dir:
        return None

    return os.path.join(_join_sub_dir(apps_dir, sub_dir), app_name, STATE_DIR_NAME)


def get_nacos_dir(app_name: str, sub_dir: str = "") -> Optional[str]:
    """
    获取运行区 Nacos 配置目录：{server.apps}/[{sub_dir}/]{app_name}/nacos

    改造后 Nacos 配置从缓存区的 {download}/{app_name}/{version}/runtime/
    迁到运行区的独立 nacos 目录。

    参数:
        app_name: 应用名称
        sub_dir:  类别子目录（"plugin" / "displayConsole"，留空为虚拟机应用）

    返回:
        目录绝对路径；apps 未配置或 app_name 为空返回 None
    """
    app_name = (app_name or "").strip()
    if not app_name:
        return None

    apps_dir = get_apps_dir()
    if not apps_dir:
        return None

    return os.path.join(_join_sub_dir(apps_dir, sub_dir), app_name, NACOS_DIR_NAME)


def get_nacos_config_file(app_name: str, sub_dir: str = "") -> Optional[str]:
    """
    获取运行区 Nacos 配置文件路径：{server.apps}/[{sub_dir}/]{app_name}/nacos/config.yaml

    参数:
        app_name: 应用名称

    返回:
        文件绝对路径；apps 未配置或 app_name 为空返回 None
    """
    nacos_dir = get_nacos_dir(app_name, sub_dir)
    return os.path.join(nacos_dir, STATE_FILE_NAME) if nacos_dir else None


def get_state_file(app_name: str, sub_dir: str = "") -> Optional[str]:
    """
    获取运行区状态文件路径：{server.apps}/[{sub_dir}/]{app_name}/state/config.yaml

    参数:
        app_name: 应用名称

    返回:
        文件绝对路径；apps 未配置或 app_name 为空返回 None
    """
    state_dir = get_state_dir(app_name, sub_dir)
    return os.path.join(state_dir, STATE_FILE_NAME) if state_dir else None


def read_state(app_name: str, sub_dir: str = "") -> Dict[str, Any]:
    """
    读取运行区状态文件。

    参数:
        app_name: 应用名称

    返回:
        状态字典；文件不存在或解析失败返回空 dict
    """
    state_file = get_state_file(app_name, sub_dir)
    if not state_file or not os.path.isfile(state_file):
        return {}

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning("[路径] 读取状态文件失败: %s -> %s", state_file, e)
        return {}


def write_state(app_name: str, state: Dict[str, Any], sub_dir: str = "") -> bool:
    """
    写入运行区状态文件（目录不存在则创建）。

    参数:
        app_name: 应用名称
        state:    状态字典，如 {"pid": "", "name": "ruoyi",
                              "runtime": False, "version": "3.9.2"}

    返回:
        是否写入成功
    """
    state_file = get_state_file(app_name, sub_dir)
    if not state_file:
        return False

    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "w", encoding="utf-8") as f:
            yaml.dump(state, f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)
        logging.info("[路径] 状态文件已写入: %s", state_file)
        return True
    except Exception as e:
        logging.error("[路径] 写入状态文件失败: %s -> %s", state_file, e)
        return False


def is_running(app_name: str, sub_dir: str = "") -> bool:
    """
    判断应用当前是否处于运行状态（读状态文件的 runtime 字段）。

    参数:
        app_name: 应用名称

    返回:
        runtime 为真值时返回 True，否则 False
    """
    state = read_state(app_name, sub_dir)
    return bool(state.get("runtime", False))


# =============================================================================
# PID 文件（{apps}/{app_name}/state/pid）
# =============================================================================
#
# 由包内 start.sh 在执行时写入（echo $! > .../state/pid）。
# Agent 在启动脚本执行完成后读取该文件，等待一段时间确认进程存活，
# 存活则把 pid 记入状态文件并置 runtime=true。


def get_pid_file(app_name: str, sub_dir: str = "") -> Optional[str]:
    """
    获取运行区 pid 文件路径：{server.apps}/[{sub_dir}/]{app_name}/state/pid

    参数:
        app_name: 应用名称

    返回:
        文件绝对路径；apps 未配置或 app_name 为空返回 None
    """
    state_dir = get_state_dir(app_name, sub_dir)
    return os.path.join(state_dir, PID_FILE_NAME) if state_dir else None


def read_pid(app_name: str, sub_dir: str = "") -> str:
    """
    读取 pid 文件内容（可能含多行，取第一行非空内容）。

    参数:
        app_name: 应用名称

    返回:
        pid 字符串；文件不存在或为空返回 ""
    """
    pid_file = get_pid_file(app_name, sub_dir)
    if not pid_file or not os.path.isfile(pid_file):
        return ""

    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    return line
    except OSError as e:
        logging.warning("[路径] 读取 pid 文件失败: %s -> %s", pid_file, e)
    return ""


def write_pid(app_name: str, pid: str, sub_dir: str = "") -> bool:
    """
    写入 pid 文件（目录不存在则创建）。

    参数:
        app_name: 应用名称
        pid:      进程号

    返回:
        是否写入成功
    """
    pid_file = get_pid_file(app_name, sub_dir)
    if not pid_file:
        return False

    try:
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(pid))
        logging.info("[路径] pid 文件已写入: %s -> %s", pid_file, pid)
        return True
    except Exception as e:
        logging.error("[路径] 写入 pid 文件失败: %s -> %s", pid_file, e)
        return False


def clear_pid(app_name: str, sub_dir: str = "") -> bool:
    """
    删除 pid 文件（停止/卸载时清理）。

    参数:
        app_name: 应用名称

    返回:
        是否执行了删除
    """
    pid_file = get_pid_file(app_name, sub_dir)
    if not pid_file or not os.path.exists(pid_file):
        return False
    try:
        os.remove(pid_file)
        logging.info("[路径] pid 文件已删除: %s", pid_file)
        return True
    except OSError as e:
        logging.warning("[路径] 删除 pid 文件失败: %s -> %s", pid_file, e)
        return False


def normalize_pid_list(raw: Any) -> List[int]:
    """
    把各种形式的 pid 输入统一成 int 列表。

    兼容：
        - 数组形式（新）：   [12345, 67890] / ["12345", "67890"]
        - 字符串形式（旧）： "12345" / "12345\\n67890" / "12345,67890" / "12345 67890"

    自动去重、剔除非数字、忽略非法值。

    参数:
        raw: pid 原始值（列表或字符串）

    返回:
        pid 整数列表（保持出现顺序）
    """
    if raw is None:
        return []

    items: List[str] = []
    if isinstance(raw, (list, tuple)):
        items = [str(x).strip() for x in raw]
    elif isinstance(raw, str):
        # 兼容换行 / 逗号 / 空格 分隔
        normalized = raw.replace(",", "\n").replace(" ", "\n")
        items = [x.strip() for x in normalized.splitlines()]
    else:
        items = [str(raw).strip()]

    result: List[int] = []
    seen = set()
    for item in items:
        if not item or not item.isdigit():
            continue
        num = int(item)
        if num in seen:
            continue
        seen.add(num)
        result.append(num)
    return result


def read_pids_from_file(app_name: str, sub_dir: str = "") -> List[int]:
    """
    读取 state/pid 文件并解析为 pid 整数列表（脚本写入的文件，可能多行）。

    参数:
        app_name: 应用名称

    返回:
        pid 整数列表；文件不存在或为空返回 []
    """
    pid_file = get_pid_file(app_name, sub_dir)
    if not pid_file or not os.path.isfile(pid_file):
        return []

    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            return normalize_pid_list(f.read())
    except OSError as e:
        logging.warning("[路径] 读取 pid 文件失败: %s -> %s", pid_file, e)
        return []


def read_state_pids(app_name: str, sub_dir: str = "") -> List[int]:
    """
    从运行状态文件读取 pids 数组。

    state/config.yaml:
        pids: [12345, 67890]      （新格式，数组）
        pid: '12345'              （旧格式，兼容读取）

    参数:
        app_name: 应用名称

    返回:
        pid 整数列表
    """
    state = read_state(app_name, sub_dir)
    # 优先新字段 pids；兼容旧字段 pid
    return normalize_pid_list(state.get("pids", state.get("pid")))


def is_process_alive(pid: str) -> bool:
    """
    判断指定 pid 的进程是否存活。

    跨平台：
      - Linux/macOS：os.kill(pid, 0)，并额外排除僵尸态（Z）
      - Windows：tasklist 查询

    参数:
        pid: 进程号（字符串或数字）

    返回:
        进程存活返回 True
    """
    pid_str = str(pid or "").strip()
    if not pid_str:
        return False

    try:
        pid_int = int(pid_str)
    except (ValueError, TypeError):
        return False

    if pid_int <= 0:
        return False

    if os.name == "nt":
        try:
            import subprocess
            # -v 在部分简体中文 Windows 下可能无声卡死，故不带 -v 且加超时
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid_int}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid_int) in (r.stdout or "")
        except Exception:
            return False

    # POSIX
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 无权限说明进程存在（属于其他用户）
        return True
    except OSError:
        return False

    # 额外排除僵尸态：进程已终止但未被父进程回收
    try:
        with open(f"/proc/{pid_int}/stat", "r") as f:
            stat = f.read()
        parts = stat.rsplit(")", 1)
        if len(parts) == 2:
            state = parts[1].split()[0] if parts[1].split() else ""
            if state in ("Z", "z", "X", "x"):
                return False
    except (OSError, IndexError):
        pass

    return True


def filter_alive_pids(pids: List[Any], app_name: str = "") -> List[int]:
    """
    过滤出仍然存活的 pid。

    参数:
        pids:     pid 列表（int 或 str 均可）
        app_name: 应用名称（仅用于日志）

    返回:
        存活 pid 整数列表（保持原顺序）
    """
    alive: List[int] = []
    for pid in normalize_pid_list(pids):
        if is_process_alive(pid):
            alive.append(pid)
        else:
            logging.info("[路径] pid %s 已不存在（应用 %s），跳过", pid, app_name or "?")
    return alive


def refresh_state_pids(app_name: str,
                       pids: List[Any],
                       processes: Optional[List[str]] = None,
                       sub_dir: str = "") -> Dict[str, Any]:
    """
    按存活结果刷新运行状态文件中的 pids / processes / runtime。

    规则：
      - 尚有存活 pid → pids 写回存活列表，processes 同步裁剪，runtime 置 True
      - 全部已死     → pids 与 processes 清空，runtime 置 False

    用于采集时顺带清理死 pid，避免残留越积越多。

    参数:
        app_name:  应用名称
        pids:      存活 pid 列表
        processes: 对应的进程名列表（与 pids 下标一一对应）；
                   不传则从原状态中按旧列表位置裁剪
    """
    state = read_state(app_name, sub_dir)
    if not state:
        # 状态文件不存在则不凭空创建（可能只是尚未安装的服务）
        return {}

    old_pids = normalize_pid_list(state.get("pids", state.get("pid")))
    old_names = state.get("processes") or []
    if not isinstance(old_names, list):
        old_names = []

    alive_nums = normalize_pid_list(pids)

    if alive_nums:
        if processes is not None:
            # 调用方显式给出进程名
            new_names = [str(n) for n in processes]
        else:
            # 按旧列表位置裁剪：保留存活 pid 对应的名字
            name_map = {}
            for idx, p in enumerate(old_pids):
                if idx < len(old_names):
                    name_map[p] = old_names[idx]
            new_names = [name_map.get(p, "") for p in alive_nums]

        state["pids"] = alive_nums
        state["processes"] = new_names
        state["runtime"] = True
        # 清掉旧字段，避免两份数据并存
        state.pop("pid", None)
    else:
        if old_pids:
            logging.info("[路径] 应用 %s 的进程全部失效，已清空并置 runtime=false", app_name)
        state["pids"] = []
        state["processes"] = []
        state["runtime"] = False
        state.pop("pid", None)

    write_state(app_name, state, sub_dir)
    return state


def get_process_name(pid: Any) -> str:
    """
    获取进程名（用于写入 processes 数组）。

    优先用 psutil 取 name()，失败时回退读 /proc/{pid}/comm。

    参数:
        pid: 进程号

    返回:
        进程名字符串；取不到返回 ""
    """
    pid_list = normalize_pid_list(pid)
    if not pid_list:
        return ""
    pid_int = pid_list[0]

    try:
        import psutil
        return psutil.Process(pid_int).name() or ""
    except Exception:
        pass

    # 回退：Linux 下读 /proc
    try:
        with open(f"/proc/{pid_int}/comm", "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def get_process_names(pids: Any) -> List[str]:
    """
    批量获取进程名，与 pids 下标一一对应。

    参数:
        pids: 进程号列表

    返回:
        进程名列表（与 pids 等长，取不到的为空字符串）
    """
    return [get_process_name(p) for p in normalize_pid_list(pids)]


def wait_process_alive(app_name: str, pids: Any, wait_seconds: int = 60) -> bool:
    """
    等待指定秒数后确认进程是否仍存活。

    用于启动任务：脚本执行完成后等待一段时间，避免"秒退"被判为启动成功。
    等待期间若进程已提前退出，直接返回 False（不必等满）。

    支持多个 pid：**全部存活才算通过**（任一提前退出即失败）。

    参数:
        app_name:     应用名称（仅用于日志）
        pids:         进程号（单个或列表）
        wait_seconds: 等待秒数，默认 60

    返回:
        全部进程存活返回 True；pid 列表为空返回 False
    """
    import time

    pid_list = normalize_pid_list(pids)
    if not pid_list:
        return False

    deadline = time.time() + max(0, wait_seconds)
    while time.time() < deadline:
        time.sleep(1)
        # 任一进程退出即判定失败
        for pid in pid_list:
            if not is_process_alive(pid):
                logging.info("[路径] pid %s 已退出（应用 %s）", pid, app_name)
                return False

    # 等待结束，复查全部进程
    return all(is_process_alive(pid) for pid in pid_list)


# =============================================================================
# 运行区软链接管理
# =============================================================================

def remove_link_or_dir(path: str) -> bool:
    """
    安全删除：符号链接直接 unlink（不追进目标目录），真实目录递归删除。

    ⚠️ 必须先判断 islink：对目录软链接调用 shutil.rmtree 会删掉目标目录内容。

    参数:
        path: 待删除路径

    返回:
        是否执行了删除（路径不存在时返回 False）
    """
    if os.path.islink(path):
        os.unlink(path)
        return True
    if os.path.isdir(path):
        import shutil
        shutil.rmtree(path)
        return True
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


# ── 运行期可写目录 ──
# 这些目录是应用运行过程中写入的（日志 / 数据 / 临时文件）。
# 因为运行区的 app/ 是指向缓存区的软链接，应用按自己硬编码的路径写入时
# 会落到「缓存区」→ 缓存区被写脏，且"清缓存重下"会连运行数据一起删掉。
# 所以在 install（建软链接）阶段把这些目录重定向到运行区：
#     {download}/.../{version}/app/{name}  ->  {apps}/.../{app_name}/{name}
# 可用 config.yaml 的 server.writable_dirs 覆盖（逗号分隔）。
DEFAULT_WRITABLE_DIR_NAMES = (
    "logs", "log", "data", "temp", "tmp", "work", "output", "run", "uploads",
)


def get_writable_dir_names() -> tuple:
    """
    返回需要重定向到运行区的「运行期可写目录」名单。

    优先读 config.yaml 的 server.writable_dirs（字符串按逗号分隔，或数组），
    未配置时用 DEFAULT_WRITABLE_DIR_NAMES。
    """
    try:
        from utils.config_loader import load_config
        raw = (load_config().get("server", {}) or {}).get("writable_dirs")
    except Exception:
        raw = None

    if isinstance(raw, str) and raw.strip():
        return tuple(n.strip() for n in raw.split(",") if n.strip())
    if isinstance(raw, (list, tuple)) and raw:
        return tuple(str(n).strip() for n in raw if str(n).strip())
    return DEFAULT_WRITABLE_DIR_NAMES


def _move_tree(src: str, dst: str) -> None:
    """
    把 src 目录整体搬到 dst（用于把包自带的运行期目录挪到运行区）。

    策略（优先零拷贝、绝不覆盖已有文件）：
        · dst 不存在 → 直接 os.rename（同盘秒级）；跨设备失败再退化为逐项搬运
        · dst 已存在 → 逐项搬，目标已存在的跳过（运行区数据优先）
    """
    import shutil

    if not os.path.exists(dst):
        try:
            os.rename(src, dst)
            return
        except OSError:
            os.makedirs(dst, exist_ok=True)
    else:
        os.makedirs(dst, exist_ok=True)

    for entry in os.listdir(src):
        s = os.path.join(src, entry)
        d = os.path.join(dst, entry)
        if os.path.exists(d) or os.path.islink(d):
            continue
        shutil.move(s, d)


def redirect_writable_dirs(app_name: str, version: str,
                           download_dir: str = "", apps_dir: str = "",
                           sub_dir: str = "") -> Dict[str, Any]:
    """
    把缓存区版本目录 app/ 下的「运行期可写目录」重定向到运行区。

    以 nacos 的 data 为例：
        改前   {download}/displayConsole/nacos/1.0.0/app/data/   ← 真实目录，应用写这里
        改后   {apps}/displayConsole/nacos/data/                 ← 真实目录，应用写这里
               {download}/displayConsole/nacos/1.0.0/app/data -> {apps}/displayConsole/nacos/data

    这样：日志/数据落在运行区；缓存区保持"包原始内容"语义，可随时清理重下；
    切版本时数据也不会跟着版本走（数据属于实例，不属于版本）。

    安全约束：
        · 不删数据 —— 缓存区里的真实目录先整体搬到运行区，再建软链；搬不动就跳过并报错
        · 运行区已有内容优先，逐项搬移时不覆盖
        · 任何失败只记日志，不影响 install 主流程

    返回: {"linked": [...], "moved": [...], "errors": [...]}
    """
    result: Dict[str, Any] = {"linked": [], "moved": [], "errors": []}

    download_dir = download_dir or get_download_dir()
    apps_dir = apps_dir or get_apps_dir()
    app_name = (app_name or "").strip()
    version = (version or "").strip()
    if not (download_dir and apps_dir and app_name and version):
        result["errors"].append("参数不完整，跳过可写目录重定向")
        return result

    cache_app_dir = os.path.join(
        _join_sub_dir(download_dir, sub_dir), app_name, version, "app")
    if not os.path.isdir(cache_app_dir):
        return result

    apps_app_dir = os.path.join(_join_sub_dir(apps_dir, sub_dir), app_name)
    os.makedirs(apps_app_dir, exist_ok=True)

    for name in get_writable_dir_names():
        src = os.path.join(cache_app_dir, name)
        dst = os.path.join(apps_app_dir, name)
        try:
            # 情况1：缓存区没有该目录 → 只建运行区目录 + 软链（应用运行时写进去）
            if not os.path.exists(src) and not os.path.islink(src):
                os.makedirs(dst, exist_ok=True)
                os.symlink(dst, src)
                result["linked"].append(name)
                logging.info("[路径] 可写目录重定向: %s -> %s", src, dst)
                continue

            # 情况2：已经是正确软链 → 什么都不用做
            if os.path.islink(src):
                if os.path.realpath(src) == os.path.realpath(dst):
                    continue
                remove_link_or_dir(src)
                os.makedirs(dst, exist_ok=True)
                os.symlink(dst, src)
                result["linked"].append(name)
                logging.info("[路径] 可写目录重定向(重建): %s -> %s", src, dst)
                continue

            # 情况3：缓存区是真实目录（包自带或历史运行产物）→ 搬到运行区后建软链
            os.makedirs(dst, exist_ok=True)
            _move_tree(src, dst)
            if os.path.exists(src) and not os.path.islink(src):
                remaining = os.listdir(src)
                if remaining:
                    # 还有搬不动的（权限等）→ 整体改名留档，避免直接删掉
                    aside = src + ".moved_aside"
                    remove_link_or_dir(aside)
                    os.rename(src, aside)
                    result["errors"].append(
                        f"{name}: 有 {len(remaining)} 项未能搬入运行区，已留档 {aside}")
                else:
                    remove_link_or_dir(src)
            os.symlink(dst, src)
            result["moved"].append(name)
            logging.info("[路径] 可写目录已搬到运行区并重定向: %s -> %s", src, dst)
        except Exception as e:
            result["errors"].append(f"{name}: {e}")
            logging.warning("[路径] 可写目录重定向失败 %s: %s", name, e)

    return result


def link_to_current(app_name: str, version: str,
                    download_dir: str = "", apps_dir: str = "",
                    sub_dir: str = "") -> Dict[str, Any]:
    """
    在运行区建立「current 版本指针 + app/bin/config 软链接」结构。

    目标结构：
        {apps}/{app_name}/
        ├── current -> {download}/{app_name}/{version}
        ├── app     -> current/app
        ├── bin     -> current/bin
        └── config  -> current/config

    app/bin/config 的链接目标写成 `current/xxx` 相对形式，
    因此切换版本时只需重建 current 一条链接，其余保持有效。

    参数:
        app_name:     应用名称
        version:      版本号
        download_dir: 缓存区根目录，留空则从配置读取
        apps_dir:     运行区根目录，留空则从配置读取

    返回:
        {"ok": bool, "error": str, "app_dir": str, "links": [...]}
    """
    download_dir = download_dir or get_download_dir()
    apps_dir = apps_dir or get_apps_dir()

    if not download_dir or not apps_dir:
        return {"ok": False, "error": "缓存区或运行区路径未配置",
                "app_dir": "", "links": []}

    app_name = (app_name or "").strip()
    version = (version or "").strip()
    if not app_name or not version:
        return {"ok": False, "error": "app_name 或 version 为空",
                "app_dir": "", "links": []}

    cache_version_dir = os.path.join(
        _join_sub_dir(download_dir, sub_dir), app_name, version)
    if not os.path.isdir(cache_version_dir):
        return {"ok": False,
                "error": f"缓存区版本目录不存在: {cache_version_dir}",
                "app_dir": "", "links": []}

    app_dir = os.path.join(_join_sub_dir(apps_dir, sub_dir), app_name)
    try:
        os.makedirs(app_dir, exist_ok=True)

        # 1. 重建 current 版本指针
        current_link = os.path.join(app_dir, CURRENT_LINK_NAME)
        remove_link_or_dir(current_link)
        os.symlink(cache_version_dir, current_link)
        logging.info("[路径] 建立版本指针: %s -> %s", current_link, cache_version_dir)

        # 2. 建立 app / bin / config 软链接（指向 current/xxx）
        links = []
        for name in LINK_DIR_NAMES:
            if not os.path.exists(os.path.join(cache_version_dir, name)):
                logging.info("[路径] 缓存区无 %s/ 子目录，跳过链接", name)
                continue
            link_path = os.path.join(app_dir, name)
            remove_link_or_dir(link_path)
            os.symlink(os.path.join(CURRENT_LINK_NAME, name), link_path)
            links.append(link_path)
            logging.info("[路径] 建立软链接: %s -> %s/%s",
                         link_path, CURRENT_LINK_NAME, name)

        # 3. 把「运行期可写目录」重定向到运行区
        #    否则应用按硬编码路径写日志/数据时会落到缓存区，
        #    一旦清理缓存重下，运行数据会被一起删掉。
        redirect = redirect_writable_dirs(app_name, version, download_dir, apps_dir, sub_dir)
        if redirect.get("errors"):
            logging.warning("[路径] 可写目录重定向有失败项: %s", redirect["errors"])

        return {"ok": True, "error": "", "app_dir": app_dir, "links": links,
                "redirect": redirect}

    except Exception as e:
        logging.error("[路径] 建立软链接失败: %s", e)
        return {"ok": False, "error": f"建立软链接失败: {e}",
                "app_dir": app_dir, "links": []}


def verify_links(app_name: str, sub_dir: str = "") -> List[str]:
    """
    校验运行区软链接是否可正常解析（防缓存区被清理后留下断链）。

    参数:
        app_name: 应用名称

    返回:
        失效链接路径列表；空列表表示全部正常
    """
    app_dir = find_apps_component_dir(app_name, sub_dir)
    if not app_dir:
        return []

    broken = []
    for name in list(LINK_DIR_NAMES) + [CURRENT_LINK_NAME]:
        link_path = os.path.join(app_dir, name)
        if os.path.islink(link_path) and not os.path.exists(link_path):
            broken.append(link_path)
    return broken
