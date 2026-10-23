import logging
import os
import subprocess
import threading
import time
from typing import Optional

from core.executor.result import CommandResult


"""
统一 Shell 执行器（核心基础层）
所有系统命令 / 脚本执行必须通过这里执行
"""

# 挂起脚本登记表：task_id -> {"popen": Popen, "script_path": str, "started": ts, "max_hang": ts}
# leave_on_timeout 模式下超时不杀，进程对象登记在这里，由 task 层取走并安排观察线程。
_HANGING: dict = {}
_HANGING_LOCK = threading.Lock()


class Shell:

    @staticmethod
    def pop_hanging(task_id: str) -> Optional[dict]:
        """
        取出挂起登记（有则返回并移除）。task 层据此安排观察线程等脚本真正退出后补报终态。
        """
        with _HANGING_LOCK:
            return _HANGING.pop(task_id, None)

    @staticmethod
    def restore_hanging(task_id: str, item: dict) -> None:
        """把取出的登记放回表里（观察线程期间保留，供平台「强制结束」按 task_id 找到脚本）。"""
        if not task_id or not item:
            return
        with _HANGING_LOCK:
            _HANGING[task_id] = item

    @staticmethod
    def drop_hanging(task_id: str) -> None:
        """观察线程补报终态后清理登记。"""
        with _HANGING_LOCK:
            _HANGING.pop(task_id, None)

    @staticmethod
    def get_hanging(task_id: str) -> Optional[dict]:
        """查挂起登记（不删除）。"""
        with _HANGING_LOCK:
            return _HANGING.get(task_id)

    @staticmethod
    def list_hanging() -> dict:
        """当前挂起中的任务（task_id -> 脚本路径/开始时间），供诊断。"""
        with _HANGING_LOCK:
            return {
                k: {
                    "script_path": v.get("script_path", ""),
                    "started": v.get("started", 0),
                    "running_seconds": int(time.time() - v.get("started", time.time())),
                }
                for k, v in _HANGING.items()
            }

    @staticmethod
    def kill_hanging(task_id: str, reason: str = "平台强制结束") -> dict:
        """
        强制结束某任务挂起中的脚本及其全部子进程。

        只杀进程、**不动登记表**：观察线程仍会读到退出码并补报失败终态，
        平台任务随之转「执行失败」，状态闭环。

        返回：{"found": bool, "pid": int, "killed": [pid...], "reason": str}
        """
        item = Shell.get_hanging(task_id)
        if not item:
            return {"found": False, "pid": None, "killed": [], "reason": reason}

        proc = item.get("popen")
        killed = []
        if proc is not None and proc.poll() is None:
            pid = proc.pid
            # ① 先递归杀子进程（脚本常带子进程，只杀 shell 会留下孤儿）
            try:
                import psutil
                parent = psutil.Process(pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                        killed.append(child.pid)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except Exception as e:
                logging.warning("[EXEC] 递归结束子进程失败: task_id=%s pid=%s -> %s", task_id, pid, e)
            # ② 杀进程组（Popen 用 start_new_session=True 起，组号 = pid）
            try:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except Exception:
                # ③ 兜底：单杀
                try:
                    proc.kill()
                except Exception:
                    pass
            killed.append(pid)

        logging.warning(
            "[EXEC] 已强制结束挂起脚本（%s）: task_id=%s script=%s killed=%s",
            reason, task_id, item.get("script_path", ""), killed,
        )
        return {
            "found": True,
            "pid": getattr(proc, "pid", None),
            "killed": killed,
            "script_path": item.get("script_path", ""),
            "reason": reason,
        }

    @staticmethod
    def run(
        cmd: str,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """
        执行单行 shell 命令

        Args:
            cmd:     要执行的命令
            cwd:     工作目录
            env:     环境变量（与当前进程 env 合并）
            timeout: 超时秒数，None 表示不限制

        Returns:
            CommandResult(success, stdout, stderr, code)
        """

        logging.info(f"[EXEC] {cmd}" + (f" (timeout={timeout}s)" if timeout else ""))

        # env 与当前进程环境合并（保持与文档一致）：
        # 直接整体替换会丢掉 PATH 等系统变量，脚本内 java/nginx 等命令会找不到。
        if env:
            merged = os.environ.copy()
            merged.update({str(k): str(v) for k, v in env.items()})
            env = merged
        else:
            env = None

        try:
            process = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            code = process.returncode
            stdout = process.stdout.strip() if process.stdout else ""
            stderr = process.stderr.strip() if process.stderr else ""
            success = code == 0

        except subprocess.TimeoutExpired:
            code = -1
            success = False
            stdout = ""
            stderr = f"命令执行超时 ({timeout}s): {cmd}"
            logging.error(f"[EXEC] 超时: {stderr}")

        except Exception as e:
            code = -1
            success = False
            stdout = ""
            stderr = f"命令执行异常: {e}"
            logging.error(f"[EXEC] 异常: {stderr}")

        result = CommandResult(
            success=success,
            stdout=stdout,
            stderr=stderr,
            code=code,
        )

        if result.success:
            if result.stdout:
                logging.info(result.stdout)
        else:
            logging.error(result.stderr)

        return result

    @staticmethod
    def run_script(
        script_path: str,
        args: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        timeout: Optional[float] = None,
        shell: str = "/bin/bash",
        leave_on_timeout: bool = False,
        task_id: str = "",
        max_hang_seconds: int = 1800,
    ) -> CommandResult:
        """
        执行脚本文件（.sh / .py / ...）

        Args:
            script_path: 脚本文件路径
            args:        传给脚本的参数（字符串形式，如 "--install --force"）
            cwd:         工作目录，默认使用脚本所在目录
            env:         环境变量
            timeout:     超时秒数
            shell:       解释器，默认 /bin/bash
            leave_on_timeout: 超时后【不杀】脚本（信任脚本模式）。
                         有些安装/启动脚本会在前台常驻（脚本不退出 = 应用在跑），
                         超时把它们判失败甚至杀掉是错的。开启后超时只登记挂起，
                         返回 still_running=True，由 task 层安排观察线程等真正
                         退出后补报终态。
            task_id:     挂起登记用的任务 ID（leave_on_timeout 时必传）

        Returns:
            CommandResult(success, stdout, stderr, code, still_running)
        """

        if not os.path.isfile(script_path):
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"脚本文件不存在: {script_path}",
                code=-1,
            )

        abs_script = os.path.abspath(script_path)
        work_dir = cwd or os.path.dirname(abs_script)

        # 确保脚本可执行
        if not os.access(abs_script, os.X_OK):
            logging.info(f"[EXEC] 设置脚本可执行权限: {abs_script}")
            try:
                os.chmod(abs_script, 0o755)
            except Exception:
                pass  # 非致命，仍有 shell 兜底

        cmd_parts = [shell, abs_script]
        if args:
            cmd_parts.append(args)
        cmd = " ".join(cmd_parts)

        logging.info(f"[EXEC] 执行脚本: {abs_script}" + (f" (timeout={timeout}s)" if timeout else ""))

        # ── 挂起模式：Popen + 超时不杀 ──
        if leave_on_timeout:
            if env:
                merged = os.environ.copy()
                merged.update({str(k): str(v) for k, v in env.items()})
                run_env = merged
            else:
                run_env = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    shell=True,
                    cwd=work_dir,
                    env=run_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    # ★独立会话/进程组：① 平台「强制结束」可整组杀掉脚本及其子孙，
                    #   不会误伤 Agent 自身；② Agent 重启不会连带杀掉常驻脚本。
                    start_new_session=True,
                )
            except Exception as e:
                logging.error(f"[EXEC] 启动脚本失败: {abs_script} -> {e}")
                return CommandResult(success=False, stdout="", stderr=f"启动脚本失败: {e}", code=-1)

            try:
                out, err = proc.communicate(timeout=timeout or 300)
                code = proc.returncode
                logging.info(f"[EXEC] 脚本退出: code={code}")
                return CommandResult(
                    success=(code == 0),
                    stdout=(out or "").strip(),
                    stderr=(err or "").strip(),
                    code=code,
                )
            except subprocess.TimeoutExpired:
                # ★不杀：登记挂起，脚本继续跑；task 层负责观察与补报终态
                with _HANGING_LOCK:
                    _HANGING[task_id] = {
                        "popen": proc,
                        "script_path": abs_script,
                        "started": time.time(),
                        # 挂起上限：超过则观察线程自动结束脚本并报失败（防脚本写错永远挂住）
                        "max_hang": int(max_hang_seconds or 1800),
                    }
                logging.warning(
                    "[EXEC] 脚本超过 %ss 仍在运行（挂起模式，不杀）: %s task_id=%s",
                    timeout, abs_script, task_id,
                )
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr=f"脚本仍在执行中（超过 {timeout}s 未退出，已转为后台观察，不会被终止）",
                    code=-2,
                    still_running=True,
                )

        # ── 常规模式：阻塞等待，超时杀 ──
        return Shell.run(cmd, cwd=work_dir, env=env, timeout=timeout)