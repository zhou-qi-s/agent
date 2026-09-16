#!/usr/bin/env python3
"""
Linux IP地址获取问题诊断脚本
在Linux上socket.gethostbyname(hostname)返回127.0.0.1的原因分析
"""

import socket
import subprocess
import os
import sys

def check_linux_ip_issues():
    """检查Linux上获取IP地址的常见问题"""
    
    hostname = socket.gethostname()
    print(f"=== Linux IP地址获取问题诊断 ===")
    print(f"主机名: {hostname}")
    print()
    
    # 1. 检查/etc/hosts文件
    print("1. 检查/etc/hosts文件:")
    try:
        with open('/etc/hosts', 'r') as f:
            hosts_content = f.read()
            if f'{hostname}' in hosts_content:
                print("   ❌ 问题: /etc/hosts文件中主机名映射到127.0.0.1")
                for line in hosts_content.split('\n'):
                    if hostname in line and '127.' in line:
                        print(f"   发现映射: {line.strip()}")
            else:
                print("   ✓ /etc/hosts文件正常")
    except Exception as e:
        print(f"   无法读取/etc/hosts: {e}")
    
    print()
    
    # 2. 检查DNS解析
    print("2. 检查DNS解析:")
    try:
        # 使用getaddrinfo检查主机名解析
        addr_info = socket.getaddrinfo(hostname, None)
        ip_list = [info[4][0] for info in addr_info]
        print(f"   主机名解析结果: {ip_list}")
        
        if '127.0.0.1' in ip_list and len(ip_list) == 1:
            print("   ❌ 问题: 主机名只解析到127.0.0.1")
        elif '127.0.0.1' in ip_list:
            print("   ⚠ 警告: 主机名解析包含127.0.0.1，但也有其他IP")
        else:
            print("   ✓ 主机名解析正常")
    except Exception as e:
        print(f"   DNS解析失败: {e}")
    
    print()
    
    # 3. 检查网络接口
    print("3. 检查网络接口:")
    try:
        import psutil
        interfaces = psutil.net_if_addrs()
        
        print(f"   找到 {len(interfaces)} 个网络接口:")
        for interface_name, addrs in interfaces.items():
            ipv4_addrs = [addr.address for addr in addrs if addr.family == socket.AF_INET]
            if ipv4_addrs:
                print(f"   {interface_name}: {ipv4_addrs}")
    except ImportError:
        print("   无法导入psutil，使用ifconfig命令:")
        try:
            result = subprocess.run(['ifconfig'], capture_output=True, text=True)
            print(result.stdout[:500] + "...")
        except:
            print("   无法执行ifconfig命令")
    
    print()
    
    # 4. 检查系统配置
    print("4. 检查系统配置:")
    
    # 检查hostname命令
    try:
        result = subprocess.run(['hostname', '-I'], capture_output=True, text=True)
        if result.returncode == 0:
            ip_addresses = result.stdout.strip().split()
            print(f"   hostname -I 输出: {ip_addresses}")
            if not ip_addresses or (len(ip_addresses) == 1 and ip_addresses[0] == '127.0.0.1'):
                print("   ❌ 问题: hostname -I 没有返回非回环地址")
        else:
            print("   hostname -I 命令失败")
    except Exception as e:
        print(f"   无法执行hostname -I: {e}")
    
    print()
    
    # 5. 推荐的解决方案
    print("5. 解决方案:")
    print("   A. 修改/etc/hosts文件:")
    print("      sudo sed -i '/127\\.0\\.0\\.1.*{hostname}/d' /etc/hosts")
    print(f"      sudo echo '$(hostname -I | cut -d' ' -f1) {hostname}' >> /etc/hosts")
    print()
    print("   B. 使用改进的Python代码:")
    print("      from utils.util import get_ip (已更新)")
    print()
    print("   C. 手动获取IP的备用方法:")
    print("      ip route get 1 | awk '{print $NF;exit}'")
    print("      hostname -I | awk '{print $1}'")

def linux_specific_ip_methods():
    """Linux特定的IP获取方法"""
    print("\n=== Linux专用IP获取方法 ===")
    
    methods = [
        ("使用ip命令", "ip route get 1 | awk '{print $NF;exit}'"),
        ("使用hostname命令", "hostname -I | awk '{print $1}'"),
        ("使用ifconfig", "ifconfig eth0 | grep 'inet ' | awk '{print $2}'"),
        ("Python socket UDP方法", "socket.create_connection(('8.8.8.8', 53), timeout=3).getsockname()[0]"),
    ]
    
    for name, cmd in methods:
        print(f"\n{name}:")
        print(f"  {cmd}")

if __name__ == "__main__":
    check_linux_ip_issues()
    linux_specific_ip_methods()
    
    # 测试当前实现
    print("\n=== 测试当前get_ip()实现 ===")
    try:
        from utils.util import get_ip, get_all_ips
        
        ip = get_ip()
        print(f"get_ip() 返回: {ip}")
        
        all_ips = get_all_ips()
        print(f"所有网络接口IP:")
        for ip_info in all_ips:
            print(f"  {ip_info['interface']}: {ip_info['ip']}")
    except Exception as e:
        print(f"测试失败: {e}")