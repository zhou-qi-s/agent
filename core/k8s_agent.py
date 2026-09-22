
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

        # Harbor 的「启动/停止/重启」需要把 op 带回，后端据此决定最终状态
        # （stop 成功 → 已停止；start/restart 成功 → 部署成功）
        if task.get("op"):
            payload["op"] = task.get("op")

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


def _comment_out_https_section(content: str) -> str:
    """
    把 harbor.yml 中的顶层 `https:` 段整体注释掉，使其以 HTTP 模式运行。

    背景：Harbor 官方 harbor.yml.tmpl 里 `https:` 是**激活**的，且
    certificate/private_key 为占位符（/your/certificate/path）。
    若不注释，prepare 阶段会报：
        "The protocol is https but attribute ssl_cert is not set"
    直接导致安装失败。

    处理范围：从顶层 `https:` 行开始，到下一个「顶格且非注释」的键或文件结束为止。
    逐行在行首加 `# `，保持缩进不变（YAML 注释后不再解析，缩进不影响）。
    """

    lines = content.splitlines()

    out = []
    inside = False

    for line in lines:
        stripped = line.strip()

        if not inside:
            # 命中顶层 https: 段起点
            if re.match(r"^https:\s*(#.*)?$", line):
                inside = True
                out.append("# " + line)
                continue
            out.append(line)
            continue

        # inside == True：判断段落是否结束
        # 结束条件：非空、非注释、且顶格（缩进为 0）的新键
        if stripped and not stripped.startswith("#") and not line.startswith((" ", "\t")):
            inside = False
            out.append(line)
            continue

        out.append("# " + line)

    return "\n".join(out) + ("\n" if content.endswith("\n") else "")


def _generate_harbor_certs(task_id: str, host_ip: str, cert_dir: str,
                           ca_source: str = "") -> dict:
    """
    为 Harbor 生成 HTTPS 自签证书（一套根 CA + 本节点服务端证书）。

    设计（重要）：
        · **一套根 CA** 是长期资产，客户端只需装一次 `ca.crt` 即可信任所有 Harbor 节点；
        · **每台节点单独签一张服务端证书**，SAN 写节点自身 IP，私钥各节点独立
          （避免同一私钥散落在多台机器上）。

    存放结构（目标机）：
        {cert_dir}/
        ├── ca.crt / ca.key          根 CA（根私钥仅本机保留，权限 600）
        └── {host_ip}.crt / .key     本节点服务端证书

    参数：
        cert_dir  : 证书目录，通常与 harbor.yml 同目录（Harbor 会读相对路径）
        ca_source : 若指定且存在，则复用外部 CA（例如平台统一分发的 ca.crt/ca.key
                    两个文件所在目录）；否则在本机自建一套。

    返回：{"ca_crt":..., "cert":..., "key":...}，失败抛异常。
    """

    os.makedirs(cert_dir, exist_ok=True)
    os.chmod(cert_dir, 0o700)

    ca_crt = os.path.join(cert_dir, "ca.crt")
    ca_key = os.path.join(cert_dir, "ca.key")
    srv_crt = os.path.join(cert_dir, f"{host_ip}.crt")
    srv_key = os.path.join(cert_dir, f"{host_ip}.key")
    csr_path = os.path.join(cert_dir, f"{host_ip}.csr")
    ext_path = os.path.join(cert_dir, "v3.ext")

    # ---------- 1. 准备根 CA ----------
    if ca_source and os.path.exists(os.path.join(ca_source, "ca.crt")) \
            and os.path.exists(os.path.join(ca_source, "ca.key")):
        # 复用平台统一分发的 CA
        shutil.copyfile(os.path.join(ca_source, "ca.crt"), ca_crt)
        shutil.copyfile(os.path.join(ca_source, "ca.key"), ca_key)
        log_step(task_id, "HARBOR", "复用平台分发的根 CA", source=ca_source)
    elif not (os.path.exists(ca_crt) and os.path.exists(ca_key)):
        log_step(task_id, "HARBOR", "生成新的根 CA")
        ok, out = _run_local([
            "openssl", "genrsa", "-out", ca_key, "4096"
        ], timeout=300)
        if not ok:
            raise Exception(f"生成 CA 私钥失败: {out}")

        ok, out = _run_local([
            "openssl", "req", "-x509", "-new", "-nodes",
            "-key", ca_key, "-sha256", "-days", "3650",
            "-out", ca_crt,
            "-subj", "/C=CN/ST=Local/L=Local/O=Harbor/OU=Dev/CN=HarborRootCA",
        ], timeout=300)
        if not ok:
            raise Exception(f"生成 CA 证书失败: {out}")
        os.chmod(ca_key, 0o600)
        log_step(task_id, "HARBOR", "根 CA 生成完成", ca=ca_crt)
    else:
        log_step(task_id, "HARBOR", "复用本机已有根 CA", ca=ca_crt)

    # ---------- 2. 服务端证书 ----------
    # SAN 必须包含节点自身 IP，否则客户端按 IP 访问时校验失败
    san = f"IP:{host_ip},DNS:harbor.local,DNS:localhost,IP:127.0.0.1"

    with open(ext_path, "w", encoding="utf-8") as f:
        f.write(
            f"subjectAltName = {san}\n"
            "extendedKeyUsage = serverAuth\n"
            "keyUsage = digitalSignature, keyEncipherment\n"
        )

    log_step(task_id, "HARBOR", "生成本节点服务端证书", host=host_ip, san=san)

    ok, out = _run_local(["openssl", "genrsa", "-out", srv_key, "4096"], timeout=300)
    if not ok:
        raise Exception(f"生成服务端私钥失败: {out}")

    ok, out = _run_local([
        "openssl", "req", "-new",
        "-key", srv_key, "-out", csr_path,
        "-subj", f"/C=CN/ST=Local/L=Local/O=Harbor/OU=Dev/CN={host_ip}",
    ], timeout=300)
    if not ok:
        raise Exception(f"生成 CSR 失败: {out}")

    ok, out = _run_local([
        "openssl", "x509", "-req",
        "-in", csr_path, "-CA", ca_crt, "-CAkey", ca_key, "-CAcreateserial",
        "-out", srv_crt, "-days", "3650", "-sha256",
        "-extfile", ext_path,
    ], timeout=300)
    if not ok:
        raise Exception(f"签发服务端证书失败: {out}")
    os.chmod(srv_key, 0o600)

    # ---------- 3. 校验 ----------
    ok, out = _run_local([
        "openssl", "x509", "-in", srv_crt, "-noout", "-subject", "-ext", "subjectAltName"
    ], timeout=60)
    if not ok:
        raise Exception(f"服务端证书校验失败: {out}")

    log_step(task_id, "HARBOR", "证书签发完成",
             subject=out.splitlines()[0] if out else "", cert=srv_crt)

    return {"ca_crt": ca_crt, "cert": srv_crt, "key": srv_key}


def _remove_ca_trust(task_id: str, host_ip, harbor_port) -> bool:
    """
    卸载 Harbor 时移除本机对该 CA 的信任（docker 证书目录 + 系统信任库）。

    不重启 docker：证书目录变更为「少了信任源」，对已有容器无影响，
    下次 docker 重启自然生效；重启反而会打断无关容器。
    """

    removed = []

    if host_ip and harbor_port:
        d_dir = f"/etc/docker/certs.d/{host_ip}:{harbor_port}"
        if os.path.isdir(d_dir):
            shutil.rmtree(d_dir, ignore_errors=True)
            removed.append(d_dir)

    sys_crt = "/usr/local/share/ca-certificates/harbor-ca.crt"
    if os.path.exists(sys_crt):
        os.remove(sys_crt)
        _run_local(["update-ca-certificates", "--fresh"], timeout=180)
        removed.append(sys_crt)

    if removed:
        log_step(task_id, "HARBOR", "已移除 CA 信任", paths=removed)
    else:
        log_step(task_id, "HARBOR", "无 CA 信任需要清理")
    return True


def _trust_ca_for_docker(task_id: str, ca_crt: str, host_ip: str, harbor_port) -> bool:
    """
    让本机 docker 信任 Harbor 的自签根 CA（HTTPS 模式必需）。

    为什么需要：docker 对 registry 的证书校验走自己的目录
        /etc/docker/certs.d/{host}:{port}/ca.crt
    不放这张 CA，push/pull 会报：
        x509: certificate signed by unknown authority

    说明：
        · 同时写入系统信任库（/usr/local/share/ca-certificates + update-ca-certificates），
          让 curl/wget 等工具也能访问；
        · **不再需要 insecure-registries**（HTTPS 模式下它是多余的，
          且会削弱证书校验的意义）。
    """

    if not os.path.exists(ca_crt):
        log_step(task_id, "HARBOR", "CA 证书不存在，跳过 docker 信任配置", ca=ca_crt)
        return False

    if not host_ip or not harbor_port:
        log_step(task_id, "HARBOR", "缺少 host/port，跳过 docker 信任配置")
        return False

    try:
        # 1) docker 专用证书目录：目录名必须是 host:port
        d_dir = f"/etc/docker/certs.d/{host_ip}:{harbor_port}"
        os.makedirs(d_dir, exist_ok=True)
        shutil.copyfile(ca_crt, os.path.join(d_dir, "ca.crt"))
        log_step(task_id, "HARBOR", "已配置 docker 信任 CA", dir=d_dir)

        # 2) 系统信任库（顺带让 curl 等工具可用）
        sys_dir = "/usr/local/share/ca-certificates"
        os.makedirs(sys_dir, exist_ok=True)
        sys_crt = os.path.join(sys_dir, "harbor-ca.crt")
        shutil.copyfile(ca_crt, sys_crt)
        ok, out = _run_local(["update-ca-certificates"], timeout=180)
        if ok:
            log_step(task_id, "HARBOR", "已安装到系统信任库", file=sys_crt)
        else:
            log_step(task_id, "HARBOR", "update-ca-certificates 失败（不阻断）", detail=out[:200])

        # 3) 重启 docker 使证书目录生效
        #    ⚠️ 此时 Harbor 尚未安装，重启 docker 无副作用
        ok, out = _run_local(["systemctl", "restart", "docker"], timeout=300)
        if not ok:
            ok2, out2 = _run_local("service docker restart", shell=True, timeout=300)
            if not ok2:
                raise Exception(f"docker 重启失败: {out2}")

        for _ in range(30):
            ok3, _o = _run_local(["docker", "info"], timeout=30)
            if ok3:
                break
            time.sleep(2)

        ok4, ver = _run_local(["docker", "version", "--format", "{{.Server.Version}}"], timeout=60)
        if not ok4:
            raise Exception("docker 重启后不可用")

        log_step(task_id, "HARBOR", "docker 已重启，CA 信任生效", version=ver.strip())
        return True

    except Exception as e:
        # 不阻断安装：Harbor 本体仍能起，只是本机 push 需要人工补配置
        log_step(task_id, "HARBOR", "配置 CA 信任失败（不阻断安装）", error=str(e))
        return False


def _remove_docker_insecure_registry(task_id: str, host_ip, harbor_port) -> bool:
    """
    从 docker 的 insecure-registries 中移除本机 Harbor 地址（卸载时调用）。

    注意：**不重启 docker** —— 卸载场景下重启会打断其它无关容器，
    而 daemon.json 的改动在下次 docker 重启时自然生效，无副作用。
    """

    daemon_json = "/etc/docker/daemon.json"
    if not os.path.exists(daemon_json):
        return False

    try:
        with open(daemon_json, "r", encoding="utf-8") as f:
            txt = f.read().strip()
        if not txt:
            return False
        current = json.loads(txt)
    except Exception as e:
        log_step(task_id, "HARBOR", "daemon.json 读取失败，跳过清理", error=str(e))
        return False

    existing = current.get("insecure-registries") or []
    if not isinstance(existing, list) or not existing:
        return False

    targets = set()
    if host_ip and harbor_port:
        targets.add(f"{host_ip}:{harbor_port}")
        targets.add(f"127.0.0.1:{harbor_port}")

    kept = [x for x in existing if x not in targets]
    if len(kept) == len(existing):
        log_step(task_id, "HARBOR", "insecure-registries 无需清理")
        return True

    if kept:
        current["insecure-registries"] = kept
    else:
        current.pop("insecure-registries", None)

    with open(daemon_json, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)

    log_step(task_id, "HARBOR", "已从 insecure-registries 移除 Harbor 地址",
             removed=list(targets), kept=kept)
    return True


def _restart_harbor_if_present(task_id: str) -> bool:
    """
    若目标机上已有 Harbor 部署目录，用 compose 把它重新拉起。

    为什么需要：`systemctl restart docker` 会终止所有运行中的容器。
    Harbor 的容器由 compose 管理，重启 docker 后不会全部自动恢复
    （实测会以 Exit 128 退出，只剩 core/log 两个，端口不再监听）。
    因此在重启 docker 后必须显式 `docker compose up -d` 复原。

    前提：调用方已知道 installDir；这里用固定默认值探测，
    未找到部署目录则静默跳过（属首次安装路径的常态）。
    """

    candidates = ["/opt/harbor"]
    for d in candidates:
        compose = os.path.join(d, "docker-compose.yml")
        if not os.path.exists(compose):
            continue

        log_step(task_id, "HARBOR", "检测到已有 Harbor 部署，重启 docker 后正在恢复",
                 dir=d)

        ok, out = _run_local(
            ["docker", "compose", "up", "-d"],
            cwd=d,
            timeout=900,
        )
        if not ok:
            ok2, out2 = _run_local(
                ["docker-compose", "up", "-d"],
                cwd=d,
                timeout=900,
            )
            if not ok2:
                log_step(task_id, "HARBOR", "恢复 Harbor 失败（不阻断）", error=out2)
                return False

        # 等待端口重新监听
        for _ in range(24):
            time.sleep(5)
            _ok, ps = _run_local(
                "docker ps --filter 'status=running' --format '{{.Names}}' | grep -qi harbor && echo UP || echo DOWN",
                shell=True,
                timeout=30,
            )
            if "UP" in (ps or ""):
                log_step(task_id, "HARBOR", "Harbor 已恢复运行")
                return True

        log_step(task_id, "HARBOR", "Harbor 恢复命令已执行，但容器未全部就绪",
                 detail=(out or "")[:300])
        return False

    return False


def _ensure_docker_insecure_registry(task_id: str, host_ip: str, harbor_port) -> bool:
    """
    把本机 Harbor 地址写入 docker 的 insecure-registries，使 HTTP 模式的 Harbor 可被 docker 推送/拉取。

    为什么必须做：Harbor 以 HTTP 提供 registry 服务时，docker 默认按 HTTPS 访问，
    会报：
        Get "https://<host>:<port>/v2/": http: server gave HTTP response to HTTPS client
    表现为「Harbor 装好了但一个镜像都推不进去」，功能等于不可用。

    做法：
        1. 读取或创建 /etc/docker/daemon.json；
        2. 若 insecure-registries 已包含该地址则跳过（幂等）；
        3. 否则追加并重启 docker（Harbor 此时尚未安装，重启 docker 无影响）。

    重启 docker 的时机放在 Harbor 安装之前，避免打断刚起来的 Harbor 容器。
    """

    if not host_ip or not harbor_port:
        log_step(task_id, "HARBOR", "跳过 insecure-registries 配置（缺少 hostname 或 port）")
        return False

    # 同时写入「ip:port」两种常见写法，保证不同 docker 版本都能识别
    entries = [f"{host_ip}:{harbor_port}"]

    daemon_json = "/etc/docker/daemon.json"

    try:
        current = {}
        if os.path.exists(daemon_json):
            try:
                with open(daemon_json, "r", encoding="utf-8") as f:
                    txt = f.read().strip()
                if txt:
                    current = json.loads(txt)
            except Exception as e:
                log_step(task_id, "HARBOR", "daemon.json 解析失败，将重建",
                         error=str(e))
                current = {}

        existing = current.get("insecure-registries") or []
        if not isinstance(existing, list):
            existing = []

        added = [e for e in entries if e not in existing]
        if not added:
            log_step(task_id, "HARBOR", "docker 已信任该 Harbor 地址，无需修改",
                     registries=existing)
            return True

        merged = existing + added
        # 去重并保持稳定顺序
        seen = set()
        merged = [x for x in merged if not (x in seen or seen.add(x))]

        current["insecure-registries"] = merged

        os.makedirs(os.path.dirname(daemon_json), exist_ok=True)
        with open(daemon_json, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2, ensure_ascii=False)

        log_step(task_id, "HARBOR", "已写入 docker insecure-registries",
                 file=daemon_json, registries=merged)

        # 重启 docker 使其生效（此时 Harbor 还没装，不会影响其容器）
        ok, out = _run_local(["systemctl", "restart", "docker"], timeout=300)
        if not ok:
            # 非 systemd 环境兜底
            ok2, out2 = _run_local("service docker restart", shell=True, timeout=300)
            if not ok2:
                raise Exception(f"docker 重启失败: {out2}")

        # 等待 docker 就绪
        for _ in range(30):
            ok3, _o = _run_local(["docker", "info"], timeout=30)
            if ok3:
                break
            time.sleep(2)

        ok4, ver = _run_local(["docker", "version", "--format", "{{.Server.Version}}"], timeout=60)
        if not ok4:
            raise Exception("docker 重启后不可用，请检查 docker 服务状态")

        log_step(task_id, "HARBOR", "docker 已重启并生效",
                 server_version=ver.strip())

        # ⚠️ 关键：`systemctl restart docker` 会把所有运行中的容器杀掉
        # （Harbor 的 compose 容器会以 Exit 128 退出）。
        # 因此在「已装 Harbor 再重装/修复」的场景下，必须把 Harbor 重新拉起，
        # 否则重启 docker 反而把好好的服务弄挂了。
        _restart_harbor_if_present(task_id)

        return True

    except Exception as e:
        # 该步骤失败不应阻断 Harbor 安装，但要明确记录（Harbor 本体仍可用，
        # 只是本机 docker 推送需要人工补配置）
        log_step(task_id, "HARBOR", "配置 insecure-registries 失败（不阻断安装）",
                 error=str(e))
        return False


def process_harbor_install(task: dict):
    """
    HARBOR 节点：安装 Harbor 与 Helm

    由后端先行检测并推包，本函数只负责执行本机的安装脚本：
        1. 解压后端推送过来的离线包（harbor / helm）
        2. 安装 harbor：进入 installDir 执行 install.sh
        3. 安装 helm：解压后把二进制拷到 installPath
        4. 上报最终结果

    任务字段（由后端 install_harbor 接口下发）:
        id            : 节点ID
        action        : install_harbor
        arch          : uname -m 结果
        needHarbor    : 是否需要装 harbor
        needHelm      : 是否需要装 helm
        harborPackage : harbor 离线包在目标机上的绝对路径（可选）
        helmPackage   : helm 离线包在目标机上的绝对路径（可选）
        installDir    : harbor 安装目录，如 /opt/harbor
        helmInstallPath: helm 二进制安装路径，如 /usr/local/bin/helm
    """

    task_id = str(task.get("id"))

    success = False

    message = ""

    try:

        log_step(
            task_id,
            "HARBOR",
            "开始安装 Harbor / Helm",
            task=task
        )

        need_harbor = bool(task.get("needHarbor"))
        need_helm = bool(task.get("needHelm"))
        need_compose = bool(task.get("needCompose"))

        if not need_harbor and not need_helm:
            success = True
            message = "Harbor 与 Helm 均已安装，无需处理"
            return {"taskId": task_id, "success": True}

        # =================================================
        # 0) 安装 docker compose 插件
        # =================================================
        # Harbor 的 install.sh 会调 common.sh，其中强制校验 docker compose
        # 或 docker-compose 至少一个可用，否则直接 error 退出。
        # 因此必须在跑 install.sh 之前把 compose 补齐。

        if need_compose:
            compose_pkg = (task.get("composePackage") or "").strip()
            compose_install_path = (
                task.get("composeInstallPath")
                or "/usr/libexec/docker/cli-plugins/docker-compose"
            ).strip()

            if not compose_pkg:
                raise Exception("未收到 docker-compose 插件包路径")

            if not os.path.exists(compose_pkg):
                raise Exception(f"docker-compose 插件包不存在: {compose_pkg}")

            log_step(task_id, "HARBOR", "开始安装 docker compose 插件",
                     package=compose_pkg)

            dest_dir = os.path.dirname(compose_install_path)
            os.makedirs(dest_dir, exist_ok=True)

            ok, out = _run_local(["cp", "-f", compose_pkg, compose_install_path])
            if not ok:
                raise Exception(f"docker-compose 插件拷贝失败: {out}")
            os.chmod(compose_install_path, 0o755)

            # 兼容旧写法：同时提供 /usr/bin/docker-compose 软链，
            # 某些脚本/文档仍按老命令调用
            if compose_install_path != "/usr/bin/docker-compose":
                _run_local(
                    ["ln", "-sf", compose_install_path, "/usr/bin/docker-compose"]
                )

            ok, out = _run_local(["docker", "compose", "version"])
            if not ok:
                raise Exception(f"docker-compose 插件安装后校验失败: {out}")

            log_step(task_id, "HARBOR", "docker compose 插件安装完成",
                     version=out.strip().splitlines()[0] if out.strip() else "")

        # =================================================
        # 1) 安装 Helm
        # =================================================

        if need_helm:
            helm_pkg = (task.get("helmPackage") or "").strip()
            helm_install_path = (task.get("helmInstallPath") or "/usr/local/bin/helm").strip()

            if not helm_pkg:
                raise Exception("未收到 Helm 离线包路径")

            if not os.path.exists(helm_pkg):
                raise Exception(f"Helm 离线包不存在: {helm_pkg}")

            log_step(task_id, "HARBOR", "开始安装 Helm", package=helm_pkg)

            extract_dir = os.path.join("/tmp", f"helm-{task_id}")
            os.makedirs(extract_dir, exist_ok=True)
            ok, out = _run_local(
                ["tar", "-xzf", helm_pkg, "-C", extract_dir]
            )
            if not ok:
                raise Exception(f"Helm 包解压失败: {out}")

            # helm-vX.Y.Z-linux-amd64/linux-amd64/helm
            bin_src = None
            for root, _dirs, files in os.walk(extract_dir):
                if "helm" in files:
                    bin_src = os.path.join(root, "helm")
                    break
            if not bin_src:
                raise Exception("Helm 包内未找到 helm 二进制")

            os.makedirs(os.path.dirname(helm_install_path), exist_ok=True)
            ok, out = _run_local(["cp", "-f", bin_src, helm_install_path])
            if not ok:
                raise Exception(f"Helm 二进制拷贝失败: {out}")
            os.chmod(helm_install_path, 0o755)

            ok, out = _run_local([helm_install_path, "version", "--short"])
            if not ok:
                raise Exception(f"Helm 安装后校验失败: {out}")

            log_step(task_id, "HARBOR", "Helm 安装完成", version=out.strip())

        # =================================================
        # 2) 安装 Harbor
        # =================================================

        if need_harbor:
            harbor_pkg = (task.get("harborPackage") or "").strip()
            install_dir = (task.get("installDir") or "/opt/harbor").strip()

            if not harbor_pkg:
                raise Exception("未收到 Harbor 离线包路径")

            if not os.path.exists(harbor_pkg):
                raise Exception(f"Harbor 离线包不存在: {harbor_pkg}")

            log_step(task_id, "HARBOR", "开始安装 Harbor", package=harbor_pkg)

            parent = os.path.dirname(install_dir.rstrip("/")) or "/opt"

            # 清理上次残留：上次安装失败会留下半套目录（只有 harbor.yml、容器全无），
            # 直接在其上重新解压会因文件冲突/状态错乱导致 install.sh 失败。
            # 注意：只在目录存在但服务未运行时清理；正常运行中的 Harbor 不会走到这里
            # （后端已判定 needHarbor=false 才跳过）。
            if os.path.isdir(install_dir):
                log_step(task_id, "HARBOR", "清理上次残留目录", path=install_dir)
                # 先尝试停掉可能存在的容器（忽略失败）
                _run_local(
                    ["docker", "compose", "down", "-v"],
                    cwd=install_dir,
                    timeout=300,
                )
                shutil.rmtree(install_dir, ignore_errors=True)

            os.makedirs(parent, exist_ok=True)
            ok, out = _run_local(
                ["tar", "-xzf", harbor_pkg, "-C", parent]
            )
            if not ok:
                raise Exception(f"Harbor 包解压失败: {out}")

            real_dir = install_dir
            if not os.path.isdir(real_dir):
                # 兜底：包内目录名可能与预期不同，找 install.sh 所在目录
                found = None
                for root, _dirs, files in os.walk(parent):
                    if "install.sh" in files and "prepare" in files:
                        found = root
                        break
                if found:
                    real_dir = found
                else:
                    raise Exception(f"Harbor 解压后未找到 install.sh，目录: {parent}")

            # 始终由模板重新生成 harbor.yml：
            # 保证 hostname / 端口 / 密码与本节点平台配置一致，
            # 避免沿用上一次（可能属于别的机器或旧端口）的残留配置。
            yml_path = os.path.join(real_dir, "harbor.yml")
            tmpl_path = os.path.join(real_dir, "harbor.yml.tmpl")
            if os.path.exists(tmpl_path):
                shutil.copyfile(tmpl_path, yml_path)

                host_ip = task.get("hostname") or _get_local_ip()
                harbor_port = task.get("harborPort")
                admin_pwd = (task.get("harborPassword") or "").strip()

                # HTTPS 端口：默认 443 需 root 且常被占用，统一改为「HTTP 端口 + 1」
                # 例如 http=30002 → https=30003。
                # ⚠️ 必须在「配置 docker 信任」之前算出来：certs.d 的目录名是
                # {ip}:{https_port}，用错端口会导致 docker push 找不到 CA。
                https_port = None
                if harbor_port:
                    try:
                        https_port = int(harbor_port) + 1
                    except (TypeError, ValueError):
                        https_port = None

                # ---------- HTTPS：生成证书 ----------
                # 一套根 CA + 本节点独立服务端证书（SAN 含节点 IP），
                # 之后 harbor.yml 的 https 段指向它，免去明文传输。
                cert_dir = os.path.join(real_dir, "certs")
                certs = _generate_harbor_certs(
                    task_id, host_ip, cert_dir,
                    ca_source=(task.get("caSource") or "").strip(),
                )

                # 让本机 docker 信任这张自签 CA（否则 docker push 报
                # x509: certificate signed by unknown authority）
                # 注意传 https_port（docker 走的是 https 端口）
                _trust_ca_for_docker(
                    task_id, certs["ca_crt"], host_ip,
                    https_port if https_port else harbor_port,
                )

                with open(yml_path, "r", encoding="utf-8") as f:
                    content = f.read()

                content = re.sub(r"(?m)^hostname:.*$", f"hostname: {host_ip}", content)

                # 把 https 段的占位证书路径替换为真实证书路径（相对 installDir）
                content = re.sub(
                    r"(?m)^(\s*)certificate:\s*.*$",
                    lambda mm: f"{mm.group(1)}certificate: {certs['cert']}",
                    content, count=1,
                )
                content = re.sub(
                    r"(?m)^(\s*)private_key:\s*.*$",
                    lambda mm: f"{mm.group(1)}private_key: {certs['key']}",
                    content, count=1,
                )

                # 改 https 段下的端口
                if https_port:
                    content = re.sub(
                        r"(?m)^(https:\n(?:.*\n)*?\s*)port:\s*\d+\s*$",
                        lambda mm: f"{mm.group(1)}port: {https_port}",
                        content, count=1,
                    )

                # http 端口：https 启用后 http 会 30x 跳转到 https，保留以便旧脚本兼容
                if harbor_port:
                    content = re.sub(
                        r"(?m)^(?P<indent>\s*)port:\s*80\s*$",
                        lambda mm: f"{mm.group('indent')}port: {harbor_port}",
                        content,
                        count=1,
                    )
                if admin_pwd:
                    content = re.sub(
                        r"(?m)^harbor_admin_password:.*$",
                        f"harbor_admin_password: {admin_pwd}",
                        content,
                    )
                with open(yml_path, "w", encoding="utf-8") as f:
                    f.write(content)

                log_step(task_id, "HARBOR", "已生成 harbor.yml（HTTPS 模式）",
                         hostname=host_ip, http_port=str(harbor_port or ""),
                         https_port=str(https_port or ""),
                         cert=certs["cert"])
            else:
                log_step(task_id, "HARBOR", "未找到 harbor.yml.tmpl，沿用包内 harbor.yml")

            install_sh = os.path.join(real_dir, "install.sh")
            if not os.path.exists(install_sh):
                raise Exception(f"install.sh 不存在: {install_sh}")

            os.chmod(install_sh, 0o755)
            ok, out = _run_local(
                ["bash", "install.sh"],
                cwd=real_dir,
                timeout=3600,
            )
            if not ok:
                raise Exception(f"Harbor 安装失败: {out}")

            log_step(task_id, "HARBOR", "Harbor 安装完成")

        success = True

        message = "Harbor / Helm 安装成功"

        log_step(task_id, "SUCCESS", message)

        return {"taskId": task_id, "success": True}

    except Exception as e:

        success = False

        message = str(e)

        logging.exception(f"[HARBOR][{task_id}] 安装任务失败")

        return None

    finally:

        report_k8s_result(
            task,
            success,
            message
        )


def process_harbor_operate(task: dict):
    """
    HARBOR 节点：启动 / 停止 / 重启 Harbor 服务

    与「安装」「卸载」的区别：**不做任何包的传输与目录的增删**，
    只对已有的 Harbor 部署执行 compose 级别的启停，数据卷与安装目录全部保留。

    任务字段：
        op         : start / stop / restart
        installDir : Harbor 安装目录，如 /opt/harbor
    """

    task_id = str(task.get("id"))
    op = (task.get("op") or "").strip().lower()

    success = False
    message = ""

    try:
        log_step(task_id, "HARBOR", "开始执行 Harbor 操作", op=op, task=task)

        if op not in ("start", "stop", "restart"):
            raise Exception(f"不支持的操作: {op}")

        install_dir = (task.get("installDir") or "/opt/harbor").strip()
        compose_file = os.path.join(install_dir, "docker-compose.yml")

        if not os.path.exists(compose_file):
            raise Exception(f"Harbor 未部署（找不到 {compose_file}），无法执行 {op}")

        # 优先用 compose 插件，失败回退旧版 docker-compose 命令
        def _compose(args, timeout=900):
            ok, out = _run_local(["docker", "compose"] + args, cwd=install_dir, timeout=timeout)
            if not ok:
                ok2, out2 = _run_local(["docker-compose"] + args, cwd=install_dir, timeout=timeout)
                if not ok2:
                    return False, out2
                return True, out2
            return True, out

        if op == "stop":
            log_step(task_id, "HARBOR", "停止 Harbor 容器")
            # 注意：用 stop 而非 down —— down 会删除容器，stop 保留以便快速重启
            ok, out = _compose(["stop"])
            if not ok:
                raise Exception(f"停止 Harbor 失败: {out}")
            log_step(task_id, "HARBOR", "Harbor 已停止（数据与配置已保留）")

        elif op == "start":
            log_step(task_id, "HARBOR", "启动 Harbor 容器")
            ok, out = _compose(["start"])
            if not ok:
                # 若容器已被删除，用 up -d 重建
                log_step(task_id, "HARBOR", "compose start 失败，改用 up -d 重建容器")
                ok2, out2 = _compose(["up", "-d"])
                if not ok2:
                    raise Exception(f"启动 Harbor 失败: {out2}")
            log_step(task_id, "HARBOR", "Harbor 启动命令已执行")

        else:  # restart
            log_step(task_id, "HARBOR", "重启 Harbor 容器")
            ok, out = _compose(["restart"])
            if not ok:
                log_step(task_id, "HARBOR", "compose restart 失败，改用 down + up 重建")
                _compose(["down"])
                ok2, out2 = _compose(["up", "-d"])
                if not ok2:
                    raise Exception(f"重启 Harbor 失败: {out2}")
            log_step(task_id, "HARBOR", "Harbor 重启命令已执行")

        # 等待就绪（最多 3 分钟）
        deadline = time.time() + 180
        ready = False
        while time.time() < deadline:
            _ok, ps = _run_local(
                "docker ps --filter 'status=running' --format '{{.Names}}' | grep -qi harbor && echo UP || echo DOWN",
                shell=True, timeout=30,
            )
            if op == "stop":
                # 停止场景：期望「没有运行中的 harbor 容器」
                if "DOWN" in (ps or ""):
                    ready = True
                    break
            else:
                if "UP" in (ps or ""):
                    # 再等端口就绪
                    _ok2, port_chk = _run_local(
                        "curl -sk -o /dev/null -w '%{http_code}' -m 8 "
                        "https://127.0.0.1:$(grep -A3 '^https:' "
                        + compose_file.replace("docker-compose.yml", "harbor.yml")
                        + " | grep 'port:' | head -1 | grep -oE '[0-9]+')/api/v2.0/ping 2>/dev/null || echo 000",
                        shell=True, timeout=30,
                    )
                    if "200" in (port_chk or ""):
                        ready = True
                        break
            time.sleep(5)

        if not ready:
            log_step(task_id, "HARBOR", f"{op} 已完成但就绪检测未通过（不视为失败）",
                     op=op)

        success = True
        message = {
            "start": "Harbor 启动成功",
            "stop": "Harbor 已停止（数据保留）",
            "restart": "Harbor 重启成功",
        }[op]

        log_step(task_id, "SUCCESS", message)

        return {"taskId": task_id, "success": True}

    except Exception as e:

        success = False
        message = str(e)
        logging.exception(f"[HARBOR][{task_id}] {op} 操作失败")
        return None

    finally:

        report_k8s_result(task, success, message)


def process_harbor_uninstall(task: dict):
    """
    HARBOR 节点：卸载 Harbor 与 Helm

    流程:
        1. 停止并删除 harbor 容器（docker compose down）
        2. 删除安装目录
        3. 删除 helm 二进制
        4. 上报最终结果
    """

    task_id = str(task.get("id"))

    success = False

    message = ""

    try:

        log_step(task_id, "HARBOR", "开始卸载 Harbor / Helm", task=task)

        install_dir = (task.get("installDir") or "/opt/harbor").strip()
        helm_path = (task.get("helmInstallPath") or "/usr/local/bin/helm").strip()

        # ---- Harbor ----
        compose_file = os.path.join(install_dir, "docker-compose.yml")
        if os.path.exists(compose_file):
            log_step(task_id, "HARBOR", "停止 Harbor 容器")
            ok, out = _run_local(
                ["docker", "compose", "down", "-v"],
                cwd=install_dir,
                timeout=900,
            )
            if not ok:
                # 兼容旧版 docker-compose
                ok2, out2 = _run_local(
                    ["docker-compose", "down", "-v"],
                    cwd=install_dir,
                    timeout=900,
                )
                if not ok2:
                    log_step(task_id, "HARBOR", "停止容器告警（继续清理）", detail=out2)
            log_step(task_id, "HARBOR", "Harbor 容器已停止")

        # 兜底：清掉残留 harbor 容器
        _run_local(
            "docker ps -a --format '{{.Names}}' | grep -i harbor | xargs -r docker rm -f",
            shell=True,
        )

        if os.path.isdir(install_dir):
            shutil.rmtree(install_dir, ignore_errors=True)
            log_step(task_id, "HARBOR", "已删除 Harbor 安装目录", path=install_dir)

        # ---- Helm ----
        if os.path.exists(helm_path):
            os.remove(helm_path)
            log_step(task_id, "HARBOR", "已删除 helm 二进制", path=helm_path)

        # ---- docker insecure-registries：摘掉本机 Harbor 地址 ----
        # 卸载后该地址已不存在，留在 daemon.json 里无意义；
        # 且若以后在本机换端口重装，旧条目会造成干扰。属尽力而为，失败不阻断。
        try:
            _remove_docker_insecure_registry(task_id, task.get("hostname"), task.get("harborPort"))
        except Exception as e:
            log_step(task_id, "HARBOR", "清理 insecure-registries 失败（忽略）", error=str(e))

        # ---- docker 证书目录 / 系统信任库：摘掉本机 Harbor 的 CA ----
        try:
            _remove_ca_trust(task_id, task.get("hostname"), task.get("harborPort"))
        except Exception as e:
            log_step(task_id, "HARBOR", "清理 CA 信任失败（忽略）", error=str(e))

        success = True
        message = "Harbor / Helm 卸载成功"

        log_step(task_id, "SUCCESS", message)

        return {"taskId": task_id, "success": True}

    except Exception as e:

        success = False

        message = str(e)

        logging.exception(f"[HARBOR][{task_id}] 卸载任务失败")

        return None

    finally:

        report_k8s_result(
            task,
            success,
            message
        )


def _run_local(cmd, cwd=None, timeout=600, shell=False):
    """
    执行本机命令（Harbor/Helm 安装用，不涉及集群）

    返回:
        (ok: bool, output: str)
    """

    try:

        if shell:

            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

        else:

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

    except subprocess.TimeoutExpired:

        return False, f"命令超时（{timeout}s）: {cmd}"

    except Exception as e:

        return False, f"命令异常: {e}"

    out = (proc.stdout or "") + (proc.stderr or "")

    return proc.returncode == 0, out.strip()


def process_k8s_task(task: dict):

    task_id = str(task.get("id"))

    # 检测是否为移除任务
    if task.get("action") == "remove":
        return process_k8s_remove(task)

    # HARBOR 节点：安装 / 卸载 Harbor 与 Helm（与本文件的 K8s 流程互不影响）
    if task.get("action") == "install_harbor":
        return process_harbor_install(task)

    if task.get("action") == "uninstall_harbor":
        return process_harbor_uninstall(task)

    # HARBOR 节点：启动 / 停止 / 重启 Harbor 服务（不重装，数据保留）
    if task.get("action") == "operate_harbor":
        return process_harbor_operate(task)

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
