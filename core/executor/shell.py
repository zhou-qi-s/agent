import logging
import os
import subprocess
from typing import Optional

from core.executor.result import CommandResult


"""
统一 Shell 执行器（核心基础层）
所有系统命令 / 脚本执行必须通过这里执行
"""


class Shell:

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

        Returns:
            CommandResult(success, stdout, stderr, code)
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

        return Shell.run(cmd, cwd=work_dir, env=env, timeout=timeout)