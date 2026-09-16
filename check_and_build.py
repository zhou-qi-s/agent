#!/usr/bin/env python3
"""
检查并完成 Nuitka 打包
"""

import os
import subprocess
import sys
import time

def check_existing_build():
    """检查现有的构建目录"""
    print("检查现有构建状态...")

    build_dirs = ["nuitka_build", "nuitka_build_new", "nuitka_final"]

    for dir_name in build_dirs:
        if os.path.exists(dir_name):
            print(f"  [OK] 发现构建目录: {dir_name}")

            # 检查是否有 main.dist
            dist_path = os.path.join(dir_name, "main.dist")
            if os.path.exists(dist_path):
                print(f"    发现 dist 目录: {dist_path}")

                # 检查可执行文件
                exe_path = os.path.join(dist_path, "main.exe")
                if os.path.exists(exe_path):
                    exe_size = os.path.getsize(exe_path) / (1024*1024)
                    print(f"    [OK] 可执行文件已存在: {exe_path} ({exe_size:.2f} MB)")
                    return True
                else:
                    print("    [ERR] 可执行文件不存在")
            else:
                print("    [ERR] dist 目录不存在")
        else:
            print(f"  [ERR] 目录不存在: {dir_name}")

    return False

def run_nuitka_build():
    """执行 Nuitka 打包"""
    print("\n开始 Nuitka 打包...")

    # 清理旧的构建目录
    if os.path.exists("nuitka_final"):
        print("清理旧的构建目录...")
        import shutil
        shutil.rmtree("nuitka_final", ignore_errors=True)

    install_cmd = [
        sys.executable, "-m", "pip", "install", "-U",
        "nuitka", "ordered-set", "zstandard",
        "--disable-pip-version-check"
    ]
    build_env = os.environ.copy()
    build_env["CLCACHE_DISABLE"] = "1"

    # 构建命令
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--nofollow-import-to=GPUtil",
        "--output-dir=nuitka_final",
        "--include-data-file=config.yaml=config.yaml",
        "--remove-output",
        "--jobs=2",
        "main.py"
    ]

    print(f"执行命令: {' '.join(install_cmd)}")
    try:
        install_result = subprocess.run(install_cmd, capture_output=True, text=True, env=build_env)
        if install_result.returncode != 0:
            print("\n[ERR] Nuitka 环境准备失败!")
            print(f"错误输出:\n{install_result.stderr}")
            return False

        print(f"执行命令: {' '.join(cmd)}")
        print("这可能需要几分钟时间，请耐心等待...")

        # 执行打包
        result = subprocess.run(cmd, capture_output=True, text=True, env=build_env)

        if result.returncode == 0:
            print("\n[OK] 打包成功!")
            
            # 检查生成的文件
            exe_path = "nuitka_final/main.dist/main.exe"
            if os.path.exists(exe_path):
                exe_size = os.path.getsize(exe_path) / (1024*1024)
                print(f"生成的可执行文件: {exe_path} ({exe_size:.2f} MB)")
                
                # 检查配置是否已包含
                config_path = "nuitka_final/main.dist/config.yaml"
                if os.path.exists(config_path):
                    print(f"配置文件已包含: {config_path}")
                else:
                    print(f"警告: 配置文件未找到")
                
                return True
            else:
                print(f"错误: 可执行文件未生成")
                return False
        else:
            print("\n[ERR] 打包失败!")
            print(f"错误输出:\n{result.stderr}")
            return False

    except Exception as e:
        print(f"\n[ERR] 打包过程中出现异常: {e}")
        return False

def main():
    print("=" * 60)
    print("Agent 项目 Nuitka 打包检查工具")
    print("=" * 60)

    # 检查现有构建
    if check_existing_build():
        print("\n[OK] 现有构建已存在，无需重新打包")
        return
    
    # 执行打包
    if run_nuitka_build():
        print("\n" + "=" * 60)
        print("打包完成!")
        print("使用方法:")
        print("  1. 进入目录: cd nuitka_final/main.dist")
        print("  2. 运行程序: main.exe")
        print("  3. 访问 API: http://localhost:8000/docs")
        print("=" * 60)
    else:
        print("\n[ERR] 打包失败，请检查错误信息")

if __name__ == "__main__":
    main()