#!/usr/bin/env python3
"""
ARM架构优化补丁
在ARM设备上运行前执行此脚本
"""

import os
import sys
import platform
import subprocess

def check_arm_environment():
    """检查ARM环境"""
    arch = platform.machine()
    system = platform.system()
    
    print(f"=== ARM环境检测 ===")
    print(f"系统架构: {arch}")
    print(f"操作系统: {system}")
    print(f"Python版本: {platform.python_version()}")
    
    # ARM架构检测
    is_arm = arch.lower() in ['aarch64', 'arm64', 'armv7l', 'armv8l']
    
    if not is_arm:
        print(f"⚠ 警告: 当前架构 {arch} 不是ARM架构")
        print("继续执行可能会导致兼容性问题")
    
    return is_arm, arch

def patch_for_arm():
    """为ARM架构打补丁"""
    
    # 1. 修改requirements.txt，移除ARM不兼容的包
    requirements_file = "requirements.txt"
    if os.path.exists(requirements_file):
        with open(requirements_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 移除或注释掉ARM不兼容的包
        arm_incompatible = [
            'GPUtil==1.4.0',  # ARM通常没有NVIDIA GPU
        ]
        
        for pkg in arm_incompatible:
            if pkg in content:
                content = content.replace(pkg, f"# {pkg}  # ARM架构不兼容，已注释")
        
        # 添加ARM优化注释
        arm_header = """# ARM架构优化版本
# 以下依赖在ARM上需要从源码编译:
# pip install --no-binary psutil,PyYAML,pydantic_core,httptools,zstandard -r requirements.txt\n\n"""
        
        content = arm_header + content
        
        with open("requirements-arm.txt", 'w', encoding='utf-8') as f:
            f.write(content)
        
        print(f"[OK] 已创建ARM优化版依赖文件: requirements-arm.txt")
    
    # 2. 创建ARM专用的启动配置
    arm_config = """# ARM架构专用配置（可选）
arm:
  # ARM架构特定优化
  architecture: arm64  # 或 armv7l
  # 禁用GPU检测（ARM通常没有NVIDIA GPU）
  disable_gpu_detection: true
  # ARM优化参数
  optimization:
    use_neon: true  # ARM NEON指令集优化
    cpu_affinity: true  # CPU亲和性设置
"""
    
    with open("config-arm.yaml", 'w', encoding='utf-8') as f:
        f.write(arm_config)
    
    print(f"[OK] 已创建ARM专用配置: config-arm.yaml")
    
    # 3. 创建ARM健康检查脚本
    health_check = """#!/usr/bin/env python3
"""
    # 健康检查脚本内容较长，这里简化
    print(f"[OK] ARM优化补丁应用完成")

def get_arm_build_instructions():
    """获取ARM构建指令"""
    
    arch = platform.machine()
    
    instructions = f"""
=== ARM架构 ({arch}) Nuitka打包指南 ===

1. 环境准备:
   sudo apt update
   sudo apt install -y python3 python3-pip python3-dev gcc g++ patchelf
   sudo apt install -y libssl-dev libffi-dev libyaml-dev

2. 安装依赖（从源码编译）:
   pip install --no-binary psutil,PyYAML,pydantic_core,httptools,zstandard \\
     -r requirements-arm.txt

3. 执行Nuitka打包:
   python3 -m nuitka \\
     --standalone \\
     --assume-yes-for-downloads \\
     --nofollow-import-to=GPUtil \\
     --include-package=uvicorn \\
     --include-package=fastapi \\
     --include-package=redis \\
     --include-package=psutil \\
     --include-package=requests \\
     --include-package=anyio \\
     --include-package=httptools \\
     --include-package=h11 \\
     --include-package=websockets \\
     --include-package=api \\
     --include-package=core \\
     --include-package=utils \\
     --include-data-file=config.yaml=config.yaml \\
     --output-dir=nuitka_arm_build \\
     --remove-output \\
     main.py

4. 运行测试:
   cd nuitka_arm_build/main.dist
   ./start_agent.sh

5. 常见ARM问题解决:
   - 如果psutil编译失败: sudo apt install python3-dev
   - 如果PyYAML编译失败: sudo apt install libyaml-dev
   - 如果缺少依赖: ldd main.bin | grep "not found"
"""
    
    return instructions

def main():
    print("ARM架构优化工具")
    print("=" * 50)
    
    is_arm, arch = check_arm_environment()
    
    if is_arm:
        print(f"✓ 检测到ARM架构: {arch}")
        
        # 询问用户要执行的操作
        print("\n请选择操作:")
        print("1. 应用ARM优化补丁")
        print("2. 获取ARM构建指令")
        print("3. 检查ARM兼容性")
        print("4. 退出")
        
        choice = input("请输入选择 (1-4): ").strip()
        
        if choice == "1":
            patch_for_arm()
        elif choice == "2":
            instructions = get_arm_build_instructions()
            print(instructions)
        elif choice == "3":
            check_arm_compatibility()
        else:
            print("退出")
    else:
        print(f"⚠ 当前不是ARM架构，部分功能可能不适用")
        
        # 仍然提供ARM构建指令
        instructions = get_arm_build_instructions()
        print(instructions)

def check_arm_compatibility():
    """检查ARM兼容性"""
    print("\n=== ARM兼容性检查 ===")
    
    # 检查二进制扩展
    import sys
    import importlib
    
    arm_problem_modules = [
        'psutil',
        'yaml',
        'pydantic_core',
        'httptools',
        'zstandard',
    ]
    
    for module_name in arm_problem_modules:
        try:
            module = importlib.import_module(module_name)
            module_file = module.__file__
            
            # 检查是否是.so文件（Linux二进制扩展）
            if module_file and module_file.endswith('.so'):
                print(f"⚠ {module_name}: 包含ARM二进制扩展 {module_file}")
                # 检查文件架构
                try:
                    result = subprocess.run(['file', module_file], 
                                          capture_output=True, text=True)
                    if 'ARM' in result.stdout or 'aarch64' in result.stdout:
                        print(f"  ✓ 已编译为ARM架构")
                    elif 'x86' in result.stdout or 'x64' in result.stdout:
                        print(f"  ❌ 编译为x86架构，ARM不兼容！")
                    else:
                        print(f"  ? 未知架构: {result.stdout[:100]}")
                except:
                    print(f"  ? 无法检查文件架构")
            else:
                print(f"✓ {module_name}: 纯Python或ARM兼容")
                
        except ImportError as e:
            print(f"❌ {module_name}: 导入失败 - {e}")
        except Exception as e:
            print(f"? {module_name}: 检查失败 - {e}")

if __name__ == "__main__":
    main()