"""
Kubernetes Pod 资源告警模块

功能：
1. 遍历 {download}/kubernetes/ 目录，发现所有告警阈值配置
2. 通过 kubectl get pods -n {ns} -l {labels} 获取 Pod 列表
3. 通过 kubectl top pod <pod-name> 获取资源使用情况
4. 与 {labels}.txt 中的阈值对比，超出则上报告警

目录结构：
{download_base}/
  kubernetes/
    {namespace}/
      {labels}.txt    ← 告警阈值 JSON {"cpu": 500, "memory": 1024, "io": 100}

阈值单位说明：
  cpu    -> millicores（1000 = 1 核）
  memory -> MiB
  io     -> 预留（kubectl top 不支持 IO，暂不比较）
"""

import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

from utils.config_loader import load_config
from utils.logger import logger

# ---- 复用 alarm_common.py 的上报能力 ----
from core.alarm.alarm_common import (
    ALARM_COOLDOWN,
    ALARM_TYPE_CPU,
    ALARM_TYPE_IO,
    ALARM_TYPE_MEMORY,
    last_alarm_time,
    report_alarm,
)


# =========================================================
# 全局配置
# =========================================================

_CONFIG = load_config()
_DOWNLOAD_BASE = _CONFIG.get("server", {}).get("download", "")
_KUBECTL_BIN = _CONFIG.get("k8s", {}).get("kubectl", "kubectl")

_LOG = "[alarm-k8s]"


# =========================================================
# kubectl 命令封装
# =========================================================

def _run_kubectl(args: List[str], timeout: int = 15) -> Tuple[bool, str]:
    """执行 kubectl 命令，返回 (success, stdout_or_stderr)。"""
    cmd = [_KUBECTL_BIN] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            logger.warning("%s kubectl 失败: %s -> %s", _LOG, " ".join(cmd), stderr)
            return False, stderr
        return True, result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("%s kubectl 超时: %s", _LOG, " ".join(cmd))
        return False, "命令超时"
    except FileNotFoundError:
        logger.error("%s kubectl 未找到: %s", _LOG, _KUBECTL_BIN)
        return False, f"kubectl 未找到: {_KUBECTL_BIN}"
    except Exception as e:
        logger.warning("%s kubectl 异常: %s -> %s", _LOG, " ".join(cmd), e)
        return False, str(e)


# =========================================================
# Pod 发现
# =========================================================

def get_pods_by_labels(namespace: str, labels: str) -> List[str]:
    """
    通过 kubectl 获取符合条件的 Pod 名称列表。

    等价命令: kubectl get pods -n {ns} -l {labels} --no-headers
    """
    success, output = _run_kubectl([
        "get", "pods",
        "-n", namespace,
        "-l", labels,
        "--field-selector=status.phase=Running",
        "--no-headers",
        "-o", "custom-columns=NAME:.metadata.name",
    ])
    if not success:
        return []
    pods = [line.strip() for line in output.split("\n") if line.strip()]
    logger.info("%s 发现 %d 个 Pod: ns=%s, labels=%s", _LOG, len(pods), namespace, labels)
    return pods


# =========================================================
# kubectl top 解析
# =========================================================

# CPU: "10m" -> 10, "1.5" -> 1500, "0.5" -> 500
_CPU_RE = re.compile(r"^(\d+\.?\d*)\s*m?$", re.IGNORECASE)

# Memory: "256Mi" / "1024Ki" / "1Gi" / "500"
_MEM_RE = re.compile(r"^(\d+\.?\d*)\s*(Ki|Mi|Gi|Ti|K|M|G|T)?$", re.IGNORECASE)
_MEM_TO_MIB: Dict[str, float] = {
    "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1048576,
    "K":  1 / 1024, "M":  1, "G":  1024, "T":  1048576,
}


def _parse_cpu(value: str) -> float:
    """将 kubectl top 的 CPU 值转为 millicores（float）。"""
    match = _CPU_RE.match(value.strip())
    if not match:
        return 0.0
    num = float(match.group(1))
    return num if value.strip().lower().endswith("m") else num * 1000.0


def _parse_memory(value: str) -> float:
    """将 kubectl top 的内存值转为 MiB（float）。"""
    match = _MEM_RE.match(value.strip())
    if not match:
        return 0.0
    num = float(match.group(1))
    unit = (match.group(2) or "").strip()
    return num * _MEM_TO_MIB.get(unit, 1.0)


def get_pod_top(pod_name: str, namespace: str) -> Optional[Dict[str, float]]:
    """
    获取单个 Pod 的资源使用情况。

    等价命令: kubectl top pod {pod_name} -n {ns} --no-headers

    Returns:
        {"cpu_millicores": 150.0, "memory_mb": 512.0}  或  None
    """
    success, output = _run_kubectl([
        "top", "pod", pod_name,
        "-n", namespace,
        "--no-headers",
    ], timeout=10)
    if not success:
        return None

    # 输出格式: "pod-name  10m  256Mi"
    parts = output.split()
    if len(parts) < 3:
        logger.warning("%s kubectl top 输出格式异常: %s", _LOG, output)
        return None

    return {
        "cpu_millicores": _parse_cpu(parts[1]),
        "memory_mb": _parse_memory(parts[2]),
    }


def get_pod_cpu_limit(pod_name: str, namespace: str) -> float:
    """
    获取 Pod 所有容器的 CPU limit 总和（millicores）。

    优先取 limits.cpu，如果没有则取 requests.cpu。
    返回值 0 表示没有设置任何 CPU 限制。
    """
    for resource_type in ("limits", "requests"):
        success, output = _run_kubectl([
            "get", "pod", pod_name,
            "-n", namespace,
            "-o", f"jsonpath={{.spec.containers[*].resources.{resource_type}.cpu}}",
        ], timeout=5)
        if not success or not output.strip():
            continue
        # 输出可能是 "500m 1" 或 "1.5" 等
        total = 0.0
        for val in output.split():
            total += _parse_cpu(val)
        if total > 0:
            return total
    return 0.0


# =========================================================
# 告警阈值读取
# =========================================================

def read_k8s_alarm_threshold(file_path: str) -> Dict[str, Any]:
    """读取 k8s 告警阈值 JSON 文件。"""
    if not os.path.isfile(file_path):
        return {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("%s 读取阈值文件失败: %s -> %s", _LOG, file_path, e)
        return {}


def discover_k8s_alarms() -> List[Dict[str, Any]]:
    """
    遍历 {download}/kubernetes/ 目录，发现所有配置了告警阈值的 namespace/labels。
    """
    base_dir = os.path.join(_DOWNLOAD_BASE, "kubernetes")
    if not os.path.isdir(base_dir):
        logger.debug("%s kubernetes 目录不存在: %s", _LOG, base_dir)
        return []

    results: List[Dict[str, Any]] = []
    for namespace in sorted(os.listdir(base_dir)):
        ns_dir = os.path.join(base_dir, namespace)
        if not os.path.isdir(ns_dir):
            continue

        for filename in sorted(os.listdir(ns_dir)):
            if not filename.endswith(".txt"):
                continue

            file_path = os.path.join(ns_dir, filename)

            thresholds = read_k8s_alarm_threshold(file_path)
            if not thresholds:
                continue

            # labels 必须从 JSON 取（文件名已被 sanitize 替换 / 为 _，不可直接用于 kubectl）
            labels = thresholds.get("labels")
            if not labels:
                logger.warning("%s 跳过: 文件中缺少 labels 字段，请重新调用 /api/alarm/k8s/trigger 写入: %s", _LOG, file_path)
                continue

            cpu_threshold = thresholds.get("cpu")
            memory_threshold = thresholds.get("memory")
            io_threshold = thresholds.get("io")

            if cpu_threshold is None or memory_threshold is None or io_threshold is None:
                logger.debug("%s 阈值不完整，跳过: %s", _LOG, file_path)
                continue

            try:
                cpu_threshold = float(cpu_threshold)
                memory_threshold = float(memory_threshold)
                io_threshold = float(io_threshold)
            except (TypeError, ValueError) as e:
                logger.warning("%s 阈值格式错误: %s -> %s", _LOG, file_path, e)
                continue

            results.append({
                "namespace": namespace,
                "labels": labels,
                "file_path": file_path,
                "cpu_threshold": cpu_threshold,
                "memory_threshold": memory_threshold,
                "io_threshold": io_threshold,
            })

    return results


# =========================================================
# 告警冷却（Pod 粒度）
# =========================================================

def _pod_alarm_key(pod_name: str, namespace: str, alarm_type: int) -> str:
    return f"k8s_{namespace}_{pod_name}_{alarm_type}"


def can_report_pod_alarm(pod_name: str, namespace: str, alarm_type: int) -> bool:
    """Pod 粒度告警冷却检查（复用 alarm.py 的冷却缓存和 ALARM_COOLDOWN）。"""
    now = time.time()
    key = _pod_alarm_key(pod_name, namespace, alarm_type)
    last = last_alarm_time.get(key)

    if last is None:
        last_alarm_time[key] = now
        return True

    if now - last > ALARM_COOLDOWN:
        last_alarm_time[key] = now
        return True

    return False


# =========================================================
# 主逻辑：检查 + 上报
# =========================================================

def check_and_report_k8s_alarms() -> List[Dict[str, Any]]:
    """
    检查所有 k8s 告警配置并上报。

    流程:
        1. 遍历 {download}/kubernetes/ 读取阈值配置
        2. kubectl get pods 获取 Pod 列表
        3. kubectl top pod 获取资源使用
        4. 对比阈值，超出则调 report_alarm 上报

    Returns:
        [{type, pod, namespace, value, threshold}, ...]
    """
    configs = discover_k8s_alarms()
    if not configs:
        return []

    reported: List[Dict[str, Any]] = []

    for cfg in configs:
        namespace = cfg["namespace"]
        labels = cfg["labels"]
        cpu_threshold = cfg["cpu_threshold"]
        memory_threshold = cfg["memory_threshold"]
        io_threshold = cfg["io_threshold"]  # noqa: F841  # 预留，kubectl top 无 IO

        # 1) 获取 Pod 列表
        pods = get_pods_by_labels(namespace, labels)
        if not pods:
            logger.info("%s 未发现 Pod: ns=%s, labels=%s", _LOG, namespace, labels)
            continue

        # 2) 逐 Pod 检查资源
        for pod_name in pods:
            top_data = get_pod_top(pod_name, namespace)
            if top_data is None:
                logger.warning("%s 获取 Pod 资源失败: %s/%s", _LOG, namespace, pod_name)
                continue

            cpu_val = top_data["cpu_millicores"]
            mem_val = top_data["memory_mb"]
            alarm_label = f"{namespace}/{labels}/{pod_name}"

            # ---- CPU 告警（百分比 = 使用量 / limit * 100） ----
            if cpu_threshold >= 0 and cpu_val > 0:
                cpu_limit = get_pod_cpu_limit(pod_name, namespace)
                if cpu_limit > 0:
                    cpu_percent = cpu_val / cpu_limit * 100.0
                else:
                    cpu_percent = 0.0
                    logger.debug("%s Pod %s/%s 未设置 CPU limit/request，跳过百分比计算", _LOG, namespace, pod_name)

                if cpu_percent > cpu_threshold:
                    if can_report_pod_alarm(pod_name, namespace, ALARM_TYPE_CPU):
                        report_alarm(
                            alarm_type=ALARM_TYPE_CPU,
                            service_name=alarm_label,
                            alarm_name=f"{namespace}/{pod_name} CPU告警",
                            content=(
                                f"容器 {namespace}/{pod_name} CPU 使用 {cpu_percent:.1f}%，"
                                f"超过阈值 {cpu_threshold:.1f}%"
                            ),
                        )
                        reported.append({
                            "type": "cpu",
                            "pod": pod_name,
                            "namespace": namespace,
                            "value": round(cpu_percent, 1),
                            "threshold": cpu_threshold,
                        })

            # ---- 内存告警 ----
            if memory_threshold >= 0 and mem_val > memory_threshold:
                if can_report_pod_alarm(pod_name, namespace, ALARM_TYPE_MEMORY):
                    report_alarm(
                        alarm_type=ALARM_TYPE_MEMORY,
                        service_name=alarm_label,
                        alarm_name=f"{namespace}/{pod_name} 内存告警",
                        content=(
                            f"容器 {namespace}/{pod_name} 内存使用 {mem_val:.1f}MB，"
                            f"超过阈值 {memory_threshold:.1f}MB"
                        ),
                    )
                    reported.append({
                        "type": "memory",
                        "pod": pod_name,
                        "namespace": namespace,
                        "value": mem_val,
                        "threshold": memory_threshold,
                    })

    return reported


# =========================================================
# 自测入口
# =========================================================

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

    print("=" * 60)
    print("  K8s 告警检测")
    print("=" * 60)

    result = check_and_report_k8s_alarms()
    if result:
        print(f"\n触发 {len(result)} 条告警:")
        for r in result:
            print(f"  type={r['type']}, pod={r['pod']}, ns={r['namespace']}, "
                  f"value={r['value']}, threshold={r['threshold']}")
    else:
        print("\n无告警触发")
    print("\n完成")
