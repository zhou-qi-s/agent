# Nacos API 路径配置化修改说明

## 修改概述

将原本硬编码的 Nacos API 接口路径改为从配置文件 `config.yaml` 中读取，提高灵活性和可维护性。

## 修改文件

### 1. `core/utils.py`

#### 主要修改内容：

1. **添加配置导入**
   ```python
   from utils.config_loader import load_config
   
   # 加载配置，获取Nacos API路径
   _config = load_config()
   _nacos_cfg = _config.get("nacos", {})
   _api_cfg = _nacos_cfg.get("api", {})
   
   # Nacos API路径（从配置文件读取，使用默认值）
   NACOS_API_REGISTER = _api_cfg.get("register", "/nacos/v1/ns/instance")
   NACOS_API_DEREGISTER = _api_cfg.get("deregister", "/nacos/v1/ns/instance")
   NACOS_API_HEARTBEAT = _api_cfg.get("heartbeat", "/nacos/v1/ns/instance/beat")
   NACOS_API_INSTANCE_LIST = _api_cfg.get("instance_list", "/nacos/v1/ns/instance/list")
   NACOS_API_SERVICE_LIST = _api_cfg.get("service_list", "/nacos/v1/ns/service/list")
   NACOS_API_SERVICE_DETAIL = _api_cfg.get("service_detail", "/nacos/v1/ns/service")
   ```

2. **修改 `register_to_nacos` 函数**
   - 使用 `NACOS_API_REGISTER` 替代硬编码路径
   - 添加更多参数支持（group_name, namespace_id, cluster_name, metadata, ephemeral）
   - 添加详细的日志记录

3. **修改 `start_heartbeat` 函数**
   - 使用 `NACOS_API_HEARTBEAT` 替代硬编码路径
   - 添加更多参数支持（group_name, namespace_id, cluster_name）
   - 使用 `json.dumps` 构建 beat 信息

4. **新增辅助函数**
   - `deregister_from_nacos`: 注销实例
   - `list_nacos_instances`: 查询实例列表
   - `get_nacos_api_paths`: 获取当前 API 路径配置

## 配置文件说明

### `config.yaml` 中的 Nacos 配置

```yaml
nacos:
  ip: 192.168.31.208
  port: 8848
  
  # 服务注册参数
  namespace: public
  group_name: DEFAULT_GROUP
  cluster_name: DEFAULT
  weight: 1.0
  healthy: true
  enabled: true
  ephemeral: true
  metadata: {"version":"1.0"}
  
  # 认证
  username: null
  password: null
  
  # 常用接口（可自定义）
  api:
    register: /nacos/v1/ns/instance          # 注册实例 (POST)
    deregister: /nacos/v1/ns/instance        # 删除实例 (DELETE)
    update: /nacos/v1/ns/instance            # 更新实例 (PUT)
    heartbeat: /nacos/v1/ns/instance/beat    # 发送心跳 (PUT)
    instance_list: /nacos/v1/ns/instance/list # 查询实例列表 (GET)
    service_list: /nacos/v1/ns/service/list  # 查询服务列表 (GET)
    service_detail: /nacos/v1/ns/service     # 查询服务详情 (GET)
```

## 使用示例

### 基本使用

```python
from core.utils import register_to_nacos, start_heartbeat
from utils.config_loader import load_config

# 读取配置
config = load_config()
nacos_ip = config.get("nacos", {}).get("ip", "127.0.0.1")
nacos_port = config.get("nacos", {}).get("port", 8848)
nacos_addr = f"http://{nacos_ip}:{nacos_port}"

# 注册服务（自动使用配置文件中的 API 路径）
registered, failed = register_to_nacos(
    nacos_addr=nacos_addr,
    service_name="my-service",
    ip="192.168.1.100",
    ports=[8080, 8081],
    group_name="DEFAULT_GROUP",
    namespace_id="public",
    metadata={"version": "1.0"}
)

# 启动心跳（自动使用配置文件中的 API 路径）
start_heartbeat(
    nacos_addr=nacos_addr,
    service_name="my-service",
    ip="192.168.1.100",
    ports=registered,
    interval=5
)
```

### 查看当前 API 路径

```python
from core.utils import get_nacos_api_paths

api_paths = get_nacos_api_paths()
print(api_paths)
# 输出:
# {
#     'register': '/nacos/v1/ns/instance',
#     'deregister': '/nacos/v1/ns/instance',
#     'heartbeat': '/nacos/v1/ns/instance/beat',
#     'instance_list': '/nacos/v1/ns/instance/list',
#     'service_list': '/nacos/v1/ns/service/list',
#     'service_detail': '/nacos/v1/ns/service'
# }
```

## 向后兼容性

- 如果配置文件中没有 `nacos.api` 配置，使用默认值
- 所有函数保持原有参数兼容性
- 新增参数均为可选，有默认值

## 默认值

| API | 默认路径 |
|-----|---------|
| register | `/nacos/v1/ns/instance` |
| deregister | `/nacos/v1/ns/instance` |
| heartbeat | `/nacos/v1/ns/instance/beat` |
| instance_list | `/nacos/v1/ns/instance/list` |
| service_list | `/nacos/v1/ns/service/list` |
| service_detail | `/nacos/v1/ns/service` |

## 测试建议

1. 验证配置文件读取正常
2. 测试注册、心跳、注销功能
3. 测试自定义 API 路径
4. 验证向后兼容性

## 相关文件

- `core/utils.py` - 主要修改文件
- `config.yaml` - 配置文件
- `nacos_api_usage_example.py` - 使用示例
- `core/task.py` - 调用方（无需修改，自动生效）