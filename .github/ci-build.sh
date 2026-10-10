#!/usr/bin/env bash
# =============================================================================
# 在【老 glibc 的发行版容器】里编 Agent（由 .github/workflows/build.yml 调用）
#
# 为什么要有这个文件：
#   Nuitka 编出来的 main.bin 动态链 libc，链接时会把【编译机】的 glibc 符号版本
#   刻进 ELF。原来直接在 ubuntu-22.04 runner 上编 → 产物要求 GLIBC_2.35
#   → 麒麟 V10（glibc 2.28）加载即失败：version `GLIBC_2.35' not found。
#   现在放进 almalinux:8（el8，glibc 2.28，与麒麟 V10 同代）里编 → floor = 2.28 ✓
#
# ⚠️ 踩过的坑（别再用 manylinux 镜像）：
#   quay.io/pypa/manylinux* 里的 python 是【静态】的（没有 libpython3.x.so），
#   而 Nuitka --standalone 必须要共享 libpython →
#       FATAL: Automatic detection of static libpython failed.
#       Nuitka on 'manylinux' has no shared libraries.
#   所以必须用发行版自带的 python（almalinux8 的 python39 有 libpython3.9.so.1.0）。
#
# ★ 这里的输出【不 pipe 给 tail】—— 之前那样写会把编译器退出码吞掉，
#   编译 FATAL 也报 success（"绿了≠有产物"）。set -e 下失败就是失败。
# =============================================================================
set -euxo pipefail

echo '================ 编译环境（决定产物 glibc floor）================'
head -3 /etc/os-release || true
ldd --version | head -1
gcc --version | head -1 || true
echo '==============================================================='

# 1) 装编译环境（容器里跑；glibc 由镜像决定，不装任何 glibc 相关东西）
if command -v dnf >/dev/null 2>&1; then
  dnf install -y gcc gcc-c++ binutils make tar gzip python39 python39-devel python39-pip \
    || { dnf module enable -y python39 && dnf install -y gcc gcc-c++ binutils make tar gzip python39 python39-devel python39-pip; }
fi

# 2) 挑 python：优先 python3.9（带共享 libpython），其次 PYTHON_BIN 指定，最后 python3
if [ -n "${PYTHON_BIN:-}" ] && [ -x "${PYTHON_BIN}" ]; then
  PY="$PYTHON_BIN"
elif command -v python3.9 >/dev/null 2>&1; then
  PY="$(command -v python3.9)"
else
  PY="$(command -v python3)"
fi
export PATH="$(dirname "$PY"):$PATH"
"$PY" --version

# 打印 libpython 情况（Nuitka --standalone 就靠它）
"$PY" -c "import sysconfig; print('LIBDIR  =', sysconfig.get_config_var('LIBDIR')); print('LDLIBRARY =', sysconfig.get_config_var('LDLIBRARY')); print('Py_ENABLE_SHARED =', sysconfig.get_config_var('Py_ENABLE_SHARED'))"

# 3) pip 依赖
"$PY" -m pip --version || "$PY" -m ensurepip --upgrade
"$PY" -m pip install -U pip setuptools wheel
# patchelf：build_offline.sh 里 require_command 会检查它
command -v patchelf >/dev/null 2>&1 || "$PY" -m pip install -U patchelf
"$PY" -m pip install -U nuitka ordered-set zstandard
"$PY" -m pip install -r requirements.txt

"$PY" -c "import psutil, requests, redis, yaml; print('base deps ok')"
"$PY" -c "import fastapi, uvicorn, websockets, httptools, anyio, h11; print('fastapi deps ok')"

# 4) 打包
# ★ 必须用 build_offline.sh（完整离线打包流水线）：
#   它生成的 stop_agent.sh 含「第 4.5 段 —— 停止时一并清 promtail」。
#   ✗ 不要换成 nuitka_linux_build.sh：那个脚本生成的 stop_agent.sh 没有该段，
#     会导致「取消纳管后节点上的 promtail 仍在继续往 Loki 推日志」。
chmod +x build_offline.sh
PYTHON_BIN="$PY" ./build_offline.sh

ls -l linux_release/agent-linux.tar.gz
