# 任务协议设计文档

## 任务 JSON 结构

```json
{
  "task_id": "uuid-20260309-001",
  "type": "install",
  "priority": 1,
  "parameters": {},
  "retry": 3,
  "timeout": 300,
  "created_at": "2026-03-09T12:00:00Z"
}
```

## 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | string | 是 | 唯一任务标识符 |
| `type` | string | 是 | 任务类型：`download`/`install`/`start`/`stop`/`execute_command`/`uninstall` |
| `priority` | int | 否 | 优先级，数字越小越高 |
| `parameters` | object | 是 | 任务执行参数，不同类型参数不同 |
| `retry` | int | 否 | 重试次数 |
| `timeout` | int | 否 | 任务超时时间（秒） |
| `created_at` | string | 否 | 任务生成时间（ISO 8601格式） |

## 任务类型与参数

### 1. download
下载文件任务

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `download_url` | string | 是 | 下载地址 |
| `save_path` | string | 是 | 保存路径 |
| `md5` | string | 否 | 文件MD5校验值 |

```json
{
  "type": "download",
  "parameters": {
    "download_url": "http://example.com/app.zip",
    "save_path": "/tmp/app.zip",
    "md5": "5d41402abc4b2a76b9719d911017c592"
  }
}
```

### 2. install
安装任务

#### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file_path` | string | 是 | 安装文件路径（本地已下载的文件） |
| `install_dir` | string | 是 | 安装目录 |
| `script` | string | 否 | 安装脚本相对路径（相对于 install_dir） |

```json
{
  "type": "install",
  "parameters": {
    "file_path": "/tmp/app.zip",
    "install_dir": "/opt/my_app",
    "script": "install.sh"
  }
}
```

#### 返回格式说明

##### 安装成功时 data 字段内容

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 安装状态："installed"（已安装） |
| `file_path` | string | 安装文件路径 |
| `install_dir` | string | 安装目录 |
| `script` | string | 安装脚本名称 |
| `script_output` | string | 脚本标准输出（如果执行了脚本） |
| `script_error` | string | 脚本错误输出（如果执行了脚本） |
| `script_return_code` | int | 脚本返回码（如果执行了脚本） |
| `attempts` | int | 尝试次数 |
| `install_result` | object | 安装结果详情 |
| `install_result.success` | bool | 安装是否成功 |
| `install_result.message` | string | 安装结果消息 |
| `install_result.installed_files_count` | int | 安装的文件数量 |
| `install_result.installed_files` | array | 安装的文件列表（最多50个） |
| `install_result.script_executed` | bool | 是否执行了脚本 |
| `install_result.script_result` | object | 脚本执行结果 |

##### 安装失败时 error 字段内容

| 字段 | 类型 | 说明 |
|------|------|------|
| `error_type` | string | 错误类型 |
| `error_message` | string | 错误消息 |
| `traceback` | string | 错误堆栈信息 |

### 3. start
启动进程任务

#### 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `start_command` | string | 是 | 启动命令 |
| `work_dir` | string | 否 | 工作目录 |
| `allow_multiple_instances` | bool | 否 | 是否允许多实例，默认false |

```json
{
  "type": "start",
  "parameters": {
    "start_command": "./my_app --daemon",
    "work_dir": "/opt/my_app",
    "allow_multiple_instances": false
  }
}
```

#### 返回格式说明

##### 启动成功时 data 字段内容

| 字段 | 类型 | 说明 |
|------|------|------|
| `process` | object | 进程信息（新启动时） |
| `process.pid` | int | 进程ID |
| `process.cmdline` | array | 命令行参数列表 |
| `process.cwd` | string | 工作目录 |
| `process.status` | string | 进程状态 |
| `process.create_time` | float | 进程创建时间 |
| `processes` | array | 进程列表（已存在时） |
| `processes[].pid` | int | 进程ID |
| `processes[].cmdline` | array | 命令行参数列表 |
| `processes[].cwd` | string | 工作目录 |
| `processes[].status` | string | 进程状态 |
| `processes[].create_time` | float | 进程创建时间 |

##### 启动失败时 error 字段内容

| 字段 | 类型 | 说明 |
|------|------|------|
| `error_type` | string | 错误类型 |
| `error_message` | string | 错误消息 |
| `start_command` | string | 启动命令 |
| `attempts` | int | 尝试次数 |

### 4. stop
停止进程任务

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `process_name` | string | 是 | 进程名称 |

```json
{
  "type": "stop",
  "parameters": {
    "process_name": "my_app"
  }
}
```

### 5. execute_command
执行命令任务

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `command` | string | 是 | 要执行的命令 |
| `working_dir` | string | 否 | 工作目录 |
| `env` | object | 否 | 环境变量 |

```json
{
  "type": "execute_command",
  "parameters": {
    "command": "echo hello",
    "working_dir": "/tmp",
    "env": {
      "PATH": "/usr/bin"
    }
  }
}
```

### 6. uninstall
卸载任务

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `process_name` | string | 是 | 进程名称 |

```json
{
  "type": "uninstall",
  "parameters": {
    "process_name": "my_app"
  }
}
```

## 任务执行结果

任务执行完成后，会将结果上传到接口，所有任务类型统一使用以下格式：

### 统一返回格式

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `success` | bool | 是 | 任务是否成功 |
| `task_type` | string | 是 | 任务类型 |
| `task_id` | string | 否 | 任务ID |
| `message` | string | 是 | 成功或失败的简要说明 |
| `data` | object | 否 | 成功时返回的任务相关信息 |
| `error` | object | 否 | 失败时返回的错误信息 |
| `timestamp` | string | 是 | 时间戳（ISO 8601格式） |

### 成功时
```json
{
  "success": true,
  "task_type": "download",
  "task_id": "uuid-20260309-001",
  "message": "下载成功",
  "data": {
    "status": "downloaded",
    "download_url": "http://example.com/app.zip",
    "save_path": "/tmp/app.zip",
    "md5": "5d41402abc4b2a76b9719d911017c592",
    "file_size": 1024000,
    "attempts": 1
  },
  "timestamp": "2026-03-09T12:00:00Z"
}
```

### 失败时
```json
{
  "success": false,
  "task_type": "download",
  "task_id": "uuid-20260309-001",
  "message": "下载失败: ConnectionError: 连接超时",
  "error": {
    "error_type": "ConnectionError",
    "error_message": "连接超时",
    "traceback": "..."
  },
  "data": {
    "download_url": "http://example.com/app.zip",
    "save_path": "/tmp/app.zip",
    "attempts": 3,
    "cleanup_result": {
      "success": true,
      "error": ""
    }
  },
  "timestamp": "2026-03-09T12:00:00Z"
}
```

### 各任务类型的 extra_data 内容

| 任务类型 | 字段 | 类型 | 说明 |
|---------|------|------|------|
| **download** | `status` | string | 下载状态："downloaded"（已下载）或 "already_exists"（已存在） |
| | `download_url` | string | 下载地址 |
| | `save_path` | string | 保存路径 |
| | `md5` | string | 文件MD5校验值 |
| | `file_size` | int | 文件大小（字节） |
| | `attempts` | int | 尝试次数 |
| | `download_result` | object | 下载结果详情 |
| | `download_result.success` | bool | 下载是否成功 |
| | `download_result.message` | string | 下载结果消息 |
| | `download_result.error_details` | object | 错误详情（失败时） |
| | `download_result.cleanup_result` | object | 清理结果（临时文件） |

| **install** | `status` | string | 安装状态："installed"（已安装） |
| | `file_path` | string | 安装文件路径 |
| | `install_dir` | string | 安装目录 |
| | `script` | string | 安装脚本名称 |
| | `script_output` | string | 脚本标准输出 |
| | `script_error` | string | 脚本错误输出 |
| | `script_return_code` | int | 脚本返回码 |
| | `attempts` | int | 尝试次数 |
| | `install_result` | object | 安装结果详情 |
| | `install_result.success` | bool | 安装是否成功 |
| | `install_result.message` | string | 安装结果消息 |
| | `install_result.installed_files_count` | int | 安装的文件数量 |
| | `install_result.installed_files` | array | 安装的文件列表（最多50个） |
| | `install_result.script_executed` | bool | 是否执行了脚本 |
| | `install_result.script_result` | object | 脚本执行结果 |
| | `install_result.error_details` | object | 错误详情（失败时） |
| | `install_result.cleanup_result` | object | 清理结果（安装目录） |

| **start** | `status` | string | 启动状态："started"（已启动）或 "already_running"（已在运行） |
| | `process_name` | string | 进程名称 |
| | `pid` | int | 进程ID |
| | `start_command` | string | 启动命令 |
| | `work_dir` | string | 工作目录 |

| **stop** | `status` | string | 停止状态："stopped"（已停止）或 "already_stopped"（已停止）或 "not_running"（未运行） |
| | `process_name` | string | 进程名称 |
| | `pid` | int | 进程ID |

| **execute_command** | `command` | string | 执行的命令 |
| | `return_code` | int | 命令返回码 |
| | `output` | string | 命令标准输出 |
| | `error` | string | 命令错误输出 |

| **uninstall** | `status` | string | 卸载状态："uninstalled"（已卸载） |
| | `process_name` | string | 进程名称 |
| | `stop_result` | object | 停止进程的结果 |
| | `deleted_dirs` | array | 删除的目录列表 |

### 示例

#### download 任务 - 成功

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 下载状态："downloaded"（已下载）或 "already_exists"（已存在） |
| `download_url` | string | 下载地址 |
| `save_path` | string | 保存路径 |
| `md5` | string | 文件MD5校验值 |
| `file_size` | int | 文件大小（字节） |
| `attempts` | int | 尝试次数 |

```json
{
  "status": "downloaded",
  "download_url": "http://example.com/app.zip",
  "save_path": "/tmp/app.zip",
  "md5": "5d41402abc4b2a76b9719d911017c592",
  "file_size": 1024000,
  "attempts": 1
}
```

#### download 任务 - 失败

| 字段 | 类型 | 说明 |
|------|------|------|
| `error` | string | 错误消息 |
| `error_type` | string | 错误类型 |
| `download_url` | string | 下载地址 |
| `save_path` | string | 保存路径 |
| `download_result` | object | 下载结果详情 |
| `download_result.success` | bool | 下载是否成功 |
| `download_result.message` | string | 下载结果消息 |
| `download_result.error_details` | object | 错误详情 |
| `download_result.cleanup_result` | object | 清理结果 |

```json
{
  "error": "ConnectionError: 连接超时",
  "error_type": "ConnectionError",
  "download_url": "http://example.com/app.zip",
  "save_path": "/tmp/app.zip",
  "download_result": {
    "success": false,
    "message": "下载失败: ConnectionError: 连接超时",
    "error_details": {
      "error_type": "ConnectionError",
      "error_message": "连接超时",
      "attempt": 3,
      "total_attempts": 3
    },
    "cleanup_result": {
      "success": true,
      "error": ""
    }
  }
}
```

#### install 任务 - 成功

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 安装状态："installed"（已安装） |
| `file_path` | string | 安装文件路径 |
| `install_dir` | string | 安装目录 |
| `script` | string | 安装脚本名称 |
| `attempts` | int | 尝试次数 |
| `install_result` | object | 安装结果详情 |
| `install_result.success` | bool | 安装是否成功 |
| `install_result.message` | string | 安装结果消息 |
| `install_result.installed_files_count` | int | 安装的文件数量 |
| `install_result.installed_files` | array | 安装的文件列表 |
| `install_result.script_executed` | bool | 是否执行了脚本 |
| `install_result.script_result` | object | 脚本执行结果 |

```json
{
  "status": "installed",
  "file_path": "/tmp/app.zip",
  "install_dir": "/opt/my_app",
  "script": "install.sh",
  "attempts": 1,
  "install_result": {
    "success": true,
    "message": "安装完成",
    "installed_files_count": 50,
    "installed_files": [
      {"path": "/opt/my_app/bin/my_app", "size": 1024000, "type": "file"},
      {"path": "/opt/my_app/config", "type": "directory"}
    ],
    "script_executed": true,
    "script_result": {
      "return_code": 0,
      "output_length": 100,
      "error_length": 0
    }
  }
}
```

#### install 任务 - 失败

| 字段 | 类型 | 说明 |
|------|------|------|
| `error` | string | 错误消息 |
| `error_type` | string | 错误类型 |
| `file_path` | string | 安装文件路径 |
| `install_dir` | string | 安装目录 |
| `script` | string | 安装脚本名称 |
| `install_result` | object | 安装结果详情 |
| `install_result.success` | bool | 安装是否成功 |
| `install_result.message` | string | 安装结果消息 |
| `install_result.error_details` | object | 错误详情 |
| `install_result.cleanup_result` | object | 清理结果 |

```json
{
  "error": "FileNotFoundError: 安装脚本不存在",
  "error_type": "FileNotFoundError",
  "file_path": "/tmp/app.zip",
  "install_dir": "/opt/my_app",
  "script": "install.sh",
  "install_result": {
    "success": false,
    "message": "安装失败: FileNotFoundError: 安装脚本不存在",
    "error_details": {
      "error_type": "FileNotFoundError",
      "error_message": "安装脚本不存在",
      "traceback": "..."
    },
    "cleanup_result": {
      "success": true,
      "error": ""
    }
  }
}
```

#### execute_command 任务
```json
{
  "command": "echo hello",
  "return_code": 0,
  "output": "hello\n",
  "error": ""
}
```

## 设计原则

1. **参数按需填写** - `parameters` 中只填写当前任务类型需要的字段，不相关的字段不传递
2. **必填参数校验** - 任务处理前应根据 `type` 校验必填参数是否存在
3. **可选字段容错** - `priority`、`retry`、`timeout`、`created_at` 等字段缺失时应使用默认值
4. **结果自动上报** - 任务执行完成后自动将结果上传到指定接口
