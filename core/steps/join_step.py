import logging
from core.pipeline.step import Step
from core.executor.shell import Shell
from core.executor.context import ExecContext


class JoinStep(Step):

    def name(self):
        return "k8s_join"

    def run(self, ctx: ExecContext):

        if not ctx.join_token:
            logging.warning("[K8S] 未提供 join token，跳过 join")
            return "skip join"

        # 执行 kubeadm join 命令
        logging.info(f"[K8S] 执行 kubeadm join")
        result = Shell.run(ctx.join_token)

        if result.success:
            logging.info("[K8S] Node join 成功")
        else:
            logging.error(f"[K8S] Node join 失败: {result.stderr}")

        return "join executed"
