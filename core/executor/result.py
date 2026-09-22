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