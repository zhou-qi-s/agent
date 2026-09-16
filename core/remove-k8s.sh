#!/bin/bash
# =========================================================
# 移除 K8S 节点
# 用于将节点从 Kubernetes 集群中移除
# =========================================================

echo "===== 开始移除 K8S 节点 ====="

# 0. 停止 kube-proxy / flanneld 等残留进程（安装脚本启动的独立进程，kubeadm reset 不会清理）
echo "[0/8] 停止 kube-proxy / flanneld 残留进程 ..."
pkill -9 -f "kube-proxy" 2>/dev/null || true
pkill -9 -f "flanneld" 2>/dev/null || true
systemctl stop kube-proxy 2>/dev/null || true
systemctl disable kube-proxy 2>/dev/null || true
systemctl stop flanneld 2>/dev/null || true
systemctl disable flanneld 2>/dev/null || true

# 0.5 清理 cri-dockerd（Docker 方案专有）—— 这是"重新加入后节点 NotReady"的根因所在
#
#   根因（2026-09-16 实测定位）：
#     cri-dockerd 的 deb 包自带了一套 unit，名字是 cri-docker.service / cri-docker.socket
#     （注意：比我们的 cri-dockerd.service 少一个 d），其中：
#       /lib/systemd/system/cri-docker.socket   ListenStream=%t/cri-dockerd.sock
#       /lib/systemd/system/cri-docker.service  ExecStart=/usr/bin/cri-dockerd --container-runtime-endpoint fd://
#     join 脚本另写的 /etc/systemd/system/cri-dockerd.service 带了
#       --pod-infra-container-image=registry.k8s.io/pause:3.7
#     两套服务都想绑定同一个 /run/cri-dockerd.sock：
#       · cri-docker.socket 是 socket 激活单元，通常抢到正式 socket，其激活出的进程
#         没有 pause 参数 → 退回编译内置默认（pause:3.10）
#       · 我们带参数的进程则被迫绑到随机名 socket（/run/<digits>），无人连接
#     离线包里只有 pause:3.7，没有 3.10 → 拉镜像失败 → pod sandbox 创建失败
#       → kube-proxy 卡 ContainerCreating、flannel 卡 Init:0/2 → 节点永久 NotReady。
#     这是竞态：谁先绑上谁赢，所以"首次安装可能成功、重启/重装后随机失败"。
#
#   因此移除时必须：
#     (a) mask 掉包自带的 cri-docker.{service,socket}，让它们永不再参与启动
#     (b) 停掉并禁用我们自己的 cri-dockerd.service
#     (c) 杀光所有 cri-dockerd 进程（含孤儿）+ 删除残留 socket
echo "[0.5/8] 清理 cri-dockerd（含包自带的 cri-docker.* 干扰单元）..."

# (a) 封印 deb 包自带的 cri-docker.service / cri-docker.socket（抢 socket 的元凶）
#     用 mask 而非 remove：可逆、不破坏 dpkg 包完整性
for _u in cri-docker.socket cri-docker.service; do
    systemctl stop "$_u" 2>/dev/null || true
    systemctl disable "$_u" 2>/dev/null || true
    if systemctl mask "$_u" 2>/dev/null; then
        echo "  已 mask（永久禁止启动）: $_u"
    else
        echo "  ⚠ mask 失败: $_u"
    fi
done
# 清掉 sockets.target 里的残留符号链接（disable 已删，这里兜底）
rm -f /etc/systemd/system/sockets.target.wants/cri-docker.socket 2>/dev/null || true
rm -f /etc/systemd/system/multi-user.target.wants/cri-docker.service 2>/dev/null || true

# (b) 停掉我们自己的服务
systemctl stop cri-dockerd.socket 2>/dev/null || true
systemctl stop cri-dockerd 2>/dev/null || true
systemctl disable cri-dockerd 2>/dev/null || true
systemctl reset-failed cri-dockerd 2>/dev/null || true

# (c) 多轮强杀 + 等待确认：覆盖"杀父进程后子进程仍在/被重新拉起"的情况。
# cri-dockerd 是多进程程序（主进程 + fork 的子进程），子进程会变孤儿（PPID=1）
# 并继续占用 socket，因此必须循环杀到「进程数为 0 且 socket 无监听者」为止。
_killed=0
for _round in $(seq 1 15); do
    pkill -9 -f "cri-dockerd" 2>/dev/null || true
    sleep 1
    LEFT_PROC=$(pgrep -f "cri-dockerd" 2>/dev/null | wc -l)
    SOCK_USED=$(ss -lxp 2>/dev/null | grep -c "cri-dockerd" || true)
    if [ "$LEFT_PROC" -eq 0 ] && [ "$SOCK_USED" -eq 0 ]; then
        _killed=1
        break
    fi
done
[ "$_killed" -eq 0 ] && echo "  （清理循环已跑满 15 轮，继续兜底删除 socket）"

# 删除残留 socket（必须删：残留文件会导致新进程 bind 失败，旧连接继续被孤儿进程服务）
rm -f /run/cri-dockerd.sock /var/run/cri-dockerd.sock
rm -rf /etc/systemd/system/cri-dockerd.socket.d 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true

# 校验
LEFT_PROC=$(pgrep -f "cri-dockerd" 2>/dev/null | wc -l)
if [ "$LEFT_PROC" -gt 0 ]; then
    echo "  ⚠ 仍有 $LEFT_PROC 个 cri-dockerd 进程残留:"
    ps -eo pid,ppid,cmd 2>/dev/null | grep "[c]ri-dockerd"
else
    echo "  cri-dockerd 进程已清理"
fi
if [ -e /run/cri-dockerd.sock ] || [ -e /var/run/cri-dockerd.sock ]; then
    echo "  ⚠ cri-dockerd socket 仍存在"
else
    echo "  cri-dockerd socket 已删除"
fi
# 校验包自带的干扰单元是否已被 mask（必须为 masked，否则下次 join 仍会抢 socket）
for _u in cri-docker.socket cri-docker.service; do
    _st=$(systemctl is-enabled "$_u" 2>&1)
    if [ "$_st" = "masked" ]; then
        echo "  $_u 已 mask"
    else
        echo "  ⚠ $_u 未 mask（当前: $_st）—— 下次加入可能仍出现 sandbox 镜像错误"
    fi
done

# 1. kubeadm reset
if command -v kubeadm >/dev/null 2>&1; then
    echo "[1/8] kubeadm reset ..."
    kubeadm reset -f 2>/dev/null || true
else
    echo "[1/8] kubeadm 未安装，跳过"
fi

# 2. 停止并禁用 kubelet
echo "[2/8] 停止并禁用 kubelet ..."
systemctl stop kubelet 2>/dev/null || true
systemctl disable kubelet 2>/dev/null || true

# 3. 清理 CNI 配置
echo "[3/8] 清理 CNI 配置 ..."
rm -rf /etc/cni/net.d

# 3.1 删除 flannel/cni 虚拟网络接口
# 说明：kubeadm reset 与 rm -rf /etc/cni/net.d 都不会删除这些接口，
#   残留的 flannel.1 会带着旧 PodCIDR 地址（10.244.x.0），
#   用同名主机名重新 join 时可能与新的 PodCIDR 冲突，导致节点异常。
echo "[3.1/8] 删除 flannel/cni 虚拟网络接口 ..."
for iface in flannel.1 cni0 kube-ipvs0 kube-bridge; do
    if ip link show "$iface" >/dev/null 2>&1; then
        # 先清掉接口上的残留 IP（避免 delete 失败）
        for addr in $(ip -o -4 addr show "$iface" 2>/dev/null | awk '{print $4}'); do
            ip addr del "$addr" dev "$iface" 2>/dev/null || true
        done
        ip link set "$iface" down 2>/dev/null || true
        if ip link delete "$iface" 2>/dev/null; then
            echo "  已删除接口: $iface"
        else
            echo "  ⚠ 删除接口 $iface 失败"
        fi
    fi
done
# 兜底：清掉任何仍指向 10.244.x.x 的 flannel 地址
ip -o -4 addr show 2>/dev/null | grep '10\.244\.' | awk '{print $2, $4}' | while read -r dev addr; do
    ip addr del "$addr" dev "$dev" 2>/dev/null || true
done

# 4. 清理 kubelet 数据（先卸载残留挂载，避免 Device or resource busy）
echo "[4/8] 清理 kubelet 数据 ..."
for m in $(findmnt -rn -o TARGET 2>/dev/null | grep '^/var/lib/kubelet'); do umount -lf "$m" 2>/dev/null || true; done
rm -rf /var/lib/kubelet
rm -rf /var/lib/etcd

# 5. 清理 kubernetes 配置与运行残留
echo "[5/8] 清理 kubernetes 配置与运行残留 ..."
rm -rf /etc/kubernetes
rm -rf /var/lib/kube-proxy
rm -rf /run/flannel
rm -f /var/log/kube-proxy*.log 2>/dev/null || true

# 6. 清理 iptables 残留规则
# kubeadm reset 不会自动清 iptables：残留的 KUBE-*/FLANNEL-* 链会累积。
# 注意：kube-proxy 会建大量**动态命名的链**（KUBE-SVC-<hash>、KUBE-SEP-<hash>、
#   KUBE-XLB-<hash> 等），固定名单清不掉，因此这里改成
#   「先删所有引用它们的跳转规则，再扫全部 KUBE-*/FLANNEL-* 链逐个清空+删除」。
echo "[6/8] 清理 iptables 残留规则 ..."
if command -v iptables >/dev/null 2>&1; then
    for t in filter nat mangle raw; do
        # 6.1 删除所有跳转到 KUBE-*/FLANNEL-* 的规则（--wait 防 xtables 锁冲突）
        iptables-save -t "$t" 2>/dev/null | grep -E '^-A .*(KUBE-|FLANNEL-)' | \
            sed 's/^-A/iptables -t '"$t"' -D/' | while read -r cmdline; do
                eval "$cmdline --wait" 2>/dev/null || true
            done
        # 6.2 逐个清空并删除 KUBE-*/FLANNEL-* 链（覆盖动态链）
        #     反复多轮：部分链之间有依赖，一轮删不干净
        for _round in 1 2 3; do
            changed=0
            for chain in $(iptables-save -t "$t" 2>/dev/null | grep -oE '^:(KUBE-[A-Za-z0-9_-]+|FLANNEL-[A-Za-z0-9_-]+)' | tr -d ':' | sort -u); do
                iptables -t "$t" -F "$chain" --wait 2>/dev/null || true
                if iptables -t "$t" -X "$chain" --wait 2>/dev/null; then
                    changed=1
                fi
            done
            [ "$changed" = "0" ] && break
        done
    done
    # 6.3 复查
    # 注意：grep -c 匹配不到时会输出 0 并以非 0 退出，若写成 `|| echo 0` 会把两个 0
    #   都拼进变量，导致 [: 0\n0: integer expression expected。故改用 wc -l 计数。
    LEFT=$(iptables-save 2>/dev/null | grep -E 'KUBE-|FLANNEL-' | wc -l)
    if [ "$LEFT" -gt 0 ]; then
        echo "  ⚠ 仍有 $LEFT 条 KUBE/FLANNEL 规则残留"
    else
        echo "  iptables KUBE/FLANNEL 规则已清理"
    fi
    # 6.4 清 IPVS（若启用了 ipvs 模式）
    if command -v ipvsadm >/dev/null 2>&1; then
        ipvsadm --clear 2>/dev/null || true
    fi
else
    echo "  iptables 不可用，跳过"
fi

# 7. 清理 /etc/hosts 中的集群条目（join 时写入的 控制面/worker 主机名映射）
echo "[7/8] 清理 /etc/hosts 集群条目 ..."
cp -f /etc/hosts /etc/hosts.bak.k8sremove 2>/dev/null || true
# 删除形如 "<IP> <k8s-workerN|masterN|btubuntuNN>" 的行（保留 localhost 等基础条目）
sed -i -E '/^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\s+(k8s-worker[0-9]*|master[0-9]*|btubuntu[0-9]*)\s*$/d' /etc/hosts 2>/dev/null || true
# 同时删除 join 脚本写入的标记行（否则残留空块，且下次 join 的 sed 范围可能异常）
sed -i -E '/^# K8S-NODES-BEGIN$/d; /^# K8S-NODES-END$/d' /etc/hosts 2>/dev/null || true
echo "  已清理 /etc/hosts 集群条目"

echo "===== K8S 节点移除完成 ====="
exit 0
