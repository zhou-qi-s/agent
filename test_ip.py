#!/usr/bin/env python3
"""
测试IP地址获取方法
"""

import socket
import psutil

def test_old_method():
    """测试旧方法"""
    print("=" * 60)
    print("测试旧方法: socket.gethostbyname(socket.gethostname())")
    print("=" * 60)
    
    hostname = socket.gethostname()
    print(f"主机名: {hostname}")
    
    try:
        ip = socket.gethostbyname(hostname)
        print(f"获取的IP: {ip}")
        if ip == '127.0.0.1':
            print("⚠ 警告: 获取到回环地址 127.0.0.1")
    except Exception as e:
        print(f"错误: {e}")
    
    print()

def test_network_interfaces():
    """测试网络接口"""
    print("=" * 60)
    print("测试网络接口信息")
    print("=" * 60)
    
    try:
        interfaces = psutil.net_if_addrs()
        print(f"找到 {len(interfaces)} 个网络接口:")
        
        for interface_name, addrs in interfaces.items():
            print(f"\n接口: {interface_name}")
            for addr in addrs:
                if addr.family == socket.AF_INET:  # IPv4
                    print(f"  IPv4: {addr.address}")
                    print(f"    掩码: {addr.netmask}")
                    print(f"    广播: {addr.broadcast}")
                elif addr.family == socket.AF_INET6:  # IPv6
                    print(f"  IPv6: {addr.address}")
    except Exception as e:
        print(f"错误: {e}")
    
    print()

def test_udp_method():
    """测试UDP方法"""
    print("=" * 60)
    print("测试UDP方法获取IP")
    print("=" * 60)
    
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        # 尝试多个服务器
        servers = [('8.8.8.8', 53), ('1.1.1.1', 53), ('223.5.5.5', 53)]
        
        for server, port in servers:
            try:
                s.connect((server, port))
                ip = s.getsockname()[0]
                print(f"通过 {server}:{port} 获取的IP: {ip}")
                if ip and not ip.startswith('127.'):
                    print(f"✓ 成功获取非回环地址: {ip}")
                    break
            except Exception as e:
                print(f"连接 {server}:{port} 失败: {e}")
        
        s.close()
    except Exception as e:
        print(f"UDP方法错误: {e}")
    
    print()

def test_new_method():
    """测试新的IP获取方法"""
    print("=" * 60)
    print("测试新的IP获取方法")
    print("=" * 60)
    
    # 导入更新后的工具函数
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    try:
        from utils.util import get_ip, get_local_ip, get_all_ips
        
        print("1. 使用 get_ip():")
        ip = get_ip()
        print(f"   IP地址: {ip}")
        
        print("\n2. 使用 get_local_ip():")
        local_ip = get_local_ip()
        print(f"   本地IP: {local_ip}")
        
        print("\n3. 所有网络接口IP:")
        all_ips = get_all_ips()
        for ip_info in all_ips:
            print(f"   接口: {ip_info['interface']}")
            print(f"     IP: {ip_info['ip']}")
            print(f"     掩码: {ip_info['netmask']}")
        
        print("\n4. 推荐使用的IP:")
        if ip and ip != '127.0.0.1':
            print(f"   ✓ {ip}")
        elif local_ip and local_ip != '127.0.0.1':
            print(f"   ✓ {local_ip}")
        elif all_ips:
            # 选择第一个非回环地址
            for ip_info in all_ips:
                if not ip_info['ip'].startswith('127.'):
                    print(f"   ✓ {ip_info['ip']} (来自接口: {ip_info['interface']})")
                    break
        
    except Exception as e:
        print(f"测试新方法错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("IP地址获取方法测试")
    print("=" * 60)
    
    test_old_method()
    test_network_interfaces()
    test_udp_method()
    test_new_method()
    
    print("=" * 60)
    print("测试完成")
    print("建议: 使用新的 get_ip() 或 get_local_ip() 方法")
    print("=" * 60)