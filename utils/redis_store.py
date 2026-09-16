import json
import time

from utils.redis_client import get_redis


class RedisStore:
    """
    Redis 统一读写封装类
    所有 Redis 操作统一从这里走
    """

    def __init__(self):
        # 获取全局 Redis 客户端（连接池单例）
        self.redis = get_redis()

    # ================== 基础KV ==================

    def set(self, key, value, ttl=None):
        """
        写入普通键值
        :param key: 键
        :param value: 值（支持 dict/list/str/int）
        :param ttl: 过期时间（秒）
        """
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)

        if ttl:
            self.redis.setex(key, ttl, value)
        else:
            self.redis.set(key, value)

    def get(self, key, default=None):
        """
        读取键值
        """
        value = self.redis.get(key)
        if value is None:
            return default

        # 尝试反序列化 JSON
        try:
            return json.loads(value)
        except:
            return value

    def delete(self, key):
        """
        删除键
        """
        return self.redis.delete(key)

    def exists(self, key):
        """
        判断 key 是否存在
        """
        return self.redis.exists(key)

    # ================== Hash ==================

    def h_set(self, name, key, value):
        """
        hash 写入
        """
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        return self.redis.hset(name, key, value)

    def hget(self, name, key, default=None):
        """
        hash 读取
        """
        value = self.redis.hget(name, key)
        if value is None:
            return default
        try:
            return json.loads(value)
        except:
            return value

    def h_get_all(self, name):
        """
        获取整个 hash
        """
        data = self.redis.hgetall(name)
        result = {}
        for k, v in data.items():
            try:
                result[k] = json.loads(v)
            except:
                result[k] = v
        return result

    # ================== List ==================

    def l_push(self, key, value):
        """
        左入队列
        """
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        return self.redis.lpush(key, value)

    def r_pop(self, key):
        """
        右出队列
        """
        value = self.redis.rpop(key)
        if value is None:
            return None
        try:
            return json.loads(value)
        except:
            return value

    # ================== Set ==================

    def s_add(self, key, value):
        """
        set 添加元素
        """
        return self.redis.sadd(key, value)

    def s_members(self, key):
        """
        set 获取所有元素
        """
        return self.redis.smembers(key)

    # ================== 工程级功能 ==================

    def heartbeat(self, agent_id, ttl=60):
        """
        心跳上报
        """
        key = f"agent:heartbeat:{agent_id}"
        self.set(key, int(time.time()), ttl=ttl)

    def save_metrics(self, agent_id, metrics: dict, ttl=60):
        """
        保存资源监控信息
        """
        key = f"agent:metrics:{agent_id}"
        self.set(key, metrics, ttl=ttl)

    def save_process_list(self, agent_id, process_list: list, ttl=120):
        """
        保存进程列表
        """
        key = f"agent:process:{agent_id}"
        self.set(key, process_list, ttl=ttl)

    def push_command(self, agent_id, command: dict):
        """
        下发命令
        """
        key = f"agent:command:{agent_id}"
        self.lpush(key, command)

    def pop_command(self, agent_id):
        """
        获取命令
        """
        key = f"agent:command:{agent_id}"
        return self.rpop(key)

    def push_limited_list(self, key, value, max_len=15):
        current_list = self.redis.lrange(key, 0, -1)
        # 反序列化
        deserialized_list = []
        for v in current_list:
            try:
                deserialized_list.append(json.loads(v))
            except:
                deserialized_list.append(v)

        # 如果长度达到最大，删除时间戳最小的元素
        if len(deserialized_list) >= max_len:
            # 假设每个元素都是 dict，包含 'timestamp' 键
            deserialized_list.sort(key=lambda x: x.get('timestamp', 0))
            deserialized_list.pop(0)  # 删除最小时间戳的元素

        # 插入新值
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value, ensure_ascii=False)
        else:
            value_str = value

        self.redis.rpush(key, value_str)

    # ================== Stream ==================

    def stream_add(self, stream_name, data: dict, max_len=None):
        """
        添加消息到 Stream
        :param stream_name: 流名称
        :param data: dict 数据
        :param max_len: 最大长度（自动裁剪）
        """
        if not isinstance(data, dict):
            raise ValueError("Stream data must be dict")

        # 所有值转字符串（Redis Stream 只支持字符串）
        data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                for k, v in data.items()}

        return self.redis.xadd(stream_name, data, maxlen=max_len)


    def stream_create_group(self, stream_name, group_name):
        """
        创建消费组（如果不存在）
        """
        try:
            self.redis.xgroup_create(
                name=stream_name,
                groupname=group_name,
                id="0",
                mkstream=True
            )
        except Exception as e:
            # 已存在会报错，忽略
            if "BUSYGROUP" not in str(e):
                raise


    def stream_read_group(self, stream_name, group_name,
                          consumer_name,
                          count=1, block=5000):
        """
        消费组读取消息
        """
        result = self.redis.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={stream_name: ">"},
            count=count,
            block=block
        )

        messages = []

        if result:
            for stream, msgs in result:
                for msg_id, msg_data in msgs:
                    parsed = {}
                    for k, v in msg_data.items():
                        try:
                            parsed[k] = json.loads(v)
                        except:
                            parsed[k] = v

                    messages.append({
                        "id": msg_id,
                        "data": parsed
                    })

        return messages


    def stream_ack(self, stream_name, group_name, msg_id):
        """
        确认消费
        """
        return self.redis.xack(stream_name, group_name, msg_id)


    def stream_pending(self, stream_name, group_name):
        """
        查看未确认消息
        """
        return self.redis.xpending(stream_name, group_name)


redisUtils = RedisStore()

