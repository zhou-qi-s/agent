#!/usr/bin/env python3
"""
监控 Nuitka 打包进度
"""

import os
import time
import sys

def monitor_build():
    """监控打包进度"""
    print("=" * 60)
    print("Nuitka 打包进度监控")
    print("=" * 60)
    
    build_dir = "nuitka_build_new"
    exe_path = os.path.join(build_dir, "main.dist", "main.exe")
    
    if not os.path.exists(build_dir):
        print("错误: 构建目录不存在")
        return
    
    print(f"监控目录: {build_dir}")
    print("按 Ctrl+C 停止监控\n")
    
    check_count = 0
    last_size = 0
    
    try:
        while True:
            check_count += 1
            
            # 检查可执行文件
            if os.path.exists(exe_path):
                file_size = os.path.getsize(exe_path) / (1024*1024)  # MB
                print(f"[{check_count}] ✓ 打包完成!")
                print(f"   可执行文件: {exe_path}")
                print(f"   文件大小: {file_size:.2f} MB")
                print(f"   生成时间: {time.ctime(os.path.getmtime(exe_path))}")
                
                # 检查配置文件
                config_path = os.path.join(build_dir, "main.dist", "config.yaml")
                if os.path.exists(config_path):
                    print(f"   配置文件: {config_path}")
                else:
                    print(f"   ⚠ 配置文件未找到")
                
                print("\n打包完成! 使用方法:")
                print("  1. 进入目录: cd nuitka_build_new/main.dist")
                print("  2. 运行程序: main.exe")
                print("  3. 访问 API: http://localhost:8000/docs")
                break
            
            # 检查构建目录内容
            build_files = []
            for root, dirs, files in os.walk(build_dir):
                for file in files:
                    if file.endswith(('.c', '.obj', '.h', '.o')):
                        build_files.append(os.path.join(root, file))
            
            current_size = sum(os.path.getsize(f) for f in build_files if os.path.exists(f)) / (1024*1024)
            
            print(f"[{check_count}] 打包进行中... ({time.strftime('%H:%M:%S')})")
            print(f"   构建文件数: {len(build_files)}")
            print(f"   构建文件大小: {current_size:.2f} MB")
            
            if current_size > last_size:
                print(f"   📈 进度: 正在生成代码 ({current_size - last_size:.1f} MB 新增)")
            elif current_size == last_size and last_size > 0:
                print(f"   ⏳ 进度: 可能正在编译或链接")
            
            last_size = current_size
            
            # 等待5秒
            time.sleep(5)
            
    except KeyboardInterrupt:
        print("\n\n监控已停止")
        print("\n当前状态:")
        if os.path.exists(exe_path):
            print("  ✓ 可执行文件已生成")
        else:
            print("  ⏳ 打包仍在进行中")
            print("  你可以:")
            print("  1. 继续等待 (打包可能需要5-15分钟)")
            print("  2. 检查构建目录: nuitka_build_new/main.build")
            print("  3. 查看是否有错误信息")

if __name__ == "__main__":
    monitor_build()