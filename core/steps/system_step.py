from core.pipeline.step import Step
from core.executor.shell import Shell
from core.executor.context import ExecContext


class SystemStep(Step):

    def name(self):
        return "system_init"

    def run(self, ctx: ExecContext):

        # 主机名
        if ctx.hostname:
            Shell.run(f"hostnamectl set-hostname {ctx.hostname}")

        # hosts 配置
        if ctx.hosts:
            hosts_content = "\n".join([f"{ip} {hostname}" for ip, hostname in ctx.hosts.items()])
            Shell.run(f'cat >> /etc/hosts <<EOF\n{hosts_content}\nEOF')

        # DNS
        Shell.run('echo "nameserver 114.114.114.114" > /etc/resolv.conf')

        # 时区
        Shell.run("timedatectl set-timezone Asia/Shanghai")

        # SELinux
        Shell.run("setenforce 0 2>/dev/null")
        Shell.run("sed -i 's/^SELINUX=enforcing/SELINUX=disabled/' /etc/selinux/config 2>/dev/null")

        # 防火墙
        Shell.run("systemctl stop firewalld 2>/dev/null")
        Shell.run("systemctl disable firewalld 2>/dev/null")

        # swap
        Shell.run("swapoff -a")
        Shell.run("sed -i '/ swap / s/^/#/' /etc/fstab")

        # 内核模块
        Shell.run("""
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
""")
        Shell.run("modprobe overlay")
        Shell.run("modprobe br_netfilter")

        # sysctl
        Shell.run("""
cat > /etc/sysctl.d/99-k8s.conf <<EOF
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
EOF
""")
        Shell.run("sed -i 's/^net.ipv4.ip_forward.*/net.ipv4.ip_forward = 1/' /etc/sysctl.conf")
        Shell.run("sysctl --system")

        return "system init completed"