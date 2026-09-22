from core.pipeline.step import Step
from core.executor.shell import Shell
from core.executor.context import ExecContext


class K8sStep(Step):

    def name(self):
        return "k8s_install"

    def run(self, ctx: ExecContext):

        base = ctx.base_dir

        # 1. kubeadm/kubelet/kubectl 二进制
        Shell.run(f"chmod +x {base}/kubeadm {base}/kubelet {base}/kubectl")
        Shell.run(f"cp {base}/kubeadm {base}/kubelet {base}/kubectl /usr/local/bin/")

        # 2. kubelet systemd 服务
        Shell.run(f"cp {base}/kubelet.service /etc/systemd/system/")
        Shell.run("mkdir -p /etc/systemd/system/kubelet.service.d")
        Shell.run(f"cp {base}/10-kubeadm.conf /etc/systemd/system/kubelet.service.d/")

        # 3. CNI 插件
        Shell.run("mkdir -p /opt/cni/bin")
        Shell.run(f"tar -xzf {base}/cni-plugins-linux-arm64-v1.3.0.tgz -C /opt/cni/bin")

        # 4. cri-dockerd
        Shell.run(f"tar -xzf {base}/cri-dockerd-0.3.4.arm64.tgz -C /usr/bin/ --strip-components=1")
        Shell.run("chmod +x /usr/bin/cri-dockerd")
        Shell.run(f"cp {base}/cri-dockerd.service /etc/systemd/system/")

        # 5. crictl
        Shell.run(f"tar -xzf {base}/crictl-v1.27.0-linux-arm64.tar.gz -C /usr/local/bin")

        # 6. 启用 cri-dockerd（kubelet 最后启动，等 join 后再启用）
        Shell.run("systemctl daemon-reload")
        Shell.run("systemctl enable --now cri-dockerd")

        return "k8s installed"