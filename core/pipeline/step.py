from abc import ABC, abstractmethod
from core.executor.context import ExecContext


"""
Step 基类
所有任务步骤必须继承
"""


class Step(ABC):

    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def run(self, ctx: ExecContext):
        pass