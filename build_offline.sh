#!/usr/bin/env bash
# 离线打包脚本：假设所有依赖已在系统 Python 中安装，跳过 pip install 步骤
# 用法: ./build_offline.sh   （或 PYTHON_BIN=python3 ./build_offline.sh）
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

# 校验关键依赖是否已安装
echo "[INFO] 校验关键依赖..."
"$PYTHON_BIN" -c "import psutil, requests, redis, yaml; print('基础依赖 ok')"
"$PYTHON_BIN" -c "import fastapi, uvicorn, websockets, httptools, anyio, h11; print('FastAPI 依赖 ok')"
"$PYTHON_BIN" -c "import ordered_set, zstandard; print('Nuitka 辅助依赖 ok')" || true

if ! "$PYTHON_BIN" -m nuitka --version >/dev/null 2>&1; then
  echo "[ERR] Nuitka 不可用，请先安装: $PYTHON_BIN -m pip install nuitka"
  exit 1
fi

rm -rf "$OUTPUT_DIR" "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

export CLCACHE_DISABLE=1

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
  --include-data-file=core/remove-k8s.sh=core/remove-k8s.sh \
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

# ---- 启动脚本（守护进程模式：崩溃自动拉起 + 开机自启） ----
cat > "$DIST_DIR/start_agent.sh" <<'STARTSH'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME=APP_NAME_PLACEHOLDER
PID_FILE=PID_FILE_PLACEHOLDER
GUARD_PID_FILE=guard.pid
LOG_FILE=LOG_FILE_PLACEHOLDER
STOP_FLAG=guard.stop

# ===== 若旧 agent/守护进程仍在运行，先停止（自动重启） =====
if [[ -f "$GUARD_PID_FILE" ]]; then
    OLD_GUARD_PID="$(cat "$GUARD_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OLD_GUARD_PID" ]] && kill -0 "$OLD_GUARD_PID" >/dev/null 2>&1; then
        echo "检测到旧守护进程 (PID: $OLD_GUARD_PID)，先停止..."
        touch "$STOP_FLAG"
        kill "$OLD_GUARD_PID" 2>/dev/null || true
        sleep 1
    fi
    rm -f "$GUARD_PID_FILE"
fi

if [[ -f "$PID_FILE" ]]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" >/dev/null 2>&1; then
        echo "检测到旧 agent (PID: $OLD_PID)，先停止..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$OLD_PID" >/dev/null 2>&1; then
            kill -9 "$OLD_PID" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
fi

rm -f "$STOP_FLAG"

# ===== 注册开机自启（crontab @reboot） =====
CRON_LINE="@reboot cd $SCRIPT_DIR && ./start_agent.sh"
if ! crontab -l 2>/dev/null | grep -F "$CRON_LINE" >/dev/null 2>&1; then
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab - 2>/dev/null \
        && echo "已注册开机自启" \
        || echo "警告：注册开机自启失败（可能需要 root 权限）"
fi

# ===== 启动守护进程（负责崩溃自动拉起） =====
nohup bash guard_agent.sh > /dev/null 2>&1 &
GUARD_PID=$!
echo "$GUARD_PID" > "$GUARD_PID_FILE"
echo "agent 守护进程已启动 (PID: $GUARD_PID)"
STARTSH

# ---- 守护脚本（崩溃自动拉起 + 启动失败保护） ----
cat > "$DIST_DIR/guard_agent.sh" <<'GUARDSH'
#!/usr/bin/env bash
# agent 守护进程：agent 崩溃/退出后自动拉起
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME=APP_NAME_PLACEHOLDER
PID_FILE=PID_FILE_PLACEHOLDER
LOG_FILE=LOG_FILE_PLACEHOLDER
STOP_FLAG=guard.stop

# 连续启动失败次数上限，超过后停止自动拉起（避免无限重启循环）
MAX_FAIL=3
FAIL_COUNT=0

rm -f "$STOP_FLAG"

while true; do
    if [[ -f "$STOP_FLAG" ]]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') 收到停止信号，守护进程退出"
        exit 0
    fi

    AGENT_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -z "$AGENT_PID" ]] || ! kill -0 "$AGENT_PID" >/dev/null 2>&1; then
        # 拉起 agent
        nohup "./$APP_NAME" >> "$LOG_FILE" 2>&1 &
        NEW_PID=$!
        echo "$NEW_PID" > "$PID_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') agent 已拉起 (PID: $NEW_PID)" >> "$LOG_FILE"

        # 等待 5 秒，检查是否启动成功（进程是否存活）
        sleep 5
        if ! kill -0 "$NEW_PID" >/dev/null 2>&1; then
            FAIL_COUNT=$((FAIL_COUNT + 1))
            echo "$(date '+%Y-%m-%d %H:%M:%S') agent 启动失败（第 $FAIL_COUNT 次），请检查 $LOG_FILE 中的错误" >> "$LOG_FILE"
            if [[ "$FAIL_COUNT" -ge "$MAX_FAIL" ]]; then
                echo "$(date '+%Y-%m-%d %H:%M:%S') 连续 $MAX_FAIL 次启动失败，守护进程停止自动拉起。请修复问题后执行 ./start_agent.sh 重新启动" >> "$LOG_FILE"
                # 停止自动拉起，守护进程保持运行，仅响应停止信号
                while true; do
                    if [[ -f "$STOP_FLAG" ]]; then
                        echo "$(date '+%Y-%m-%d %H:%M:%S') 收到停止信号，守护进程退出"
                        exit 0
                    fi
                    sleep 60
                done
            fi
            continue
        fi
        # 启动成功，重置失败计数
        FAIL_COUNT=0
    fi
    sleep 5
done
GUARDSH

sed -i "s|APP_NAME_PLACEHOLDER|$EXECUTABLE_NAME|g" "$DIST_DIR/start_agent.sh" "$DIST_DIR/guard_agent.sh"
sed -i "s|PID_FILE_PLACEHOLDER|$PID_FILE|g" "$DIST_DIR/start_agent.sh" "$DIST_DIR/guard_agent.sh"
sed -i "s|LOG_FILE_PLACEHOLDER|$LOG_FILE|g" "$DIST_DIR/start_agent.sh" "$DIST_DIR/guard_agent.sh"
chmod +x "$DIST_DIR/start_agent.sh" "$DIST_DIR/guard_agent.sh"

# ---- 停止脚本（先停守护进程，再停 agent） ----
# 旧版先停 agent、再靠 guard.pid 停守护进程，存在两个致命问题：
#   1) guard.pid 可能缺失（Agent 被手工 setsid 命令拉起，绕过了 start_agent.sh），
#      于是守护进程根本没被杀掉，5 秒后把 agent 重新拉起 ——
#      表现为"脚本退出码 0、后端以为停成功，但 Agent 仍在运行"；
#   2) 脚本末尾无条件 rm 掉 guard.stop 停止标志，守护进程存活时再也看不到停止信号；
#   3) 没有注销 start_agent.sh 注册的 crontab @reboot 开机自启，
#      机器重启后守护进程与 agent 会被再次拉起，"取消纳管"形同虚设。
# 新版改为：先注销开机自启，再用 pgrep 兜底确保守护进程真的退出，再停 agent，
#          最后确认无残留才清除停止标志。
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
GUARD_PID_FILE=guard.pid

AGENT_STATUS="未运行"
if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" >/dev/null 2>&1; then
        AGENT_STATUS="运行中 (PID: $PID)"
    fi
fi

GUARD_STATUS="未运行"
if [[ -f "$GUARD_PID_FILE" ]]; then
    GPID="$(cat "$GUARD_PID_FILE" 2>/dev/null || true)"
    if [[ -n "$GPID" ]] && kill -0 "$GPID" >/dev/null 2>&1; then
        GUARD_STATUS="运行中 (PID: $GPID)"
    fi
fi

echo "agent: $AGENT_STATUS"
echo "守护进程: $GUARD_STATUS"

if [[ "$AGENT_STATUS" == "未运行" ]]; then
    exit 1
fi
STATUSH

sed -i "s|PID_FILE_PLACEHOLDER|$PID_FILE|g" "$DIST_DIR/status_agent.sh"
chmod +x "$DIST_DIR/status_agent.sh"

tar -czf "$RELEASE_DIR/${APP_NAME}-linux.tar.gz" -C "$OUTPUT_DIR" main.dist

echo "[OK] 打包完成"
echo "[OK] 目录: $DIST_DIR"
echo "[OK] 启动脚本: $DIST_DIR/start_agent.sh"
echo "[OK] 守护脚本: $DIST_DIR/guard_agent.sh"
echo "[OK] 停止脚本: $DIST_DIR/stop_agent.sh"
echo "[OK] 状态脚本: $DIST_DIR/status_agent.sh"
echo "[OK] 压缩包: $RELEASE_DIR/${APP_NAME}-linux.tar.gz"
echo "[OK] 使用方式:"
echo "      启动: cd $DIST_DIR && ./start_agent.sh"
echo "      停止: cd $DIST_DIR && ./stop_agent.sh"
echo "      状态: cd $DIST_DIR && ./status_agent.sh"
