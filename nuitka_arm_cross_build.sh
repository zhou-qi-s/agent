#!/usr/bin/env bash
set -euo pipefail

# ============================================
# ARM架构交叉编译脚本（高级，需要Docker）
# 可以在x86机器上为ARM架构交叉编译
# ============================================

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

TARGET_ARCH="${1:-aarch64}"  # aarch64 或 armv7l
DOCKER_IMAGE="multiarch/ubuntu-core:${TARGET_ARCH}-focal"
BUILD_DIR="cross_build_${TARGET_ARCH}"
OUTPUT_DIR="nuitka_${TARGET_ARCH}_cross"
RELEASE_DIR="${TARGET_ARCH}_release"

echo "[INFO] ARM交叉编译: ${TARGET_ARCH}"
echo "[INFO] Docker镜像: ${DOCKER_IMAGE}"
echo "[INFO] 清理旧构建..."
rm -rf "$BUILD_DIR" "$OUTPUT_DIR" "$RELEASE_DIR"

# 创建构建目录
mkdir -p "$BUILD_DIR"
cp -r main.py api core utils config.yaml requirements.txt "$BUILD_DIR/"

# 创建Docker构建脚本
cat > "$BUILD_DIR/build_inside_docker.sh" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "[DOCKER] 在容器内构建ARM版本..."

# 更新和安装依赖
apt-get update
apt-get install -y python3 python3-pip python3-dev gcc g++ patchelf
apt-get install -y libssl-dev libffi-dev libyaml-dev

# 安装Nuitka
pip3 install -U pip nuitka ordered-set zstandard

# 安装项目依赖（从源码编译）
cd /build
pip3 install --no-binary psutil,PyYAML,pydantic_core,httptools,zstandard \
  -r requirements.txt

# ARM架构特定的Nuitka参数
ARCH=$(uname -m)
NUITKA_FLAGS=""

if [[ "$ARCH" == "aarch64" ]]; then
    echo "[DOCKER] 目标架构: aarch64 (ARM64)"
    NUITKA_FLAGS="--target-arch=arm64"
elif [[ "$ARCH" == "armv7l" ]]; then
    echo "[DOCKER] 目标架构: armv7l (ARM32)"
    NUITKA_FLAGS="--target-arch=armv7"
else
    echo "[DOCKER] 未知架构: $ARCH"
    exit 1
fi

# 执行Nuitka打包
echo "[DOCKER] 开始Nuitka打包..."
python3 -m nuitka \
  --standalone \
  --assume-yes-for-downloads \
  --nofollow-import-to=GPUtil \
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
  --output-dir="/output" \
  --remove-output \
  $NUITKA_FLAGS \
  main.py

# 验证构建结果
if [[ -d "/output/main.dist" ]]; then
    echo "[DOCKER] 构建成功!"
    # 检查文件架构
    file /output/main.dist/main.bin 2>/dev/null || file /output/main.dist/main 2>/dev/null || true
else
    echo "[DOCKER] 构建失败!"
    exit 1
fi
EOF

chmod +x "$BUILD_DIR/build_inside_docker.sh"

# 运行Docker构建
echo "[INFO] 启动Docker交叉编译..."
docker run --rm \
  -v "$(pwd)/$BUILD_DIR:/build" \
  -v "$(pwd)/$OUTPUT_DIR:/output" \
  "$DOCKER_IMAGE" \
  /bin/bash /build/build_inside_docker.sh

# 检查构建结果
if [[ -d "$OUTPUT_DIR/main.dist" ]]; then
    echo "[OK] 交叉编译成功!"
    
    # 创建启动脚本
    cat > "$OUTPUT_DIR/main.dist/start_agent.sh" << 'START_EOF'
#!/usr/bin/env bash
set -euo pipefail

# ARM架构检查
ARCH=$(uname -m)
EXPECTED_ARCH="$TARGET_ARCH"  # 替换为实际架构

if [[ "$ARCH" != "$EXPECTED_ARCH" ]]; then
    echo "[ERROR] 此程序为 $EXPECTED_ARCH 架构编译，当前架构: $ARCH"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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

echo "[INFO] 启动 $EXPECTED_ARCH Agent..."
exec "$EXECUTABLE" "$@"
START_EOF
    
    chmod +x "$OUTPUT_DIR/main.dist/start_agent.sh"
    
    # 打包发布
    mkdir -p "$RELEASE_DIR"
    tar -czf "$RELEASE_DIR/agent-${TARGET_ARCH}.tar.gz" -C "$OUTPUT_DIR" main.dist
    
    echo "[OK] 发布包: $RELEASE_DIR/agent-${TARGET_ARCH}.tar.gz"
    echo "[OK] 文件架构验证:"
    file "$OUTPUT_DIR/main.dist/"* 2>/dev/null | grep -E "ELF|ARM|aarch" || true
    
else
    echo "[ERR] 交叉编译失败"
    exit 1
fi

# 清理
rm -rf "$BUILD_DIR"

echo "[OK] ARM交叉编译完成!"
echo "[INFO] 部署到ARM设备:"
echo "       scp $RELEASE_DIR/agent-${TARGET_ARCH}.tar.gz user@arm-device:/tmp/"
echo "       ssh user@arm-device 'tar -xzf /tmp/agent-${TARGET_ARCH}.tar.gz -C /opt/'"
echo "       ssh user@arm-device 'cd /opt/main.dist && ./start_agent.sh'"