#!/bin/bash
# =========================================================
# 移除 K8S 节点
# 用于将节点从 Kubernetes 集群中移除
# =========================================================

echo "===== 开始移除 K8S 节点 ====="

# 0. 停止 kube-proxy / flanneld 等残留进程（安装脚本启动的独立进程，kubeadm reset 不会清理）
echo "[0/6] 停止 kube-proxy / flanneld 残留进程 ..."
pkill -9 -f "kube-proxy" 2>/dev/null || true
pkill -9 -f "flanneld" 2>/dev/null || true
systemctl stop kube-proxy 2>/dev/null || true
systemctl disable kube-proxy 2>/dev/null || true
systemctl stop flanneld 2>/dev/null || true
systemctl disable flanneld 2>/dev/null || true

# 1. kubeadm reset
if command -v kubeadm >/dev/null 2>&1; then
    echo "[1/6] kubeadm reset ..."
    kubeadm reset -f 2>/dev/null || true
else
    echo "[1/6] kubeadm 未安装，跳过"
fi

# 2. 停止并禁用 kubelet
echo "[2/6] 停止并禁用 kubelet ..."
systemctl stop kubelet 2>/dev/null || true
systemctl disable kubelet 2>/dev/null || true

# 3. 清理 CNI 配置
echo "[3/6] 清理 CNI 配置 ..."
rm -rf /etc/cni/net.d

# 4. 清理 kubelet 数据（先卸载残留挂载，避免 Device or resource busy）
echo "[4/6] 清理 kubelet 数据 ..."
for m in $(findmnt -rn -o TARGET 2>/dev/null | grep '^/var/lib/kubelet'); do umount -lf "$m" 2>/dev/null || true; done
rm -rf /var/lib/kubelet
rm -rf /var/lib/etcd

# 5. 清理 kubernetes 配置与运行残留
echo "[5/6] 清理 kubernetes 配置与运行残留 ..."
rm -rf /etc/kubernetes
rm -rf /var/lib/kube-proxy
rm -rf /run/flannel
rm -f /var/log/kube-proxy*.log 2>/dev/null || true

# 6. 清理 iptables 残留规则
# kubeadm reset 不会自动清 iptables：残留的 KUBE-*/DOCKER 链会累积，
# 反复移除/加入后可能干扰 Service 转发与新节点网络，故此处主动清理。
echo "[6/6] 清理 iptables 残留规则 ..."
if command -v iptables >/dev/null 2>&1; then
    # 清理 KUBE- 系列链（kube-proxy 创建）
    iptables-save 2>/dev/null | grep -o 'KUBE-[A-Z-]*' | sort -u | while read -r chain; do
        iptables -F "$chain" 2>/dev/null || true
        iptables -X "$chain" 2>/dev/null || true
    done
    # 清理 nat 表内的 KUBE 规则
    for t in nat filter; do
        for c in KUBE-SERVICES KUBE-POSTROUTING KUBE-MARK-DROP KUBE-MARK-MASQ KUBE-FORWARD KUBE-NODEPORTS KUBE-SVC KUBE-SEP; do
            iptables -t $t -F "$c" 2>/dev/null || true
            iptables -t $t -X "$c" 2>/dev/null || true
        done
    done
    # 清 nat 表里所有 KUBE 相关规则（兜底）
    iptables-save -t nat 2>/dev/null | grep -E '^-A.*KUBE' | sed 's/^-A/iptables -t nat -D/' | while read -r cmdline; do
        eval "$cmdline" 2>/dev/null || true
    done
    echo "  iptables KUBE 规则已清理"
else
    echo "  iptables 不可用，跳过"
fi

# 7. 清理 /etc/hosts 中的集群条目（join 时写入的 控制面/worker 主机名映射）
echo "[7/7] 清理 /etc/hosts 集群条目 ..."
cp -f /etc/hosts /etc/hosts.bak.k8sremove 2>/dev/null || true
# 删除形如 "<IP> <k8s-workerN|masterN|btubuntuNN>" 的行（保留 localhost 等基础条目）
sed -i -E '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\s+(k8s-worker[0-9]*|master[0-9]*|btubuntu[0-9]*)\s*$/d' /etc/hosts 2>/dev/null || true
echo "  已清理 /etc/hosts 集群条目"

echo "===== K8S 节点移除完成 ====="
exit 0