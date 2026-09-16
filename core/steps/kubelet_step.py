from core.pipeline.step import Step
from core.executor.shell import Shell
from core.executor.context import ExecContext


class KubeletStep(Step):

    def name(self):
        return "kubelet_enable"

    def run(self, ctx: ExecContext):
        # 最后启动 kubelet（此时镜像已导入，等 join cluster）
        Shell.run("systemctl daemon-reload")
        Shell.run("systemctl enable --now kubelet")

        return "kubelet enabled"
