#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
OUTPUT_DIR="${OUTPUT_DIR:-nuitka_linux_build}"
RELEASE_DIR="${RELEASE_DIR:-linux_release}"
APP_NAME="${APP_NAME:-agent}"

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[ERR] 缺少命令: $cmd"
    exit 1
  fi
}

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "[ERR] 该脚本需要在 Linux 系统中执行，才能打出可在 Linux 上运行的包。"
  exit 1
fi

require_command "$PYTHON_BIN"
require_command gcc
require_command patchelf
require_command tar

rm -rf "$OUTPUT_DIR" "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

export CLCACHE_DISABLE=1

echo "[INFO] 安装/升级打包依赖..."
"$PYTHON_BIN" -m pip install -U pip --disable-pip-version-check
"$PYTHON_BIN" -m pip install -r requirements.txt --disable-pip-version-check
# 注意：不能对 anyio/h11/httptools 用 -U —— requirements.txt 里 fastapi 0.104.1 要求
# anyio<4.0.0，而 -U 会拉到 anyio 4.x，导致 Redis/HTTP 相关行为异常（实测 redis 连接失败）。
"$PYTHON_BIN" -m pip install nuitka ordered-set zstandard --disable-pip-version-check
"$PYTHON_BIN" -m pip install "anyio<4.0.0" httptools h11 --disable-pip-version-check

echo "[INFO] 开始 Nuitka 打包..."
"$PYTHON_BIN" -m nuitka \
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
  --output-dir="$OUTPUT_DIR" \
  --remove-output \
  main.py

DIST_DIR="$OUTPUT_DIR/main.dist"
if [[ ! -d "$DIST_DIR" ]]; then
  echo "[ERR] 未找到打包输出目录: $DIST_DIR"
  exit 1
fi

EXECUTABLE_NAME=""
for candidate in main.bin main; do
  if [[ -x "$DIST_DIR/$candidate" ]]; then
    EXECUTABLE_NAME="$candidate"
    break
  fi
done

if [[ -z "$EXECUTABLE_NAME" ]]; then
  EXECUTABLE_PATH="$(find "$DIST_DIR" -maxdepth 1 -type f -perm -111 | head -n 1 || true)"
  if [[ -z "$EXECUTABLE_PATH" ]]; then
    echo "[ERR] 未找到 Linux 可执行文件"
    exit 1
  fi
  EXECUTABLE_NAME="$(basename "$EXECUTABLE_PATH")"
fi

PID_FILE="./$EXECUTABLE_NAME.pid"
LOG_FILE="./$EXECUTABLE_NAME.log"

# ---- 启动脚本 ----
cat > "$DIST_DIR/start_agent.sh" <<'STARTSH'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME=APP_NAME_PLACEHOLDER
PID_FILE=PID_FILE_PLACEHOLDER
LOG_FILE=LOG_FILE_PLACEHOLDER

if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" >/dev/null 2>&1; then
        echo "agent 已在运行中 (PID: $OLD_PID)"
        exit 1
    fi
    rm -f "$PID_FILE"
fi

echo "启动 agent..."
nohup "./$APP_NAME" "$@" > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "agent 已启动 (PID: $NEW_PID)"
STARTSH

sed -i "s|APP_NAME_PLACEHOLDER|$EXECUTABLE_NAME|g" "$DIST_DIR/start_agent.sh"
sed -i "s|PID_FILE_PLACEHOLDER|$PID_FILE|g" "$DIST_DIR/start_agent.sh"
sed -i "s|LOG_FILE_PLACEHOLDER|$LOG_FILE|g" "$DIST_DIR/start_agent.sh"
chmod +x "$DIST_DIR/start_agent.sh"

# ---- 停止脚本（先注销开机自启，再停守护进程，最后停 agent） ----
# 本脚本只产出 start/stop/status 三个脚本，不生成 guard_agent.sh（守护进程），
# 但 stop_agent.sh 仍按"存在守护进程"的流程处理：目标机上的守护进程可能由
# build_offline.sh 安装或被人手工 setsid 拉起，一旦漏杀，5 秒后它会把 agent
# 重新拉起，而旧版脚本仍以退出码 0 结束 -> 后端误判"取消纳管成功"、Agent 却还在跑。
# 另外必须注销 start_agent.sh 注册的 crontab @reboot 自启，否则机器重启后复活。
# 无守护进程时 guard_pids() 返回空，以下流程自然跳过，不会误报。
# 关键告警走 stderr：后端 SSHUtils 在退出码非 0 时只把 stderr 回传给前端。
cat > "$DIST_DIR/stop_agent.sh" <<'STOPSH'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE=PID_FILE_PLACEHOLDER
GUARD_PID_FILE=guard.pid
STOP_FLAG=guard.stop

# 守护进程 PID 列表（pgrep 缺失时回退 ps）
guard_pids() {
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -f 'guard_agent\.sh' 2>/dev/null || true
    else
        ps -eo pid,args 2>/dev/null | awk '/[g]uard_agent\.sh/ {print $1}' || true
    fi
}

# Agent 进程 PID 列表
agent_pids() {
    if command -v pgrep >/dev/null 2>&1; then
        pgrep -f 'main\.bin' 2>/dev/null || true
    else
        ps -eo pid,args 2>/dev/null | awk '/[m]ain\.bin/ {print $1}' || true
    fi
}

# 1. 注销开机自启：只杀进程不够，机器重启后 @reboot 会把守护进程和 agent 一起拉起来
CRON_LINE="@reboot cd $SCRIPT_DIR && ./start_agent.sh"
if crontab -l 2>/dev/null | grep -Fq "$CRON_LINE"; then
    crontab -l 2>/dev/null | grep -vF "$CRON_LINE" | crontab - 2>/dev/null || true
    echo "已注销开机自启"
fi

# 2. 放停止标志：即使守护进程没被杀干净，它下一轮检查也会自行退出
touch "$STOP_FLAG"

# 3. 停止守护进程（必须在 agent 之前，否则它会立刻把 agent 重新拉起）
PIDS="$( { cat "$GUARD_PID_FILE" 2>/dev/null || true; guard_pids; } | sort -n -u | tr '\n' ' ' )"
for p in $PIDS; do
    if [[ -n "$p" && "$p" != "$$" ]]; then
        if kill -0 "$p" >/dev/null 2>&1; then
            echo "正在停止守护进程 (PID: $p)..."
            kill "$p" 2>/dev/null || true
        fi
    fi
done
rm -f "$GUARD_PID_FILE"

# 等守护进程退出（最多 5 秒），仍未退出则强制终止
for i in $(seq 1 10); do
    if [[ -z "$(guard_pids)" ]]; then
        break
    fi
    sleep 0.5
done
if [[ -n "$(guard_pids)" ]]; then
    echo "守护进程未响应，强制终止..." >&2
    for p in $(guard_pids); do
        if [[ "$p" != "$$" ]]; then
            kill -9 "$p" 2>/dev/null || true
        fi
    done
    sleep 1
fi

# 4. 停止 agent 进程
PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -n "$PID" ]] && kill -0 "$PID" >/dev/null 2>&1; then
    echo "正在停止 agent (PID: $PID)..."
    kill "$PID" 2>/dev/null || true
    for i in $(seq 1 20); do
        if ! kill -0 "$PID" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
    done
    if kill -0 "$PID" >/dev/null 2>&1; then
        echo "优雅停止超时，强制终止..." >&2
        kill -9 "$PID" 2>/dev/null || true
    fi
fi
rm -f "$PID_FILE"

# pid 文件缺失时兜底：按进程名清理残留 agent
for p in $(agent_pids); do
    if [[ "$p" != "$$" ]]; then
        kill "$p" 2>/dev/null || true
    fi
done
sleep 1
for p in $(agent_pids); do
    if [[ "$p" != "$$" ]]; then
        kill -9 "$p" 2>/dev/null || true
    fi
done

# 5. 只有确认没有残留才清除停止标志；否则保留标志，防止 Agent 被重新拉起
if [[ -n "$(guard_pids)$(agent_pids)" ]]; then
    echo "警告：仍有 Agent/守护进程残留，保留 $STOP_FLAG 以防被拉起，请手工检查" >&2
    exit 1
fi
rm -f "$STOP_FLAG"
echo "agent 已停止"
STOPSH

sed -i "s|PID_FILE_PLACEHOLDER|$PID_FILE|g" "$DIST_DIR/stop_agent.sh"
chmod +x "$DIST_DIR/stop_agent.sh"

# ---- 状态脚本 ----
cat > "$DIST_DIR/status_agent.sh" <<'STATUSH'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE=PID_FILE_PLACEHOLDER

if [[ ! -f "$PID_FILE" ]]; then
    echo "agent: 未运行"
    exit 1
fi

PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "$PID" ]]; then
    echo "agent: 未运行 (PID 文件为空)"
    exit 1
fi

if kill -0 "$PID" >/dev/null 2>&1; then
    echo "agent: 运行中 (PID: $PID)"
else
    echo "agent: 已停止 (PID: $PID 进程不存在)"
    exit 1
fi
STATUSH

sed -i "s|PID_FILE_PLACEHOLDER|$PID_FILE|g" "$DIST_DIR/status_agent.sh"
chmod +x "$DIST_DIR/status_agent.sh"

tar -czf "$RELEASE_DIR/${APP_NAME}-linux.tar.gz" -C "$OUTPUT_DIR" main.dist

echo "[OK] 打包完成"
echo "[OK] 目录: $DIST_DIR"
echo "[OK] 启动脚本: $DIST_DIR/start_agent.sh"
echo "[OK] 停止脚本: $DIST_DIR/stop_agent.sh"
echo "[OK] 状态脚本: $DIST_DIR/status_agent.sh"
echo "[OK] 压缩包: $RELEASE_DIR/${APP_NAME}-linux.tar.gz"
echo "[OK] 使用方式:"
echo "      启动: cd $DIST_DIR && ./start_agent.sh"
echo "      停止: cd $DIST_DIR && ./stop_agent.sh"
echo "      状态: cd $DIST_DIR && ./status_agent.sh"
