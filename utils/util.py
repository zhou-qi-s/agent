import getpass
import logging
import socket
import psutil

username = getpass.getuser()
hostname = socket.gethostname()
ip = None


def get_ip():
    """获取本地IP地址（优化版本，避免返回127.0.0.1）"""
    global ip
    if not ip:
        ip = get_local_ip()
    return ip


def get_local_ip():
    """获取本地非回环地址的IP（主要函数，跨平台优化）"""
    try:
        import platform
        system = platform.system()

        # 优先：如果 config.yaml 配置了指定网卡，直接用该网卡获取 IP
        from utils.config_loader import load_config
        config = load_config()
        nic = (config.get("server", {}) or {}).get("network_interface", "") or ""
        if nic:
            nic_ip = get_ip_by_nic(nic)
            if nic_ip:
                return nic_ip

        # Linux系统：使用专门的Linux方法
        if system == 'Linux':
            linux_ip = get_ip_for_linux()
            if linux_ip and not linux_ip.startswith('127.'):
                return linux_ip
        
        # 方法1: 使用 UDP 连接获取外网IP（最可靠，跨平台）
        external_ip = get_ip_by_udp()
        if external_ip and not external_ip.startswith('127.'):
            return external_ip
        
        # 方法2: 使用 psutil 获取网络接口IP
        network_ip = get_ip_from_network_interfaces()
        if network_ip and not network_ip.startswith('127.'):
            return network_ip
        
        # 方法3: 传统的 socket.gethostbyname（备用）
        try:
            traditional_ip = socket.gethostbyname(hostname)
            if traditional_ip and not traditional_ip.startswith('127.'):
                return traditional_ip
        except:
            pass
        
        # Linux备用方法：尝试shell命令
        if system == 'Linux':
            try:
                import subprocess
                # 尝试多种Linux命令获取IP
                commands = [
                    "hostname -I | awk '{print $1}'",
                    "ip route get 1 | awk '{print $NF;exit}'",
                    "ifconfig eth0 | grep 'inet ' | awk '{print $2}' 2>/dev/null || ifconfig enp0s3 | grep 'inet ' | awk '{print $2}'"
                ]
                
                for cmd in commands:
                    try:
                        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=3)
                        if result.returncode == 0:
                            ip = result.stdout.strip()
                            if ip and not ip.startswith('127.'):
                                return ip
                    except:
                        continue
            except:
                pass
        
        # 如果都没找到，返回默认值
        return '127.0.0.1'
    except Exception as e:
        logging.warning(f"获取IP地址失败: {e}")
        return '127.0.0.1'


def get_ip_for_linux():
    """Linux专用的IP获取方法"""
    try:
        import subprocess
        import re
        
        # 方法1: 使用ip命令（最可靠）
        try:
            result = subprocess.run(
                ["ip", "route", "get", "1"],
                capture_output=True,
                text=True,
                timeout=3
            )
            if result.returncode == 0:
                match = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', result.stdout)
                if match:
                    ip = match.group(1)
                    if ip and not ip.startswith('127.'):
                        return ip
        except:
            pass
        
        # 方法2: 使用hostname命令
        try:
            result = subprocess.run(
                ["hostname", "-I"],
                capture_output=True,
                text=True,
                timeout=3
            )
            if result.returncode == 0:
                ips = result.stdout.strip().split()
                for ip in ips:
                    if ip and not ip.startswith('127.'):
                        return ip
        except:
            pass
        
        # 方法3: 检查默认网关接口
        try:
            result = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True,
                text=True,
                timeout=3
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if lines:
                    # 获取默认路由的接口
                    match = re.search(r'dev\s+(\w+)', lines[0])
                    if match:
                        interface = match.group(1)
                        # 获取该接口的IP
                        result2 = subprocess.run(
                            ["ip", "addr", "show", interface],
                            capture_output=True,
                            text=True,
                            timeout=3
                        )
                        if result2.returncode == 0:
                            match2 = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)/', result2.stdout)
                            if match2:
                                ip = match2.group(1)
                                if ip and not ip.startswith('127.'):
                                    return ip
        except:
            pass
        
        return None
    except Exception as e:
        logging.debug(f"Linux专用IP获取失败: {e}")
        return None


def get_ip_by_nic(nic_name: str):
    """通过指定网卡名称获取 IP 地址"""
    try:
        interfaces = psutil.net_if_addrs()
        if nic_name in interfaces:
            for addr in interfaces[nic_name]:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if ip and not (ip.startswith('127.') or ip.startswith('169.254.')):
                        return ip
        logging.warning(f"网卡 '{nic_name}' 不存在或无有效 IPv4 地址")
        return None
    except Exception as e:
        logging.warning(f"通过网卡 {nic_name} 获取IP失败: {e}")
        return None


def get_ip_by_udp():
    """通过UDP连接获取外网IP（最可靠的方法）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        # 连接到公共DNS服务器，不会真正发送数据
        s.connect(('8.8.8.8', 53))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        logging.debug(f"UDP方法获取IP失败: {e}")
        return None


def get_ip_from_network_interfaces():
    """从网络接口获取IP地址（跨平台）"""
    try:
        interfaces = psutil.net_if_addrs()
        
        # Linux 常见的网络接口（优先检查）
        linux_preferred = ['eth0', 'enp0s3', 'enp0s8', 'ens33', 'ens160', 'wlan0', 'wlp2s0']
        # Windows 网络接口
        windows_preferred = ['以太网', '本地连接', 'Wi-Fi', 'WLAN', 'Ethernet']
        
        # 根据平台选择优先接口列表
        import platform
        system = platform.system()
        
        if system == 'Linux':
            preferred_interfaces = linux_preferred
        elif system == 'Windows':
            preferred_interfaces = windows_preferred
        else:
            preferred_interfaces = []  # 其他系统
        
        # 1. 先检查优先接口（平台特定）
        for interface_name in preferred_interfaces:
            if interface_name in interfaces:
                for addr in interfaces[interface_name]:
                    if addr.family == socket.AF_INET:
                        ip = addr.address
                        if ip and not (ip.startswith('127.') or ip.startswith('169.254.')):
                            return ip
        
        # 2. Linux特定：排除虚拟和内部接口
        if system == 'Linux':
            excluded_interfaces = ['lo', 'docker', 'veth', 'br-', 'virbr', 'tun', 'tap']
            for interface_name, addrs in interfaces.items():
                # 跳过虚拟和内部接口
                if any(excluded in interface_name for excluded in excluded_interfaces):
                    continue
                
                for addr in addrs:
                    if addr.family == socket.AF_INET:
                        ip = addr.address
                        if ip and not (ip.startswith('127.') or 
                                     ip.startswith('169.254.') or 
                                     ip.startswith('0.') or
                                     ip.startswith('172.') or  # Docker内部网络
                                     ip.startswith('10.')):     # 内部网络
                            return ip
        
        # 3. 查找所有非回环地址（通用）
        for interface_name, addrs in interfaces.items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    ip = addr.address
                    if ip and not (ip.startswith('127.') or 
                                 ip.startswith('169.254.') or 
                                 ip.startswith('0.') or
                                 ip.startswith('172.17.')):  # Docker默认网络
                        return ip
        
        # 4. 返回第一个IPv4地址（最后手段）
        for interface_name, addrs in interfaces.items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    return addr.address
        
        return None
    except Exception as e:
        logging.debug(f"网络接口方法获取IP失败: {e}")
        return None


def get_all_ips():
    """获取所有网络接口的IP地址"""
    try:
        interfaces = psutil.net_if_addrs()
        all_ips = []
        
        for interface_name, addrs in interfaces.items():
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    all_ips.append({
                        'interface': interface_name,
                        'ip': addr.address,
                        'netmask': addr.netmask,
                        'broadcast': addr.broadcast
                    })
        
        return all_ips
    except Exception as e:
        logging.error(f"获取所有IP失败: {e}")
        return []


def update_ip():
    """更新IP地址缓存"""
    global ip
    old_ip = ip
    try:
        ip = get_local_ip()
        if old_ip != ip:
            logging.info(f"IP地址已更新: {old_ip} -> {ip}")
    except Exception as e:
        logging.error(f"更新IP地址失败: {e}")
        ip = old_ip  # 回滚到旧值
