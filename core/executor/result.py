from dataclasses import dataclass


"""
命令执行结果封装
用于统一 pipeline 返回结构
"""


@dataclass
class CommandResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    code: int = 0
    # 挂起模式（leave_on_timeout）：超时后脚本仍在运行、未被终止。
    # task 层据此把任务标记为「执行中」并安排观察线程等真正退出后补报终态。
    still_running: bool = False