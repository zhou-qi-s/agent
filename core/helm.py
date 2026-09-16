"""
Helm release 列表同步模块

查询集群中所有已部署的 Helm release，
同步到 Redis(ZSET + HASH)。

Redis结构:

helm:release:index
    ZSET
    member:
        cluster:namespace:name
    score:
        更新时间

helm:release:data:{release_id}
    HASH
    保存release详情
"""

import json
import logging
import os
import subprocess
import time
from typing import Any, Dict, List

import utils.util
from utils.config_loader import load_config
from utils.redis_store import RedisStore

_LOG = "helm_list"


def _run_command(cmd: list, timeout: int = 60) -> tuple:
    """
    执行命令
    返回:
        success, stdout, stderr
    """

    logging.info(
        "[%s] 执行: %s",
        _LOG,
        " ".join(cmd)
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        return (
            result.returncode == 0,
            result.stdout.strip(),
            result.stderr.strip()
        )

    except subprocess.TimeoutExpired:
        logging.error(
            "[%s] 命令超时",
            _LOG
        )
        return False, "", "timeout"

    except Exception as e:
        logging.error(
            "[%s] 命令异常:%s",
            _LOG,
            e
        )
        return False, "", str(e)


def _parse_time_score(time_str: str) -> int:
    """
    简单生成排序时间

    Helm时间格式复杂，
    这里失败时使用当前时间
    """

    try:
        import datetime

        dt = datetime.datetime.fromisoformat(
            time_str.replace("Z", "+00:00")
        )

        return int(dt.timestamp())

    except Exception:
        return int(time.time())


def init_kubeconfig() -> bool:
    """
    设置 KUBECONFIG 环境变量。

    从 config.yaml 的 helm.config 读取 kubeconfig 文件路径，
    写入当前进程的 KUBECONFIG 环境变量。

    返回:
        True 成功，False 失败
    """
    config = load_config()
    helm_cfg = config.get("helm", {}) or {}
    kubeconfig_path = (helm_cfg.get("config", "") or "").strip()

    if not kubeconfig_path:
        logging.warning("[%s] helm.config 未配置，跳过 KUBECONFIG 设置", _LOG)
        return False

    if not os.path.exists(kubeconfig_path):
        logging.warning("[%s] kubeconfig 文件不存在: %s", _LOG, kubeconfig_path)
        return False

    os.environ["KUBECONFIG"] = kubeconfig_path
    logging.info("[%s] KUBECONFIG 已设置为: %s", _LOG, kubeconfig_path)
    return True


def sync_helm_list() -> List[Dict[str, Any]]:
    config = load_config()

    # 设置 KUBECONFIG 环境变量（helm 依赖）
    init_kubeconfig()

    helm_cfg = config.get("helm", {}) or {}

    index_key = (
            helm_cfg.get("index_key")
            or "helm:release:index"
    )

    data_prefix = (
            helm_cfg.get("data_prefix")
            or "helm:release:data:"
    )

    # =============================
    # 1. 查询 Helm
    # =============================

    success, stdout, stderr = _run_command(
        [
            "helm",
            "list",
            "-A",
            "--all",
            "-o",
            "json"
        ]
    )

    # helm失败不能清缓存
    if not success:
        logging.error(
            "[%s] helm查询失败:%s",
            _LOG,
            stderr
        )

        return []

    try:

        raw_list = json.loads(stdout)

    except Exception as e:

        logging.error(
            "[%s] JSON解析失败:%s",
            _LOG,
            e
        )

        return []

    redis_store = RedisStore()

    agent_ip = utils.util.get_ip() or "unknown"

    cluster_id = agent_ip

    # 当前helm真实存在的release
    current_ids = set()

    result_list = []

    # =============================
    # 2. 同步新增/更新
    # =============================

    for release in raw_list:
        name = release.get(
            "name",
            ""
        )

        namespace = release.get(
            "namespace",
            ""
        )

        release_id = (
            f"{cluster_id}:"
            f"{namespace}:"
            f"{name}"
        )

        current_ids.add(release_id)

        data = {

            "id": release_id,

            "name": name,

            "namespace": namespace,

            "revision": str(
                release.get(
                    "revision",
                    ""
                )
            ),

            "updated": release.get(
                "updated",
                ""
            ),

            "status": release.get(
                "status",
                ""
            ),

            "chart": release.get(
                "chart",
                ""
            ),

            "version": release.get(
                "app_version",
                ""
            ),

            "ip": agent_ip,

            "sync_time": int(
                time.time()
            )

        }

        redis_key = (
                data_prefix
                +
                release_id
        )

        # 保存详情（逐字段写入，兼容旧版 redis-py）
        for field_name, field_value in data.items():
            redis_store.h_set(redis_key, field_name, field_value)

        # 加入分页索引
        redis_store.redis.zadd(
            index_key,
            {
                release_id:
                    _parse_time_score(
                        data["updated"]
                    )
            }
        )

        result_list.append(data)

        logging.info(
            "[%s] 同步:%s",
            _LOG,
            release_id
        )

    # =============================
    # 3. 删除检测
    # =============================

    cached_ids = set(
        redis_store.redis.zrange(
            index_key,
            0,
            -1
        )
    )

    # redis返回bytes
    cached_ids = {
        x.decode()
        if isinstance(x, bytes)
        else x
        for x in cached_ids
    }

    deleted_ids = (
            cached_ids
            -
            current_ids
    )

    for delete_id in deleted_ids:
        redis_store.redis.zrem(
            index_key,
            delete_id
        )

        redis_store.redis.delete(
            data_prefix + delete_id
        )

        logging.info(
            "[%s] 删除缓存:%s",
            _LOG,
            delete_id
        )

    logging.info(
        "[%s] 同步完成 当前:%d 删除:%d",
        _LOG,
        len(current_ids),
        len(deleted_ids)
    )

    return result_list
