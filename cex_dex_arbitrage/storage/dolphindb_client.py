"""
DolphinDB 客户端封装。

这一层只负责：
1. 管理 DolphinDB Session 的创建与关闭
2. 提供统一的 `run` 调用入口
3. 屏蔽上层对具体连接细节的感知

注意：
这里先实现一个轻量封装，方便后续 writer 直接复用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
import logging

try:
    import dolphindb as ddb
except ImportError:  # pragma: no cover
    ddb = None


logger = logging.getLogger(__name__)


@dataclass
class DolphinDBConfig:
    """
    DolphinDB 连接配置。

    字段说明：
    - `host`: 数据库服务地址
    - `port`: 数据库端口
    - `user`: 登录用户名
    - `password`: 登录密码
    - `db_path`: DolphinDB 数据库路径
    - `table_name`: 订单簿表名
    - `partition_start`: 分区起始日期
    - `partition_end`: 分区结束日期
    """

    host: str = "localhost"
    port: int = 8848
    user: str = ""
    password: str = ""
    db_path: str = "dfs://default_db"
    table_name: str = "default_table"
    partition_start: str = "2026.01.01"
    partition_end: str = "2027.12.31"


class DolphinDBClient:
    """
    DolphinDB Session 轻量封装。

    这个类的职责很单一：
    - 连接
    - 执行脚本
    - 关闭连接

    上层 writer 不需要直接接触 `ddb.Session()`，只依赖这个类即可。
    """

    def __init__(self, config: Optional[DolphinDBConfig] = None):
        self.config = config or DolphinDBConfig()
        self.session = None

    @property
    def connected(self) -> bool:
        """返回当前是否已经建立会话。"""
        return self.session is not None

    def connect(self) -> None:
        """
        建立 DolphinDB 连接。

        如果本地环境没有安装 `dolphindb` Python 包，会直接抛出异常，
        这样调用方能尽快发现依赖问题。
        """
        if self.session is not None:
            return

        if ddb is None:
            raise ImportError("未安装 dolphindb Python 包，无法创建 DolphinDB 连接。")

        session = ddb.Session()
        session.connect(
            host=self.config.host,
            port=self.config.port,
            userid=self.config.user,
            password=self.config.password,
        )
        self.session = session
        logger.info(
            "DolphinDB 连接成功: host=%s port=%s db_path=%s table=%s",
            self.config.host,
            self.config.port,
            self.config.db_path,
            self.config.table_name,
        )

    def ensure_connected(self) -> None:
        """保证当前连接可用；如未连接则自动连接。"""
        if self.session is None:
            self.connect()

    def run(self, script: str) -> Any:
        """
        执行 DolphinDB 脚本并返回结果。

        参数：
        - `script`: 要执行的 DolphinDB 脚本文本
        """
        self.ensure_connected()
        return self.session.run(script)

    def close(self) -> None:
        """关闭当前会话。"""
        if self.session is None:
            return

        try:
            self.session.close()
        finally:
            self.session = None
            logger.info("DolphinDB 连接已关闭")
