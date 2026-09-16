#!/usr/bin/env bash
set -euo pipefail

# ============================================
# ARM离线打包脚本（无互联网连接）
# 适用于企业内网/离线环境
# ============================================

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

# 配置参数
TARGET_ARCH="aarch64"  # 或 armv7l
OFFLINE_DEPS_DIR="offline_deps"
BUILD_DIR="offline_build_${TARGET_ARCH}"
OUTPUT_DIR="nuitka_${TARGET_ARCH}_offline"
RELEASE_DIR="${TARGET_ARCH}_offline_release"

echo "[INFO] ARM离线打包: ${TARGET_ARCH}"
echo "[INFO] 离线依赖目录: ${OFFLINE_DEPS_DIR}"
echo "=" * 60

# 检查离线依赖
check_offline_deps() {
    echo "[STEP 1] 检查离线依赖..."
    
    if [[ ! -d "$OFFLINE_DEPS_DIR" ]]; then
        echo "[ERROR] 离线依赖目录不存在: $OFFLINE_DEPS_DIR"
        echo "请先在有网络的机器上准备离线依赖包："
        echo ""
        echo "准备工作脚本:"
        echo "-----------------------"
        cat << 'PREP_EOF'
mkdir -p offline_deps/{arm64,source,tools}

# 1. 下载ARM架构的wheel包
pip download \
  --platform manylinux2014_aarch64 \
  --python-version 39 \
  --implementation cp \
  --abi cp39 \
  --only-binary=:all: \
  -r requirements.txt \
  -d offline_deps/arm64/

# 2. 下载源码包（用于编译）
pip download \
  --no-binary psutil,PyYAML,pydantic_core,httptools,zstandard \
  -r requirements.txt \
  -d offline_deps/source/

# 3. 下载构建工具
pip download nuitka ordered-set zstandard -d offline_deps/tools/

# 4. 创建requirements离线版
cp requirements.txt offline_deps/requirements-offline.txt

# 5. 打包所有依赖
tar -czf offline_deps.tar.gz offline_deps/
PREP_EOF
        echo "-----------------------"
        echo "将生成的 offline_deps.tar.gz 复制到离线环境"
        exit 1
    fi
    
    # 检查关键依赖
    required_dirs=("arm64" "source" "tools")
    for dir in "${required_dirs[@]}"; do
        if [[ ! -d "$OFFLINE_DEPS_DIR/$dir" ]]; then
            echo "[WARN] 缺少目录: $OFFLINE_DEPS_DIR/$dir"
        fi
    done
    
    # 检查关键文件
    if [[ ! -f "requirements.txt" ]]; then
        echo "[ERROR] 缺少 requirements.txt"
        exit 1
    fi
    
    echo "[OK] 离线依赖检查完成"
}

# 安装离线依赖
install_offline_deps() {
    echo "[STEP 2] 安装离线依赖..."
    
    # 1. 安装构建工具
    if [[ -d "$OFFLINE_DEPS_DIR/tools" ]]; then
        echo "安装构建工具..."
        pip install --no-index --find-links="$OFFLINE_DEPS_DIR/tools" \
          nuitka ordered-set zstandard --disable-pip-version-check
    else
        echo "[WARN] 使用系统已安装的构建工具"
    fi
    
    # 2. 尝试安装预编译的ARM wheel包
    echo "尝试安装预编译ARM包..."
    if [[ -d "$OFFLINE_DEPS_DIR/arm64" ]]; then
        pip install --no-index --find-links="$OFFLINE_DEPS_DIR/arm64" \
          -r requirements.txt --disable-pip-version-check || true
    fi
    
    # 3. 对于无法安装预编译包的，尝试从源码编译
    echo "从源码编译剩余依赖..."
    if [[ -d "$OFFLINE_DEPS_DIR/source" ]]; then
        # 创建临时requirements文件，排除已安装的包
        temp_req="requirements_source.txt"
        python3 -c "
import pkg_resources
import sys

# 读取原始requirements
with open('requirements.txt', 'r') as f:
    lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]

# 检查已安装的包
installed = {pkg.key for pkg in pkg_resources.working_set}

# 筛选需要从源码安装的包
source_packages = []
for line in lines:
    if '==' in line:
        pkg_name = line.split('==')[0].strip().lower().replace('-', '_')
    else:
        pkg_name = line.split('>=')[0].strip().lower().replace('-', '_')
    
    if pkg_name not in installed:
        source_packages.append(line)

# 写入临时文件
with open('$temp_req', 'w') as f:
    for pkg in source_packages:
        f.write(pkg + '\n')
print(f'需要从源码安装 {len(source_packages)} 个包')
"
        
        if [[ -s "$temp_req" ]]; then
            pip install --no-index --find-links="$OFFLINE_DEPS_DIR/source" \
              -r "$temp_req" --no-deps --disable-pip-version-check || {
                echo "[WARN] 部分包源码编译失败，尝试简化安装..."
            }
            rm -f "$temp_req"
        fi
    fi
    
    echo "[OK] 离线依赖安装完成"
}

# 创建离线优化的requirements
create_offline_requirements() {
    echo "[STEP 3] 创建离线优化配置..."
    
    # 1. 修改requirements.txt，添加离线源
    cat > "requirements-offline.txt" << 'EOF'
# === 离线环境专用依赖配置 ===
# 注意：在离线环境中使用以下配置
# 安装命令: pip install --no-index --find-links=./offline_deps/arm64/ -r requirements-offline.txt

# 基础依赖（优先使用预编译wheel）
psutil==5.9.0
requests==2.31.0
redis==5.0.1
PyYAML==6.0.1
fastapi==0.104.1
uvicorn[standard]==0.24.0
pydantic==2.5.0
python-multipart==0.0.6
websockets==12.0

# ARM优化：禁用GPU相关依赖
# GPUtil==1.4.0  # ARM离线环境通常无GPU，已注释

# 打包工具（开发用）
# pytest==7.4.3
# black==23.11.0
# flake8==6.1.0
# pyinstaller==6.3.0

# Nuitka相关
nuitka>=2.6
ordered-set>=4.1.0
zstandard>=0.22.0

# === 离线安装说明 ===
# 1. 首先尝试安装预编译包:
#    pip install --no-index --find-links=./offline_deps/arm64/ -r requirements-offline.txt
#
# 2. 如果失败，尝试从源码编译:
#    pip install --no-index --find-links=./offline_deps/source/ --no-binary psutil,PyYAML,pydantic_core,httptools,zstandard -r requirements-offline.txt
#
# 3. 如果仍然失败，手动安装:
#    cd offline_deps/source/
#    for pkg in *.tar.gz; do tar -xzf "\$pkg" && cd "\${pkg%.tar.gz}" && python setup.py install && cd ..; done
EOF
    
    # 2. 创建离线配置文件
    cat > "config-offline.yaml" << 'CONFIG_EOF'
# 离线环境专用配置
server:
  # 使用局域网IP
  iP: http://192.168.1.1  # 请根据实际修改
  port: 30000
  compartment: CPT-CMD-004
  redis:
    host: 192.168.1.100   # 局域网Redis服务器
    port: 6379
    db: 0
    password: null
  
  fastapi:
    host: 0.0.0.0
    port: 8000

# 离线优化
offline:
  enable: true
  # 禁用需要网络的功能
  disable_auto_update: true
  disable_external_checks: true
  # 资源限制
  resource_limits:
    max_cpu_percent: 80
    max_memory_mb: 1024

# ARM架构优化
arm:
  architecture: aarch64
  # 禁用ARM不兼容的功能
  disable_gpu_monitoring: true
  use_system_psutil: false
CONFIG_EOF
    
    echo "[OK] 离线配置创建完成"
}

# 执行Nuitka离线打包
build_offline_nuitka() {
    echo "[STEP 4] 执行Nuitka离线打包..."
    
    # 清理旧构建
    rm -rf "$BUILD_DIR" "$OUTPUT_DIR" "$RELEASE_DIR"
    
    # 检查Python环境
    PYTHON_BIN="python3"
    if ! command -v "$PYTHON_BIN" >/dev/null; then
        PYTHON_BIN="python"
    fi
    
    # 检查Nuitka是否安装
    if ! "$PYTHON_BIN" -c "import nuitka" 2>/dev/null; then
        echo "[ERROR] Nuitka未安装，尝试从离线包安装..."
        if [[ -d "$OFFLINE_DEPS_DIR/tools" ]]; then
            pip install --no-index --find-links="$OFFLINE_DEPS_DIR/tools" nuitka
        else
            echo "[ERROR] 无法安装Nuitka，请检查离线依赖"
            exit 1
        fi
    fi
    
    # ARM架构参数
    ARM_FLAGS=""
    if [[ "$TARGET_ARCH" == "aarch64" ]]; then
        ARM_FLAGS="--target-arch=arm64"
        echo "[INFO] 目标架构: ARM64 (aarch64)"
    elif [[ "$TARGET_ARCH" == "armv7l" ]]; then
        ARM_FLAGS="--target-arch=armv7"
        echo "[INFO] 目标架构: ARM32 (armv7l)"
    else
        echo "[WARN] 未知架构，使用默认参数"
    fi
    
    # 执行Nuitka打包
    echo "开始Nuitka打包过程..."
    "$PYTHON_BIN" -m nuitka \
      --standalone \
      --assume-yes-for-downloads \
      --nofollow-import-to=GPUtil \
      --plugin-enable=pylint-warnings \
      --include-package=uvicorn \
      --include-package=fastapi \
      --include-package=redis \
      --include-package=psutil \
      --include-package=requests \
      --include-package=anyio \
      --include-package=httptools \
      --include-package=h11 \
      --include-package=websockets \
      --include-package=api \
      --include-package=core \
      --include-package=utils \
      --include-data-file=config.yaml=config.yaml \
      --include-data-file=config-offline.yaml=config-offline.yaml \
      --output-dir="$OUTPUT_DIR" \
      --remove-output \
      $ARM_FLAGS \
      main.py
    
    # 检查构建结果
    DIST_DIR="$OUTPUT_DIR/main.dist"
    if [[ ! -d "$DIST_DIR" ]]; then
        echo "[ERROR] 构建失败，未找到输出目录"
        exit 1
    fi
    
    echo "[OK] Nuitka构建完成"
}

# 创建启动脚本和文档
create_deployment_files() {
    echo "[STEP 5] 创建部署文件..."
    
    DIST_DIR="$OUTPUT_DIR/main.dist"
    
    # 1. 创建启动脚本
    cat > "$DIST_DIR/start_offline_agent.sh" << 'START_EOF'
#!/usr/bin/env bash
set -euo pipefail

# ============================================
# 离线环境Agent启动脚本
# 适用于ARM架构无网络环境
# ============================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 环境检查
echo "[OFFLINE] 离线环境检查..."
echo "工作目录: $SCRIPT_DIR"
echo "架构: $(uname -m)"

# 检查配置文件优先级
CONFIG_FILE="config.yaml"
if [[ -f "config-offline.yaml" ]]; then
    echo "[INFO] 检测到离线配置文件，将使用离线配置"
    # 可以在这里添加配置合并逻辑
fi

# 查找可执行文件
EXECUTABLE=""
for candidate in main.bin main; do
    if [[ -x "./$candidate" ]]; then
        EXECUTABLE="./$candidate"
        break
    fi
done

if [[ -z "$EXECUTABLE" ]]; then
    EXECUTABLE=$(find . -maxdepth 1 -type f -perm -111 | head -n 1)
    if [[ -z "$EXECUTABLE" ]]; then
        echo "[ERROR] 未找到可执行文件"
        exit 1
    fi
fi

# 环境变量设置（离线优化）
export OFFLINE_MODE=1
export PYTHONOPTIMIZE=1  # 优化模式
export PYTHONHASHSEED=0  # 固定哈希种子

echo "[OFFLINE] 启动离线Agent..."
echo "[OFFLINE] 可执行文件: $EXECUTABLE"

# 启动服务
exec "$EXECUTABLE" "$@"
START_EOF
    
    chmod +x "$DIST_DIR/start_offline_agent.sh"
    
    # 2. 创建部署文档
    cat > "$DIST_DIR/DEPLOYMENT_GUIDE.md" << 'GUIDE_EOF'
# ARM离线环境部署指南

## 1. 环境要求
- ARM64 (aarch64) 或 ARM32 (armv7l) 架构
- Linux 系统 (Ubuntu/CentOS/Debian)
- Python 3.8+ 运行时（已包含在包内）
- 无需互联网连接

## 2. 部署步骤

### 2.1 单机部署
```bash
# 解压部署包
tar -xzf agent-arm64-offline.tar.gz -C /opt/

# 进入目录
cd /opt/main.dist

# 启动Agent
./start_offline_agent.sh

# 查看日志
tail -f agent.log
```

### 2.2 系统服务部署
```bash
# 创建systemd服务
sudo tee /etc/systemd/system/agent-offline.service << 'SERVICE_EOF'
[Unit]
Description=Offline Agent Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/main.dist
ExecStart=/opt/main.dist/start_offline_agent.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# 启用并启动服务
sudo systemctl daemon-reload
sudo systemctl enable agent-offline
sudo systemctl start agent-offline

# 查看状态
sudo systemctl status agent-offline
```

## 3. 配置文件说明

### 3.1 主配置文件 (config.yaml)
- 服务器地址、端口等基础配置

### 3.2 离线配置文件 (config-offline.yaml)
- 离线环境专用优化配置
- 资源限制设置
- ARM架构适配

## 4. 网络配置
确保以下端口可访问：
- 8000: Agent管理API
- 30000: 服务端通信（根据config.yaml配置）

## 5. 故障排除

### 5.1 启动失败
```bash
# 检查文件权限
chmod +x start_offline_agent.sh
chmod +x main.bin

# 检查依赖
ldd main.bin | grep "not found"

# 查看日志
cat agent.log
```

### 5.2 网络连接问题
- 检查config.yaml中的服务器IP是否正确
- 检查防火墙设置
- 验证网络连通性：ping <server_ip>

### 5.3 性能问题
- 调整config-offline.yaml中的资源限制
- 检查系统资源使用：top, free -m

## 6. 更新部署
离线环境更新需要手动操作：
1. 停止当前服务
2. 备份旧版本
3. 部署新版本
4. 启动服务
GUIDE_EOF
    
    # 3. 创建健康检查脚本
    cat > "$DIST_DIR/health_check.sh" << 'HEALTH_EOF'
#!/usr/bin/env bash
# 离线环境健康检查脚本

check_agent() {
    # 检查进程
    if pgrep -f "main.bin" >/dev/null || pgrep -f "main" >/dev/null; then
        echo "✓ Agent进程运行正常"
        return 0
    else
        echo "✗ Agent进程未运行"
        return 1
    fi
}

check_ports() {
    # 检查端口
    if ss -tln | grep -q ":8000 "; then
        echo "✓ API端口(8000)监听正常"
    else
        echo "✗ API端口未监听"
    fi
}

check_resources() {
    # 检查资源使用
    echo "系统资源检查:"
    free -m | awk 'NR==2{printf "内存: %s/%sMB (%.1f%%)\n", $3,$2,$3*100/$2}'
    top -bn1 | grep "Cpu(s)" | awk '{printf "CPU: %s\n", $2}'
}

check_logs() {
    # 检查日志
    if [[ -f "agent.log" ]]; then
        echo "最近日志:"
        tail -5 agent.log
    else
        echo "日志文件不存在"
    fi
}

echo "=== 离线Agent健康检查 ==="
echo "时间: $(date)"
echo "架构: $(uname -m)"
echo ""

check_agent
check_ports
echo ""
check_resources
echo ""
check_logs
HEALTH_EOF
    
    chmod +x "$DIST_DIR/health_check.sh"
    
    # 4. 打包发布
    mkdir -p "$RELEASE_DIR"
    tar -czf "$RELEASE_DIR/agent-${TARGET_ARCH}-offline.tar.gz" \
      -C "$OUTPUT_DIR" main.dist
    
    # 5. 创建MD5校验文件
    cd "$RELEASE_DIR"
    md5sum "agent-${TARGET_ARCH}-offline.tar.gz" > "agent-${TARGET_ARCH}-offline.tar.gz.md5"
    cd "$PROJECT_ROOT"
    
    echo "[OK] 部署文件创建完成"
}

# 主流程
main() {
    echo "开始ARM离线打包流程..."
    echo "=" * 60
    
    check_offline_deps
    install_offline_deps
    create_offline_requirements
    build_offline_nuitka
    create_deployment_files
    
    echo ""
    echo "=" * 60
    echo "[SUCCESS] ARM离线打包完成！"
    echo ""
    echo "输出文件:"
    echo "  1. 部署包: $RELEASE_DIR/agent-${TARGET_ARCH}-offline.tar.gz"
    echo "  2. MD5校验: $RELEASE_DIR/agent-${TARGET_ARCH}-offline.tar.gz.md5"
    echo "  3. 构建目录: $OUTPUT_DIR/"
    echo ""
    echo "部署步骤:"
    echo "  1. 复制部署包到ARM设备:"
    echo "     scp $RELEASE_DIR/agent-${TARGET_ARCH}-offline.tar.gz user@arm-device:/tmp/"
    echo "  2. 在ARM设备上执行:"
    echo "     tar -xzf /tmp/agent-${TARGET_ARCH}-offline.tar.gz -C /opt/"
    echo "     cd /opt/main.dist"
    echo "     ./start_offline_agent.sh"
    echo ""
    echo "离线验证:"
    echo "  cd /opt/main.dist && ./health_check.sh"
    echo "=" * 60
}

# 执行主流程
main "$@"