#!/usr/bin/env bash
# =============================================================================
# 在【老 glibc 容器】里编 Agent（由 .github/workflows/build.yml 调用）
#
# 为什么要有这个文件：
#   Nuitka 编出来的 main.bin 动态链 libc，链接时会把【编译机】的 glibc 符号版本
#   刻进 ELF。原来直接在 ubuntu-22.04 runner 上编 → 要求 GLIBC_2.35
#   → 麒麟 V10（glibc 2.28）加载即失败：version `GLIBC_2.35' not found。
#   放进 manylinux_2_28 容器（AlmaLinux 8 / glibc 2.28）里编，floor 就降到 2.28。
#
# ★ 注意：这里【不把 build_offline.sh 的输出 pipe 给 tail】—— 之前那样写会把
#   编译器的退出码吞掉，编译 FATAL 也报成功（"绿了≠有产物"）。
#   现在 set -e + 不 pipe，失败就真失败。
# =============================================================================
set -euxo pipefail

PY="${PYTHON_BIN:-/opt/python/cp39-cp39/bin/python}"
export PATH="$(dirname "$PY"):$PATH"

echo '================ 编译环境（决定产物 glibc floor）================'
head -3 /etc/os-release || true
ldd --version | head -1
"$PY" --version
gcc --version | head -1
echo "PATH=$PATH"
echo '==============================================================='

# patchelf：build_offline.sh 里 require_command 会检查它
command -v patchelf >/dev/null 2>&1 || "$PY" -m pip install -U patchelf

"$PY" -m pip install -U pip setuptools wheel
"$PY" -m pip install -U nuitka ordered-set zstandard
"$PY" -m pip install -r requirements.txt

"$PY" -c "import psutil, requests, redis, yaml; print('base deps ok')"
"$PY" -c "import fastapi, uvicorn, websockets, httptools, anyio, h11; print('fastapi deps ok')"

# ★ 必须用 build_offline.sh（完整离线打包流水线）：
#   它生成的 stop_agent.sh 含「第 4.5 段 —— 停止时一并清 promtail」。
#   ✗ 不要换成 nuitka_linux_build.sh：那个脚本生成的 stop_agent.sh 没有该段，
#     会导致「取消纳管后节点上的 promtail 仍在继续往 Loki 推日志」。
chmod +x build_offline.sh
PYTHON_BIN="$PY" ./build_offline.sh

ls -l linux_release/agent-linux.tar.gz
