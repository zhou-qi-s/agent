from core.pipeline.step import Step
from core.executor.shell import Shell
from core.executor.context import ExecContext


class ImageStep(Step):

    def name(self):
        return "image_load"

    def run(self, ctx: ExecContext):

        base = ctx.base_dir

        images = [
            "kube-apiserver.tar",
            "kube-controller-manager.tar",
            "kube-scheduler.tar",
            "kube-proxy.tar",
            "pause.tar",
            "etcd.tar",
            "coredns.tar",
            "flannel.tar"
        ]

        for img in images:
            Shell.run(f"docker load -i {base}/{img}")

        return "images loaded"