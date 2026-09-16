from dataclasses import dataclass, field
from typing import Optional, Dict


"""
执行上下文（贯穿整个 pipeline）
替代所有 cd / 全局变量
"""


@dataclass
class ExecContext:
    # 离线安装包目录
    base_dir: str = "/opt/offline"

    # 节点主机名
    hostname: str = ""

    # 节点 hosts 映射 {"IP": "hostname"}
    hosts: Dict[str, str] = field(default_factory=dict)

    # kubeadm join 命令
    join_token: str = ""

    # 扩展环境变量
    env: Optional[Dict[str, str]] = None

    # 依赖包 URL 列表
    packages: list = field(default_factory=list)