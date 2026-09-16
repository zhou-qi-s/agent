# 心跳机制改进说明

## 改进概述

统一了 Nacos 心跳管理机制，使用 `_start_nacos_heartbeats` 替代 `start_heartbeat`，提供更完善的心跳管理功能。

## 改进内容

### 1. 统一心跳管理

**之前的问题：**
- `start_task` 使用 `start_heartbeat`（简单实现）
- `core/task.py` 有 `_start_nacos_heartbeats`（完善实现）
- 两个心跳机制重复运行

**改进后：**
- 统一使用 `_start_nacos_heartbeats`
- 移除 `start_heartbeat` 的调用
- `start_heartbeat` 标记为弃用

### 2. 功能对比

| 功能 | `start_heartbeat` (旧) | `_start_nacos_heartbeats` (新) |
|------|------------------------|--------------------------------|
| 进程监控 | ❌ 不支持 | ✅ 支持（进程退出自动停止） |
| 优雅停止 | ❌ 不支持 | ✅ 支持（stop_event机制） |
| 状态跟踪 | ❌ 不支持 | ✅ 支持（状态、错误信息） |
| 线程管理 | ❌ 无管理 | ✅ 统一管理（NACOS_HEARTBEAT_THREADS） |
| 配置读取 | ✅ 配置文件 | ✅ 配置文件 |
| 日志记录 | ✅ 基础日志 | ✅ 详细日志 |

### 3. 代码变更

#### `core/task.py`

**修改前：**
```python
from core.utils import (
    ...
    start_heartbeat,  # 导入简单版本
    ...
)

# 在 start_task 中
start_heartbeat(
    nacos_addr=nacos_addr,
    service_name=service_name,
    ip=ip,
    ports=registered,
    interval=5
)
```

**修改后：**
```python
from core.utils import (
    ...
    # 移除 start_heartbeat 导入
    ...
)

# 在 start_task 中
if registered:
    instances = []
    for port in registered:
        instance = {
            "service_name": service_name,
            "ip": ip,
            "port": port,
            "process_id": launcher.pid,
            "process_name": service_name,
            "nacos_address": nacos_addr,
            "group_name": group_name,
            "cluster_name": cluster_name,
            "namespace_id": namespace_id
        }
        instances.append(instance)
    
    started, failed_hb = _start_nacos_heartbeats(instances, heartbeat_interval)
```

#### `core/utils.py`

**修改：**
```python
def start_heartbeat(...):
    """
    [已弃用] 为多个端口启动心跳线程
    
    请使用 core.task._start_nacos_heartbeats 替代...
    """
    import warnings
    warnings.warn(
        "start_heartbeat is deprecated. Use _start_nacos_heartbeats from core.task instead.",
        DeprecationWarning,
        stacklevel=2
    )
    # ... 原有实现
```

## 新心跳机制的优势

### 1. 进程监控
```python
def _nacos_heartbeat_loop(instance_key, stop_event):
    while not stop_event.is_set():
        # 检查进程是否存在
        process_id = instance.get("process_id")
        if process_id and not psutil.pid_exists(int(process_id)):
            logging.info(f"[Nacos心跳] 进程已退出，停止心跳")
            stop_event.set()
            break
        # ... 发送心跳
```

### 2. 状态跟踪
```python
# 可以查询心跳状态
with NACOS_REGISTRY_LOCK:
    instance = NACOS_HEARTBEAT_THREADS.get(instance_key)
    if instance:
        status = instance.get("heartbeat_status")  # running / failed
        last_beat = instance.get("last_heartbeat_time")
        error = instance.get("heartbeat_error")
```

### 3. 优雅停止
```python
# 停止特定实例的心跳
_stop_heartbeat_entries({instance_key})

# 停止时会设置 stop_event，线程优雅退出
if stop_event:
    stop_event.set()
if thread and thread.is_alive():
    thread.join(timeout=2)
```

## 使用示例

### 启动任务（自动使用新机制）

```python
result = start_task({
    "install_location": "/opt/myapp",
    "script": "java -jar app.jar",
    "service_name": "my-service",
    "nacos_address": "http://192.168.1.100:8848"
})

# 返回结果包含心跳启动状态
{
    "success": True,
    "data": {
        "ports": [8080],
        "registered": [8080],
        "pid": 12345,
        "heartbeat_started": 1  # 新增：心跳启动数量
    }
}
```

### 手动启动心跳（高级用法）

```python
from core.task import _start_nacos_heartbeats

instances = [
    {
        "service_name": "my-service",
        "ip": "192.168.1.100",
        "port": 8080,
        "process_id": 12345,
        "process_name": "my-service",
        "nacos_address": "http://192.168.1.100:8848",
        "group_name": "DEFAULT_GROUP",
        "cluster_name": "DEFAULT",
        "namespace_id": "public"
    }
]

started, failed = _start_nacos_heartbeats(instances, heartbeat_interval=5)
```

### 停止心跳

```python
from core.task import _stop_heartbeat_entries, _build_nacos_instance_key

# 构建实例key
instance_key = _build_nacos_instance_key({
    "nacos_address": "http://192.168.1.100:8848",
    "namespace_id": "public",
    "group_name": "DEFAULT_GROUP",
    "cluster_name": "DEFAULT",
    "service_name": "my-service",
    "ip": "192.168.1.100",
    "port": 8080
})

# 停止心跳
_stop_heartbeat_entries({instance_key})
```

## 迁移指南

### 对于使用 `start_heartbeat` 的代码

**旧代码：**
```python
from core.utils import start_heartbeat

start_heartbeat(
    nacos_addr="http://192.168.1.100:8848",
    service_name="my-service",
    ip="192.168.1.100",
    ports=[8080],
    interval=5
)
```

**新代码：**
```python
from core.task import _start_nacos_heartbeats

instances = [{
    "service_name": "my-service",
    "ip": "192.168.1.100",
    "port": 8080,
    "process_id": pid,  # 需要传入进程ID
    "process_name": "my-service",
    "nacos_address": "http://192.168.1.100:8848",
    "group_name": "DEFAULT_GROUP",
    "cluster_name": "DEFAULT",
    "namespace_id": ""
}]

_start_nacos_heartbeats(instances, heartbeat_interval=5)
```

## 注意事项

1. **进程ID必须提供**：新机制需要 `process_id` 来监控进程状态
2. **配置一致性**：确保 `group_name`、`cluster_name`、`namespace_id` 与注册时一致
3. **线程安全**：使用 `NACOS_REGISTRY_LOCK` 保护共享数据
4. **资源清理**：程序退出时会自动清理，但建议显式调用停止函数

## 后续优化建议

1. **心跳失败重试**：添加指数退避重试机制
2. **健康检查**：结合服务健康检查决定是否发送心跳
3. **批量心跳**：多个实例合并为一个心跳请求
4. **配置热更新**：支持动态调整心跳间隔

## 相关文件

- `core/task.py` - 主要实现
- `core/utils.py` - 弃用的 `start_heartbeat`
- `config.yaml` - 配置信息