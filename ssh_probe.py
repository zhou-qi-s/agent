# -*- coding: utf-8 -*-
"""探查 192.168.0.4 源码目录与配置"""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import paramiko

HOST = "192.168.0.4"
USER = "bontor"
PASS = "bontor@123"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, port=22, username=USER, password=PASS, timeout=15)

def run(cmd, timeout=20):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return out, err

cmds = [
    ("源码目录", "ls -la /home/bontor/agent-build/Agent-master/ 2>/dev/null | head -40"),
    ("config.yaml server段", "grep -A 16 '^server:' /home/bontor/agent-build/Agent-master/config.yaml 2>/dev/null"),
    ("fastapi 段", "grep -A 4 'fastapi:' /home/bontor/agent-build/Agent-master/config.yaml 2>/dev/null"),
    ("python 依赖", "python3 -c \"import fastapi, uvicorn, psutil, redis, yaml; print('依赖OK')\" 2>&1 || echo '缺依赖'"),
    ("sudo 权限", "echo 'bontor@123' | sudo -S -p '' true 2>&1 && echo SUDO_OK || echo SUDO_FAIL"),
    ("download 目录", "ls -la /var/cache/agent/download/ 2>/dev/null | head -20"),
    ("git 状态", "cd /home/bontor/agent-build/Agent-master && git log --oneline -3 2>&1 | head; echo '---'; git status --short 2>&1 | head -20"),
]

for title, cmd in cmds:
    print(f"\n===== {title} =====")
    out, err = run(cmd)
    print(out.strip() if out.strip() else "(无输出)")
    if err.strip():
        print("[STDERR]", err.strip())

client.close()
