#!/usr/bin/env python3
"""
Nacos API 使用示例
展示如何使用配置文件中的 Nacos 接口路径
"""

from core.utils import (
    register_to_nacos,
    deregister_from_nacos,
    start_heartbeat,
    list_nacos_instances,
    get_nacos_api_paths
)
from utils.config_loader import load_config
import time


def example_show_api_paths():
    """示例1：显示当前使用的 Nacos API 路径"""
    print("=" * 60)
    print("当前 Nacos API 路径配置")
    print("=" * 60)
    
    api_paths = get_nacos_api_paths()
    for name, path in api_paths.items():
        print(f"  {name:20s}: {path}")
    
    print()


def example_register_service():
    """示例2：注册服务到 Nacos"""
    print("=" * 60)
    print("注册服务到 Nacos")
    print("=" * 60)
    
    # 从配置读取 Nacos 地址
    config = load_config()
    nacos_ip = config.get("nacos", {}).get("ip", "127.0.0.1")
    nacos_port = config.get("nacos", {}).get("port", 8848)
    nacos_addr = f"http://{nacos_ip}:{nacos_port}"
    
    # 服务信息
    service_name = "my-test-service"
    ip = "192.168.1.100"
    ports = [8080, 8081]
    
    try:
        # 注册实例（使用配置文件中的 API 路径）
        registered, failed = register_to_nacos(
            nacos_addr=nacos_addr,
            service_name=service_name,
            ip=ip,
            ports=ports,
            group_name="DEFAULT_GROUP",
            namespace_id="public",
            cluster_name="DEFAULT",
            metadata={"version": "1.0", "env": "test"},
            ephemeral=True
        )
        
        print(f"✓ 注册成功端口: {registered}")
        if failed:
            print(f"✗ 注册失败端口: {failed}")
        
        return registered
        
    except Exception as e:
        print(f"✗ 注册失败: {e}")
        return []


def example_start_heartbeat(ports):
    """示例3：启动心跳保活"""
    print("=" * 60)
    print("启动 Nacos 心跳")
    print("=" * 60)
    
    # 从配置读取 Nacos 地址
    config = load_config()
    nacos_ip = config.get("nacos", {}).get("ip", "127.0.0.1")
    nacos_port = config.get("nacos", {}).get("port", 8848)
    nacos_addr = f"http://{nacos_ip}:{nacos_port}"
    
    # 启动心跳（使用配置文件中的 API 路径）
    start_heartbeat(
        nacos_addr=nacos_addr,
        service_name="my-test-service",
        ip="192.168.1.100",
        ports=ports,
        interval=5,  # 每5秒一次心跳
        group_name="DEFAULT_GROUP",
        namespace_id="public",
        cluster_name="DEFAULT"
    )
    
    print(f"✓ 心跳已启动，端口: {ports}")
    print()


def example_query_instances():
    """示例4：查询服务实例列表"""
    print("=" * 60)
    print("查询 Nacos 服务实例")
    print("=" * 60)
    
    # 从配置读取 Nacos 地址
    config = load_config()
    nacos_ip = config.get("nacos", {}).get("ip", "127.0.0.1")
    nacos_port = config.get("nacos", {}).get("port", 8848)
    nacos_addr = f"http://{nacos_ip}:{nacos_port}"
    
    # 查询实例（使用配置文件中的 API 路径）
    instances = list_nacos_instances(
        nacos_addr=nacos_addr,
        service_name="my-test-service",
        group_name="DEFAULT_GROUP",
        namespace_id="public",
        healthy_only=False
    )
    
    if instances:
        print(f"✓ 查询成功:")
        print(f"  服务名: {instances.get('name')}")
        print(f"  健康实例数: {instances.get('healthyInstanceCount')}")
        print(f"  总实例数: {len(instances.get('hosts', []))}")
        
        for host in instances.get('hosts', [])[:3]:  # 只显示前3个
            print(f"    - {host.get('ip')}:{host.get('port')} (健康: {host.get('healthy')})")
    else:
        print("✗ 查询失败或无实例")
    
    print()


def example_deregister_service():
    """示例5：从 Nacos 注销服务"""
    print("=" * 60)
    print("从 Nacos 注销服务")
    print("=" * 60)
    
    # 从配置读取 Nacos 地址
    config = load_config()
    nacos_ip = config.get("nacos", {}).get("ip", "127.0.0.1")
    nacos_port = config.get("nacos", {}).get("port", 8848)
    nacos_addr = f"http://{nacos_ip}:{nacos_port}"
    
    # 注销实例（使用配置文件中的 API 路径）
    success = deregister_from_nacos(
        nacos_addr=nacos_addr,
        service_name="my-test-service",
        ip="192.168.1.100",
        port=8080,
        group_name="DEFAULT_GROUP",
        namespace_id="public",
        cluster_name="DEFAULT",
        ephemeral=True
    )
    
    if success:
        print("✓ 注销成功")
    else:
        print("✗ 注销失败")
    
    print()


def example_custom_config():
    """示例6：自定义 Nacos API 路径"""
    print("=" * 60)
    print("自定义 Nacos API 路径")
    print("=" * 60)
    
    # 可以在 config.yaml 中自定义 API 路径：
    config_example = """
nacos:
  ip: 192.168.31.208
  port: 8848
  
  # 自定义 API 路径（如果不配置，使用默认值）
  api:
    register: /nacos/v1/ns/instance          # 默认
    deregister: /nacos/v1/ns/instance        # 默认
    heartbeat: /nacos/v1/ns/instance/beat    # 默认
    instance_list: /nacos/v1/ns/instance/list
    service_list: /nacos/v1/ns/service/list
    service_detail: /nacos/v1/ns/service
"""
    
    print("配置示例 (config.yaml):")
    print(config_example)
    
    # 显示当前配置
    api_paths = get_nacos_api_paths()
    print("当前实际使用的 API 路径:")
    for name, path in api_paths.items():
        print(f"  {name}: {path}")
    
    print()


if __name__ == "__main__":
    print("\n")
    print("*" * 60)
    print("Nacos API 使用示例")
    print("*" * 60)
    print()
    
    # 示例1：显示 API 路径
    example_show_api_paths()
    
    # 示例2：注册服务
    registered_ports = example_register_service()
    
    # 示例3：启动心跳
    if registered_ports:
        example_start_heartbeat(registered_ports)
        
        # 示例4：查询实例
        example_query_instances()
        
        # 模拟运行一段时间
        print("=" * 60)
        print("服务运行中... (按 Ctrl+C 停止)")
        print("=" * 60)
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            pass
        
        # 示例5：注销服务
        example_deregister_service()
    
    # 示例6：自定义配置
    example_custom_config()
    
    print("*" * 60)
    print("示例完成")
    print("*" * 60)
