from core.pipeline.step import Step
from core.executor.shell import Shell
from core.executor.context import ExecContext


class DockerStep(Step):

    def name(self):
        return "docker_install"

    def run(self, ctx: ExecContext):

        base = ctx.base_dir

        # 清理旧版本
        Shell.run("yum remove -y docker-engine 2>/dev/null")

        # 依赖
        Shell.run(f"yum localinstall -y {base}/deps/socat-*.rpm")
        Shell.run(f"yum localinstall -y {base}/deps/lib*.rpm")
        Shell.run(f"yum localinstall -y {base}/deps/conntrack-tools-*.rpm")
        Shell.run(f"yum localinstall -y {base}/deps/ipvsadm-*.rpm")

        # docker
        Shell.run(f"yum localinstall -y {base}/deps/container-selinux-*.rpm")
        Shell.run(f"yum localinstall -y {base}/deps/containerd.io-*.rpm")
        Shell.run(f"yum localinstall -y {base}/deps/docker-ce-*.rpm")
        Shell.run(f"yum localinstall -y {base}/deps/docker-ce-cli-*.rpm")

        # daemon
        Shell.run("""
cat > /etc/docker/daemon.json <<EOF
{
  "exec-opts": ["native-cgroupdriver=systemd"],
  "storage-driver": "overlay2"
}
EOF
""")

        Shell.run("systemctl daemon-reload")
        Shell.run("systemctl enable --now docker")

        return "docker installed"