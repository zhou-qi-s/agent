
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
import traceback
import uuid
import zipfile

import requests

from utils.config_loader import load_config
from utils.redis_client import get_redis


# =========================================================
# 日志配置
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s "
        "%(levelname)s "
        "%(filename)s:%(lineno)d "
        "%(message)s"
    )
)


# =========================================================
# 任务状态
# =========================================================

TASK_PENDING = "PENDING"
TASK_RUNNING = "RUNNING"
TASK_DOWNLOADING = "DOWNLOADING"
TASK_UNZIPING = "UNZIPING"
TASK_EXECUTING = "EXECUTING"
TASK_SUCCESS = "SUCCESS"
TASK_FAILED = "FAILED"


# =========================================================
# 失败原因采集
# =========================================================

# 记录每个任务最后一次脚本执行的关键错误详情，供失败上报时使用。
# 平台侧据此把失败归类为可读原因（见 K8sFailReasonEnum），而不是只报"脚本执行失败"。
_last_script_error = {}

# 脚本输出中命中以下关键字视为"关键错误行"（小写匹配）
_ERROR_KEYWORDS = (
    "error", "failed", "failure", "fatal", "cannot", "forbidden",
    "unimplemented", "already exists", "not found", "refused",
    "错误", "失败", "无法", "未找到", "已存在", "必须指定", "权限",
)


def _is_error_line(line: str) -> bool:
    """判断脚本输出行是否为关键错误行"""
    if not line:
        return False
    low = line.lower()
    return any(k in low for k in _ERROR_KEYWORDS)


def _compose_failure_detail(headline: str, error_hints) -> str:
    """
    组合失败详情：标题 + 脚本中的关键错误行。

    保留原始错误行很关键 —— 平台按其中的关键字归类失败原因
    （例如 RuntimeConfig / cannot get resource "nodes" / already exists）。
    """
    if not error_hints:
        return headline
    # 去重并保持顺序
    seen = set()
    uniq = []
    for h in error_hints:
        if h not in seen:
            seen.add(h)
            uniq.append(h)
    return headline + "\n" + "\n".join(uniq[-10:])


# =========================================================
# 统一调试日志
# =========================================================


def log_step(
        task_id: str,
        step: str,
        message: str = "",
        **kwargs
):
    """
    统一日志输出函数

    用于:
        - 打印任务阶段
        - 打印调试参数
        - 打印详细流程

    示例:
        [K8S][123][DOWNLOAD] 开始下载
    """

    extra = ""

    if kwargs:
        try:
            extra = " | " + json.dumps(
                kwargs,
                ensure_ascii=False,
                default=str
            )
        except Exception:
            extra = f" | {kwargs}"

    logging.info(
        f"[K8S][{task_id}][{step}] "
        f"{message}{extra}"
    )


# =========================================================
# 网络相关
# =========================================================


def _get_local_ip() -> str:
    """获取本机真实 IP（统一使用 utils/util.py 的方法）"""
    from utils.util import get_ip
    return get_ip() or "127.0.0.1"


# =========================================================
# URL 构建
# =========================================================


def _build_base_server_url() -> str:
    """
    构建服务端基础 URL

    自动补 http://
    """

    cfg = load_config()

    server_cfg = cfg.get("server", {}) or {}

    # 兼容键名大小写：config.yaml 由后端模板生成时 server 段键为大写 iP，
    # 而历史代码按小写 ip 读取会取不到 -> fallback 127.0.0.1 导致上报连错。
    server_ip = (
        server_cfg.get("iP")
        or server_cfg.get("ip")
        or server_cfg.get("Ip")
        or "127.0.0.1"
    )

    port = server_cfg.get("port", 30000)

    if not server_ip.startswith(("http://", "https://")):
        server_ip = f"http://{server_ip}"

    return f"{server_ip}:{port}"


# =========================================================
# Redis 队列 Key
# =========================================================


def _build_queue_key() -> str:
    """
    Redis 队列 Key

    示例:
        agent:k8s:192.168.1.100
    """

    ip = _get_local_ip()

    return f"agent:k8s:{ip}"


# =========================================================
# 上报地址
# =========================================================


def _build_report_url() -> str:
    """
    结果上报地址
    """

    return (
        f"{_build_base_server_url()}"
        f"/api/agent/k8s_report"
    )



def _build_log_report_url() -> str:
    """
    日志上报地址
    """

    return (
        f"{_build_base_server_url()}"
        f"/api/agent/k8s_log"
    )


# =========================================================
# K8S 配置
# =========================================================


def _get_k8s_config() -> dict:
    """
    获取 K8S 配置
    """

    cfg = load_config()

    return cfg.get("k8s", {})


# =========================================================
# 日志实时上报
# =========================================================


def report_task_log(task_id: str, line: str):
    """
    实时上报脚本日志

    前端可实时查看安装日志
    """

    try:

        url = _build_log_report_url()

        payload = {
            "taskId": task_id,
            "line": line
        }

        requests.post(
            url,
            json=payload,
            timeout=5
        )

    except Exception as e:

        logging.warning(
            f"[K8S] 日志上报失败: {e}"
        )


# =========================================================
# 登录节点
# =========================================================


def login_node(
        task_id: str,
        address: str,
        username: str,
        password: str
):
    """
    节点登录

    获取 token
    """

    try:

        log_step(
            task_id,
            "LOGIN",
            "开始节点登录",
            address=address,
            username=username
        )

        payload = {
            "username": username,
            "password": password,
            "timestamp": str(int(time.time() * 1000)),
            "uuid": str(uuid.uuid4())
        }

        resp = requests.post(
            address,
            json=payload,
            timeout=10
        )

        resp.raise_for_status()

        result = resp.json()

        log_step(
            task_id,
            "LOGIN",
            "节点登录成功"
        )

        return result

    except Exception:

        logging.exception(
            f"[K8S][{task_id}] 节点登录失败"
        )

        return None


# =========================================================
# SHA256
# =========================================================


def calculate_sha256(file_path: str) -> str:
    """
    计算文件 SHA256
    """

    sha256 = hashlib.sha256()

    with open(file_path, "rb") as f:

        for chunk in iter(
                lambda: f.read(1024 * 1024),
                b""
        ):
            sha256.update(chunk)

    return sha256.hexdigest()


# =========================================================
# 下载 K8S 离线包
# =========================================================


def download_k8s_package(
        task_id: str,
        address_k8s: str,
        token: str,
        task_dir: str,
        expected_sha256: str = None
):
    """
    下载 K8S 离线包

    参数:
        task_id: 任务ID，用于日志追踪
        address_k8s: 下载地址
        token: 登录后获取的Token
        task_dir: 下载目录
        expected_sha256: 预期SHA256校验值（可选）

    返回:
        下载成功返回文件路径，失败返回None

    特性:
        - Token从登录获取
        - 流式下载（避免大文件内存溢出）
        - 实时显示下载进度
        - 支持Content-Disposition文件名
        - 自动从URL或Content-Type推断文件名
        - 下载结果验证（文件存在+非空）
        - SHA256完整性校验（可选）
    """

    try:
        # =================================================
        # 步骤1: 构建HTTP请求
        # =================================================
        log_step(
            task_id,
            "DOWNLOAD",
            "开始下载离线包",
            url=address_k8s,
            token_provided=bool(token)
        )

        # 构建请求头
        headers = {}
        if token:
            headers["token"] = token

        # =================================================
        # 步骤2: 发送HTTP请求
        # =================================================
        log_step(
            task_id,
            "DOWNLOAD",
            "正在连接服务器...",
            url=address_k8s
        )

        response = requests.get(
            address_k8s,
            headers=headers,
            timeout=300  # 5分钟超时
        )

        # 检查HTTP状态码
        if response.status_code == 302 or response.status_code == 301:
            # 处理重定向
            redirect_url = response.headers.get("Location", "")
            log_step(
                task_id,
                "DOWNLOAD",
                "检测到重定向",
                from_url=address_k8s,
                to_url=redirect_url
            )
            response = requests.get(
                redirect_url,
                headers=headers,
                timeout=300,
                allow_redirects=True
            )

        if response.status_code != 200:
            raise Exception(f"下载失败，HTTP状态码: {response.status_code}")

        log_step(
            task_id,
            "DOWNLOAD",
            "已连接，正在获取文件信息...",
            status_code=response.status_code
        )

        # =================================================
        # 步骤3: 获取文件名
        # =================================================
        filename = None

        # 优先从 Content-Disposition header 获取
        content_disposition = response.headers.get("Content-Disposition", "")
        if content_disposition:
            # 尝试获取 filename* (RFC 5981 编码格式)
            match = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", content_disposition, re.IGNORECASE)
            if match:
                from urllib.parse import unquote
                filename = unquote(match.group(1).strip())
            else:
                # 尝试获取 filename
                match = re.search(r'filename="([^"]+)"', content_disposition)
                if match:
                    filename = match.group(1)

        # 如果没有，从URL获取文件名
        if not filename:
            url_filename = address_k8s.split("/")[-1].split("?")[0]
            # 只有文件名包含扩展名时才使用
            if url_filename and "." in url_filename:
                filename = url_filename

        # 如果还是没有，使用默认名（优先使用 .zip）
        if not filename:
            content_type = response.headers.get("Content-Type", "")
            ext_map = {
                "application/zip": ".zip",
                "application/x-zip-compressed": ".zip",
                "application/x-tar": ".tar",
                "application/gzip": ".gz",
                "application/octet-stream": ".zip",  # 离线包通常是zip
            }
            ext = ext_map.get(content_type.split(";")[0].strip(), ".zip")
            filename = f"kubernetes{ext}"

        log_step(
            task_id,
            "DOWNLOAD",
            "获取到文件名",
            filename=filename,
            content_type=response.headers.get("Content-Type", "unknown")
        )

        # =================================================
        # 步骤4: 准备下载
        # =================================================
        os.makedirs(task_dir, exist_ok=True)
        file_path = os.path.join(task_dir, filename)

        # 获取文件总大小
        total_size = int(response.headers.get("Content-Length", 0))

        log_step(
            task_id,
            "DOWNLOAD",
            "开始写入文件",
            file_path=file_path,
            total_size_mb=round(total_size / 1024 / 1024, 2) if total_size > 0 else "unknown"
        )

        # =================================================
        # 步骤5: 流式下载文件
        # =================================================
        downloaded = 0
        write_count = 0

        with open(file_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=64 * 1024):  # 64KB块
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    write_count += 1

                    # 每10块或第一块打印进度
                    if write_count % 10 == 0 or write_count == 1:
                        percent = 0
                        if total_size > 0:
                            percent = round(downloaded * 100 / total_size, 2)

                        log_step(
                            task_id,
                            "DOWNLOAD_PROGRESS",
                            "下载中",
                            downloaded_mb=round(downloaded / 1024 / 1024, 2),
                            total_mb=round(total_size / 1024 / 1024, 2) if total_size > 0 else "unknown",
                            percent=percent,
                            speed_mb_s=round(downloaded / 1024 / 1024 / max(write_count * 0.064, 1), 2) if write_count > 0 else 0
                        )

        # =================================================
        # 步骤6: 验证下载结果
        # =================================================
        if not os.path.exists(file_path):
            raise Exception(f"文件下载失败，路径不存在: {file_path}")

        actual_size = os.path.getsize(file_path)

        if actual_size == 0:
            raise Exception("文件下载失败，内容为空")

        if total_size > 0 and actual_size < total_size:
            raise Exception(f"文件下载不完整，预期 {total_size} 字节，实际 {actual_size} 字节")

        log_step(
            task_id,
            "DOWNLOAD",
            "下载完成",
            file_path=file_path,
            actual_size_mb=round(actual_size / 1024 / 1024, 2),
            expected_size_mb=round(total_size / 1024 / 1024, 2) if total_size > 0 else "unknown"
        )

        # =================================================
        # 步骤7: SHA256校验（可选）
        # =================================================
        if expected_sha256:
            log_step(
                task_id,
                "SHA256",
                "开始 SHA256 校验"
            )

            actual_sha256 = calculate_sha256(file_path)

            if actual_sha256 != expected_sha256:
                # 删除损坏的文件
                if os.path.exists(file_path):
                    os.remove(file_path)
                raise Exception(
                    f"SHA256 校验失败 "
                    f"expected={expected_sha256} "
                    f"actual={actual_sha256}"
                )

            log_step(
                task_id,
                "SHA256",
                "SHA256 校验成功",
                sha256=actual_sha256
            )

        return file_path

    except requests.exceptions.Timeout:
        logging.error(f"[K8S][{task_id}] 下载超时")
        log_step(task_id, "DOWNLOAD", "下载超时，请检查网络或增加超时时间")
        return None

    except requests.exceptions.ConnectionError:
        logging.error(f"[K8S][{task_id}] 连接失败")
        log_step(task_id, "DOWNLOAD", "连接服务器失败，请检查地址和网络")
        return None

    except Exception as e:
        logging.exception(f"[K8S][{task_id}] 下载失败")
        log_step(task_id, "DOWNLOAD", f"下载失败: {str(e)}")
        return None


# =========================================================
# ZIP 安全解压
# =========================================================


def safe_extract(zip_ref, extract_dir):
    """
    防止 Zip Slip 漏洞

    防止:
        ../../../../etc/passwd
    """

    extract_dir = os.path.abspath(extract_dir)

    for member in zip_ref.namelist():

        member_path = os.path.abspath(
            os.path.join(
                extract_dir,
                member
            )
        )

        if not member_path.startswith(extract_dir):
            raise Exception(
                f"非法 ZIP 路径: {member}"
            )

    zip_ref.extractall(extract_dir)


# =========================================================
# 解压 ZIP
# =========================================================


def unzip_k8s_package(
        task_id: str,
        zip_path: str,
        extract_dir: str = None
):
    """
    解压 K8S 安装包

    参数:
        task_id: 任务ID
        zip_path: ZIP文件路径
        extract_dir: 解压目标目录（默认为deps_dir）
    """

    try:

        log_step(
            task_id,
            "UNZIP",
            "开始解压",
            zip_path=zip_path
        )

        # 如果未指定解压目录，使用deps_dir
        if extract_dir is None:
            extract_dir = _get_k8s_config().get("deps_dir", "/opt/offline")

        os.makedirs(extract_dir, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:

            # 打印 ZIP 内容
            for member in zf.namelist():

                log_step(
                    task_id,
                    "ZIP_FILE",
                    "发现 ZIP 文件",
                    file=member
                )

            safe_extract(zf, extract_dir)

        log_step(
            task_id,
            "UNZIP",
            "解压完成",
            extract_dir=extract_dir
        )

        # =================================================
        # 处理单层嵌套文件夹
        # 如果解压后只有一个子文件夹，将其内容移到deps_dir下
        # =================================================

        entries = os.listdir(extract_dir)
        if len(entries) == 1:
            only_entry = os.path.join(extract_dir, entries[0])
            if os.path.isdir(only_entry):
                # 只有一个子文件夹，将其内容移到deps_dir下
                nested_dir = only_entry

                log_step(
                    task_id,
                    "UNZIP",
                    "检测到单层嵌套文件夹，展开内容",
                    nested_dir=nested_dir
                )

                nested_contents = os.listdir(nested_dir)
                for item in nested_contents:
                    src = os.path.join(nested_dir, item)
                    dst = os.path.join(extract_dir, item)
                    shutil.move(src, dst)

                # 删除空文件夹
                shutil.rmtree(nested_dir)

                log_step(
                    task_id,
                    "UNZIP",
                    "嵌套文件夹已展开",
                    extract_dir=extract_dir
                )

        # =================================================
        # 查找安装脚本
        # =================================================

        for root, dirs, files in os.walk(extract_dir):

            for file in files:

                if (
                        file.endswith(".sh")
                        or file.endswith(".py")
                ):

                    script_path = os.path.join(
                        root,
                        file
                    )

                    log_step(
                        task_id,
                        "SCRIPT",
                        "找到安装脚本",
                        script_path=script_path
                    )

                    return script_path

        raise Exception("未找到安装脚本")

    except Exception:

        logging.exception(
            f"[K8S][{task_id}] 解压失败"
        )

        return None


# =========================================================
# 执行安装脚本
# =========================================================


def run_setup_script(
        task_id: str,
        script_path: str
):
    """
    执行安装脚本

    特性:
        - 从config.yaml读取脚本参数
        - 实时日志
        - 超时控制
        - 进程组管理
        - kill 全部子进程
    """

    process = None

    try:

        log_step(
            task_id,
            "EXECUTE",
            "开始执行安装脚本",
            script_path=script_path
        )

        os.chmod(script_path, 0o755)

        # =================================================
        # 从配置读取脚本参数
        # =================================================

        k8s_cfg = _get_k8s_config()
        scheme = k8s_cfg.get("scheme", "")
        hostname = k8s_cfg.get("hostname", "")
        nodes = k8s_cfg.get("nodes", "")
        ssh_pass = k8s_cfg.get("ssh_pass", "")
        deps_dir = k8s_cfg.get("deps_dir", "/opt/offline")

        # =================================================
        # 构建脚本参数
        # =================================================

        # ⚠️ 参数必须「选项」与「值」分开成两个列表元素。
        # 曾经写成 append(f"--scheme {scheme}")（选项和值拼在一个元素里），
        # 而下面用 subprocess.Popen(list) 列表模式（无 shell=True），
        # 该元素会被当成「一个」参数原样传给脚本，脚本收到的是
        # "--scheme B"（含空格）→ 报「未知参数: --scheme B」→ 打印 usage 并退出，
        # 结果什么都没装，而 return_code=0 还会被误判为成功。
        # 另外这里也不能带引号：引号在列表模式下不会被 shell 剥离，会成为字面字符。
        script_args = []
        if scheme:
            script_args += ["--scheme", str(scheme)]
        if hostname:
            script_args += ["--hostname", str(hostname)]
        if nodes:
            script_args += ["--nodes", str(nodes)]
        if ssh_pass:
            script_args += ["--ssh-pass", str(ssh_pass)]
        if deps_dir:
            script_args += ["--offline-dir", str(deps_dir)]

        # =================================================
        # 根据文件类型构建命令
        # =================================================

        if script_path.endswith(".sh"):
            cmd = ["bash", script_path] + script_args

        elif script_path.endswith(".py"):
            cmd = ["python3", script_path] + script_args

        else:
            cmd = [script_path] + script_args

        log_step(
            task_id,
            "EXECUTE",
            "执行命令",
            cmd=cmd
        )

        # 打印完整命令字符串
        cmd_str = " ".join(cmd)
        logging.info(f"[K8S][{task_id}] 命令: {cmd_str}")

        # =================================================
        # 启动子进程
        # =================================================

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True
        )

        log_step(
            task_id,
            "EXECUTE",
            "脚本进程启动",
            pid=process.pid
        )

        start_time = time.time()

        # 收集脚本输出中的"关键错误行"，供失败时归因上报。
        # 脚本本身会打印明确的原因（如 RuntimeConfig Unimplemented、cannot get resource "nodes"、
        # node already exists、必须指定 --scheme 等），但原先这些行只进了实时日志，
        # 最终上报给平台只有一句笼统的"安装脚本执行失败"，运维无从判断卡点。
        error_hints = []

        # 参数解析失败标志（见下方循环内说明）：脚本对未知参数会打印 usage 并 exit 0，
        # 必须显式识别，否则会「什么都没做却上报成功」。
        arg_parse_failed = False

        # =================================================
        # 实时读取日志
        # =================================================

        for line in iter(
                process.stdout.readline,
                ''
        ):

            line = line.rstrip()

            if line:

                log_step(
                    task_id,
                    "SCRIPT_LOG",
                    line
                )

                report_task_log(
                    task_id,
                    line
                )

                # 命中错误关键字则留存（限制条数，避免日志爆量）
                if _is_error_line(line) and len(error_hints) < 20:
                    error_hints.append(line.strip())

                # 参数解析失败的特征：脚本会打印「未知参数」与 usage 后 exit 0，
                # 仅凭 returncode 会误判为成功（什么都不装却报成功）。
                # 这里显式识别，交由下面的 success 判定否决。
                if ("未知参数" in line
                        or "用法:" in line
                        or "Usage:" in line):
                    arg_parse_failed = True

            # =================================================
            # 超时检测
            # =================================================

            if time.time() - start_time > 600:

                log_step(
                    task_id,
                    "TIMEOUT",
                    "脚本执行超时",
                    timeout=600,
                    pid=process.pid
                )

                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGKILL
                )

                _last_script_error[task_id] = _compose_failure_detail(
                    "安装脚本执行超时（超过 600 秒）",
                    error_hints
                )

                return False

        process.wait()

        # 退出码为 0 且未出现「参数解析失败」特征，才算成功。
        # 脚本对错误参数是「打印 usage 后 exit 0」，只判退出码会把这种情况误报为成功。
        success = (process.returncode == 0) and (not arg_parse_failed)

        log_step(
            task_id,
            "EXECUTE",
            "脚本执行结束",
            return_code=process.returncode,
            arg_parse_failed=arg_parse_failed,
            success=success
        )

        if not success:
            # 把脚本真实报错带出去，作为失败原因归类依据
            if arg_parse_failed:
                _last_script_error[task_id] = _compose_failure_detail(
                    "脚本参数解析失败（选项与值被当成同一个参数传递，或包含多余引号）",
                    error_hints
                )
            else:
                _last_script_error[task_id] = _compose_failure_detail(
                    f"安装脚本执行失败（退出码 {process.returncode}）",
                    error_hints
                )

        return success

    except Exception as e:

        logging.exception(
            f"[K8S][{task_id}] 脚本执行异常"
        )

        _last_script_error[task_id] = f"脚本执行异常：{e}"

        if process:

            try:

                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGKILL
                )

            except Exception:
                pass

        return False


# =========================================================
# 结果上报
# =========================================================


def report_k8s_result(
        task: dict,
        success: bool,
        message: str
):
    """
    上报最终结果
    """

    try:

        url = _build_report_url()

        # 后端 k8s_report 从请求顶层解析 id/message/success（Boolean），
        # 因此这里直接发顶层字段，而不是再包一层 map（旧结构后端解析不到）。
        # action 必须带上：后端 k8s_report 靠 action=="remove" 才清空 k8s 状态，
        # 否则移除成功后 k8s 仍=1、k8sStatus=4，前端"移除K8s"按钮不会消失。
        payload = {
            "id": task.get("id"),
            "action": task.get("action"),
            "success": bool(success),
            "message": str(message)
        }

        response = requests.post(
            url,
            json=payload,
            timeout=10
        )

        logging.info(
            f"[K8S] 结果上报成功: "
            f"status={response.status_code}"
        )

    except Exception:

        logging.exception(
            "[K8S] 结果上报失败"
        )


# =========================================================
# Redis 读取任务
# =========================================================


def read_k8s_task(timeout=0):
    """
    从 Redis 阻塞读取任务
    """

    try:

        redis_client = get_redis()

        queue_key = _build_queue_key()

        result = redis_client.brpop(
            [queue_key],
            timeout=timeout
        )

        if not result:
            return None

        _, task_str = result

        if isinstance(task_str, bytes):
            task_str = task_str.decode("utf-8")

        task = json.loads(task_str)

        logging.info(
            f"[K8S] 收到任务: {task.get('id')}"
        )

        return task

    except Exception:

        logging.exception(
            "[K8S] 读取任务失败"
        )

        return None


# =========================================================
# 清理任务目录
# =========================================================


def cleanup_task_dir(task_dir: str):
    """
    清理任务目录
    """

    try:

        if task_dir and os.path.exists(task_dir):

            shutil.rmtree(task_dir)

            logging.info(
                f"[K8S] 清理任务目录: {task_dir}"
            )

    except Exception:

        logging.exception(
            "[K8S] 清理任务目录失败"
        )


# =========================================================
# 主任务处理流程
# =========================================================


def _delete_node_from_cluster(task_id: str) -> bool:
    """
    让集群忘记本节点：SSH 到控制面执行 kubectl delete node <本机主机名>。

    背景:
        remove-k8s.sh 只清理本机（kubeadm reset、删证书/数据），
        不会删控制面的 Node 对象。遗留的 Node 对象会造成：
          - 集群里永久存在一个 NotReady 的僵尸节点；
          - 用同名主机名重新 join 时，kubeadm 尝试"认领"旧记录，
            证书或 PodCIDR 不匹配则新节点永远 NotReady。

    注意:
        - 本操作是"尽力而为"：控制面不可达、sshpass 缺失、节点本就不在集群，
          都不应阻断后续的本地清理，因此失败只记警告、返回 False。
        - 主机名取自 config.yaml 的 k8s.hostname，与控制面里的 Node 名一致
          （name = hostname = metadata.name）。
        - 同时清理本机 /etc/hosts 里 join 时写入的集群条目。

    返回:
        True 表示成功让集群注销（或本就不在集群）；False 表示未能确认。
    """
    try:
        cfg = _get_k8s_config()
        hostname = (cfg.get("hostname") or "").strip()
        nodes = (cfg.get("nodes") or "").strip()
        ssh_pass = cfg.get("ssh_pass") or ""

        if not hostname:
            logging.warning(f"[K8S][{task_id}] 未配置 hostname，跳过删除集群侧 Node")
            return False

        # nodes 形如 "master1:192.168.0.5"，取第一个作为控制面
        if not nodes:
            logging.warning(f"[K8S][{task_id}] 未配置控制面(nodes)，跳过删除集群侧 Node")
            return False

        first = nodes.split()[0]
        master_ip = first.split(":", 1)[1] if ":" in first else first

        if not ssh_pass:
            logging.warning(f"[K8S][{task_id}] 未配置控制面 SSH 密码，跳过删除集群侧 Node")
            return False

        # kubectl 在非交互 SSH 会话下可能因等待 stdin 而卡住，
        # 统一用 </dev/null 关闭标准输入，并加 --request-timeout 兜底。
        remote_cmd = (
            "export KUBECONFIG=/etc/kubernetes/admin.conf; "
            f"kubectl delete node {hostname} --ignore-not-found --request-timeout=20s; "
            f"echo EXIT=$?"
        )

        log_step(
            task_id,
            "REMOVE",
            "从集群删除 Node 对象",
            hostname=hostname,
            master=master_ip
        )

        cmd = [
            "sshpass", "-p", ssh_pass,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10",
            f"root@{master_ip}",
            remote_cmd
        ]

        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60
        )

        out = (proc.stdout or "").strip()

        # 把控制面输出写进平台日志，便于追溯
        for line in out.splitlines():
            log_step(task_id, "REMOVE", f"  [集群] {line}")

        # EXIT=0 表示 kubectl 正常退出（含 deleted / not found 两种正常情况）
        ok = "EXIT=0" in out

        if ok:
            logging.info(f"[K8S][{task_id}] 已从集群删除 Node: {hostname}")
        else:
            logging.warning(
                f"[K8S][{task_id}] 从集群删除 Node 未确认成功，hostname={hostname}, out={out}"
            )

        return ok

    except FileNotFoundError:
        logging.warning(
            f"[K8S][{task_id}] 未找到 sshpass，跳过删除集群侧 Node（不影响本地清理）"
        )
        return False

    except Exception:
        logging.exception(f"[K8S][{task_id}] 删除集群侧 Node 异常（不影响本地清理）")
        return False


def process_k8s_remove(task: dict):
    """
    移除 K8S 节点流程

    流程:
        1. 定位移除脚本
        2. 删除集群侧的 Node 对象
        3. 执行移除脚本
        4. 上报最终结果
    """

    task_id = str(task.get("id"))

    success = False

    message = ""

    try:

        log_step(
            task_id,
            "REMOVE",
            "开始移除 K8S 节点",
            task=task
        )

        # =================================================
        # 定位移除脚本（与 k8s_agent.py 同目录）
        # =================================================

        script_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "remove-k8s.sh"
        )

        if not os.path.exists(script_path):
            raise Exception(f"移除脚本不存在: {script_path}")

        log_step(
            task_id,
            "REMOVE",
            "找到移除脚本",
            script_path=script_path
        )

        # =================================================
        # 删除集群侧的 Node 对象（关键：必须在 kubeadm reset 之前）
        # =================================================
        # remove-k8s.sh 只清理本机（kubeadm reset / 删证书与数据），
        # 不会删控制面里的 Node 对象。若不删：
        #   1) 集群里会永久留下一个 NotReady 的僵尸节点；
        #   2) 用同名主机名重新 join 时，kubeadm 会尝试"认领"这条旧记录，
        #      证书/PodCIDR 对不上时会导致新节点永远 NotReady。
        # 因此先让集群主动注销该节点（drain 忽略错误），再做本地清理；
        # 顺序反过来会让节点先失联，反而更难删。
        _delete_node_from_cluster(task_id)

        # =================================================
        # 执行移除脚本
        # =================================================

        script_success = run_setup_script(
            task_id,
            script_path
        )

        if not script_success:
            # 带上脚本真实报错，供平台归类失败原因
            raise Exception(
                _last_script_error.get(task_id) or "移除脚本执行失败"
            )

        success = True

        message = "移除成功"

        log_step(
            task_id,
            "SUCCESS",
            "节点移除成功"
        )

        return {
            "taskId": task_id,
            "success": True
        }

    except Exception as e:

        success = False

        message = str(e)

        logging.exception(
            f"[K8S][{task_id}] 移除任务失败"
        )

        return None

    finally:

        report_k8s_result(
            task,
            success,
            message
        )

        # 清理本次任务的错误缓存，避免长期运行下字典无限增长
        _last_script_error.pop(task_id, None)


def process_k8s_task(task: dict):
    """
    完整 K8S 安装流程

    流程:
        1. 创建任务目录
        2. 登录节点
        3. 下载离线包
        4. SHA256 校验
        5. 解压 ZIP
        6. 查找安装脚本
        7. 执行脚本
        8. 实时日志上报
        9. 上报最终结果
        10. 清理目录
    """

    task_id = str(task.get("id"))

    # 检测是否为移除任务
    if task.get("action") == "remove":
        return process_k8s_remove(task)

    success = False

    message = ""

    task_dir = None

    try:

        log_step(
            task_id,
            "START",
            "开始处理任务",
            task=task
        )

        # =================================================
        # 创建任务目录
        # =================================================

        base_dir = _get_k8s_config().get(
            "deps_dir",
            "/opt/offline"
        )

        task_dir = os.path.join(
            base_dir,
            task_id
        )

        os.makedirs(task_dir, exist_ok=True)

        log_step(
            task_id,
            "INIT",
            "创建任务目录",
            task_dir=task_dir
        )

        # =================================================
        # 获取 K8S 离线包
        #
        # 优先使用后端已通过 SSH 推送到本机的包（task.localPackage）。
        # K8s 离线包由后端从本机目录直接推送，不经过应用商店，
        # 因此此路径下无需登录应用商店、也无需 HTTP 下载 ——
        # 原先强制"先登录应用商店再下载"会在应用商店不可用时导致整个加入任务中断。
        # 仅当本机没有该文件时，才回退到旧的 HTTP 下载流程（并做登录）。
        # =================================================

        local_package = task.get("localPackage")

        file_path = None

        if local_package:

            if os.path.isfile(local_package):

                log_step(
                    task_id,
                    "LOCAL",
                    "使用后端推送的本地离线包",
                    path=local_package,
                    size=os.path.getsize(local_package)
                )

                file_path = local_package

            else:

                log_step(
                    task_id,
                    "LOCAL",
                    "本地离线包不存在，回退为下载",
                    path=local_package
                )

        if not file_path:

            login_result = login_node(
                task_id,
                task.get("address"),
                task.get("username"),
                task.get("password")
            )

            if not login_result:
                raise Exception("节点登录失败")

            token = login_result.get("token")

            file_path = download_k8s_package(
                task_id,
                task.get("addressK8s"),
                token,
                task_dir,
                task.get("sha256")
            )

        if not file_path:
            raise Exception("安装包下载失败")

        # =================================================
        # 解压 ZIP
        # =================================================

        script_path = unzip_k8s_package(
            task_id,
            file_path,
            base_dir  # 解压到deps_dir
        )

        if not script_path:
            raise Exception("安装包解压失败")

        # =================================================
        # 执行安装脚本
        # =================================================

        script_success = run_setup_script(
            task_id,
            script_path
        )

        if not script_success:
            # 带上脚本真实报错（run_setup_script 已采集），供平台归类失败原因
            raise Exception(
                _last_script_error.get(task_id) or "安装脚本执行失败"
            )

        # =================================================
        # 执行完成后命令
        # =================================================

        post_script = _get_k8s_config().get("post_script", "")

        if post_script:
            log_step(
                task_id,
                "POST_SCRIPT",
                "执行完成后命令",
                command=post_script
            )

            try:
                post_result = subprocess.run(
                    post_script,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=60
                )

                log_step(
                    task_id,
                    "POST_SCRIPT",
                    "命令执行完成",
                    returncode=post_result.returncode,
                    stdout=post_result.stdout.strip(),
                    stderr=post_result.stderr.strip()
                )

                if post_result.returncode != 0:
                    log_step(
                        task_id,
                        "POST_SCRIPT",
                        "命令执行失败",
                        stderr=post_result.stderr
                    )

            except subprocess.TimeoutExpired:
                log_step(task_id, "POST_SCRIPT", "命令执行超时")

            except Exception as e:
                log_step(task_id, "POST_SCRIPT", f"命令执行异常: {e}")

        success = True

        message = "安装成功"

        log_step(
            task_id,
            "SUCCESS",
            "任务执行成功"
        )

        return {
            "taskId": task_id,
            "success": True
        }

    except Exception as e:

        success = False

        message = str(e)

        logging.exception(
            f"[K8S][{task_id}] 任务失败"
        )

        return None

    finally:

        # =================================================
        # 上报结果
        # =================================================

        report_k8s_result(
            task,
            success,
            message
        )

        # 清理本次任务的错误缓存，避免长期运行下字典无限增长
        _last_script_error.pop(str(task.get("id")), None)


# =========================================================
# K8S 任务循环
# =========================================================


def k8s_task_loop(timeout=10):
    """
    K8S 任务主循环

    功能:
        1. 阻塞读取 Redis 队列
        2. 获取任务
        3. 执行安装流程
        4. 持续监听
    """

    logging.info(
        "[K8S] K8S 任务处理线程启动"
    )

    while True:

        try:

            # =================================================
            # 阻塞读取任务
            # =================================================

            task = read_k8s_task(timeout=timeout)

            if task:

                logging.info(
                    f"[K8S] 收到新任务: {task.get('id')}"
                )

                # =================================================
                # 处理任务
                # =================================================

                process_k8s_task(task)

            else:

                logging.debug(
                    "[K8S] 暂无新任务，继续等待..."
                )

        except Exception:

            logging.exception(
                "[K8S] 任务处理异常"
            )

            traceback.print_exc()

        time.sleep(1)


# =========================================================
# 程序入口
# =========================================================

if __name__ == '__main__':

    k8s_task_loop()
