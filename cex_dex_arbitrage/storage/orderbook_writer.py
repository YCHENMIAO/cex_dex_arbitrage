"""
订单簿写入器。

这一层负责把统一的 `OrderBookSnapshot` 批量转换为待落库行，
并通过独立线程异步写入 DolphinDB。

当前阶段先完成：
1. 接收 `OrderBookSnapshot`
2. 放入内存队列
3. 独立线程按批次取出
4. 调用 DolphinDB 客户端执行 append 脚本

这样做的目的是避免在 WebSocket 回调线程里直接写库，
降低网络抖动或数据库阻塞对实时交易逻辑的影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Dict, List, Optional
from datetime import date, datetime, timezone
import logging
import time

from models import DEFAULT_BOOK_DEPTH, OrderBookSnapshot
from storage.dolphindb_client import DolphinDBClient, DolphinDBConfig
from storage.dolphindb_schema import (
    get_create_orderbook_table_script,
    get_orderbook_column_types,
    get_orderbook_columns,
)


logger = logging.getLogger(__name__)


@dataclass
class WriterStats:
    """记录写入器运行期间的一些基础统计信息。"""

    enqueued_count: int = 0
    flushed_batches: int = 0
    flushed_rows: int = 0
    failed_batches: int = 0


class DolphinDBWriter:
    """
    DolphinDB 异步订单簿写入器。

    使用方式：
    1. 初始化 writer
    2. 调用 `start()` 启动后台线程
    3. 在行情回调中调用 `enqueue(snapshot)`
    4. 程序退出时调用 `stop()`
    """

    def __init__(
        self,
        client: Optional[DolphinDBClient] = None,
        config: Optional[DolphinDBConfig] = None,
        table_depth: int = DEFAULT_BOOK_DEPTH,
        batch_size: int = 100,
        flush_interval_seconds: float = 0.5,
        queue_maxsize: int = 50000,
        auto_create_table: bool = True,
    ):
        self.config = config or DolphinDBConfig()
        self.client = client or DolphinDBClient(self.config)
        self.table_depth = table_depth
        self.batch_size = batch_size
        self.flush_interval_seconds = flush_interval_seconds
        self.auto_create_table = auto_create_table

        self.queue: Queue[OrderBookSnapshot] = Queue(maxsize=queue_maxsize)
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        self._stats_lock = Lock()
        self.stats = WriterStats()
        self.column_names = get_orderbook_columns(depth=self.table_depth)
        self.column_types = get_orderbook_column_types(depth=self.table_depth)

    def start(self) -> None:
        """
        启动后台写入线程。

        首次启动时会尝试连接 DolphinDB，并在需要时自动建表。
        """
        if self._thread and self._thread.is_alive():
            return

        self.client.ensure_connected()
        if self.auto_create_table:
            self._ensure_table()

        self._stop_event.clear()
        self._thread = Thread(target=self._run_loop, name="DolphinDBWriter", daemon=True)
        self._thread.start()
        logger.info(
            "DolphinDBWriter 已启动: table=%s batch_size=%s flush_interval=%.3fs",
            self.config.table_name,
            self.batch_size,
            self.flush_interval_seconds,
        )

    def stop(self, flush_remaining: bool = True, join_timeout: float = 3.0) -> None:
        """
        停止后台线程。

        参数：
        - `flush_remaining`: 停止前是否尽量刷出剩余队列数据
        - `join_timeout`: 等待线程退出的秒数
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=join_timeout)

        if flush_remaining:
            self.flush()

        self.client.close()

    def enqueue(self, snapshot: OrderBookSnapshot, block: bool = False, timeout: Optional[float] = None) -> None:
        """
        将一个标准化订单簿快照放入待写入队列。

        这里不直接做数据库写入，只负责把数据交给后台线程。
        """
        self.queue.put(snapshot, block=block, timeout=timeout)
        with self._stats_lock:
            self.stats.enqueued_count += 1

    def flush(self) -> int:
        """
        立即尝试刷出当前队列中的全部数据。

        返回：
        - 本次实际刷出的行数
        """
        snapshots: List[OrderBookSnapshot] = []

        while True:
            try:
                snapshots.append(self.queue.get_nowait())
            except Empty:
                break

        if not snapshots:
            return 0

        self._flush_batch(snapshots)
        return len(snapshots)

    def get_stats(self) -> WriterStats:
        """返回当前统计信息副本。"""
        with self._stats_lock:
            return WriterStats(
                enqueued_count=self.stats.enqueued_count,
                flushed_batches=self.stats.flushed_batches,
                flushed_rows=self.stats.flushed_rows,
                failed_batches=self.stats.failed_batches,
            )

    def _ensure_table(self) -> None:
        """确保订单簿表存在。"""
        script = get_create_orderbook_table_script(
            db_path=self.config.db_path,
            table_name=self.config.table_name,
            depth=self.table_depth,
        )
        self.client.run(script)

    def _run_loop(self) -> None:
        """
        后台循环：
        - 每次尽量收集一批 snapshot
        - 满批或超时后执行一次写库
        """
        batch: List[OrderBookSnapshot] = []
        last_flush_time = time.time()

        while not self._stop_event.is_set():
            timeout = max(0.01, self.flush_interval_seconds - (time.time() - last_flush_time))
            try:
                snapshot = self.queue.get(timeout=timeout)
                batch.append(snapshot)
            except Empty:
                pass

            now = time.time()
            if batch and (
                len(batch) >= self.batch_size
                or (now - last_flush_time) >= self.flush_interval_seconds
            ):
                self._flush_batch(batch)
                batch = []
                last_flush_time = now

        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, snapshots: List[OrderBookSnapshot]) -> None:
        """
        执行单批次写入。

        当前实现为了尽快搭起第一版骨架，采用脚本拼接方式写入。
        后面如果确认 Python API 支持更高效的表参数传递，
        可以再升级成 dataframe / table 直传方式。
        """
        rows = [snapshot.to_storage_dict(depth=self.table_depth) for snapshot in snapshots]
        script = self._build_append_script(rows)

        try:
            self.client.run(script)
            with self._stats_lock:
                self.stats.flushed_batches += 1
                self.stats.flushed_rows += len(rows)
            logger.debug("订单簿批量写入成功: rows=%s", len(rows))
        except Exception:
            with self._stats_lock:
                self.stats.failed_batches += 1
            logger.exception("订单簿批量写入失败: rows=%s", len(rows))

    def _build_append_script(self, rows: List[Dict[str, object]]) -> str:
        """
        构造 DolphinDB append 脚本。

        当前使用“显式列别名”的方式构造临时表：
        `table(vec1 as col1, vec2 as col2, ...)`

        这样比 `table(colArrays, colNames)` 更稳，尤其适合当前版本的
        DolphinDB，避免列名列表在解析时出现歧义。
        """
        column_exprs = []

        for column in self.column_names:
            dtype = self.column_types[column]
            values = [row.get(column) for row in rows]
            vector_expr = self._format_column(values, dtype)
            column_exprs.append(f"{vector_expr} as {column}")

        return f"""
tb = loadTable("{self.config.db_path}", "{self.config.table_name}")
tmp = table({",".join(column_exprs)})
append!(tb, tmp)
""".strip()

    @staticmethod
    def _format_column(values: List[object], dtype: str) -> str:
        """
        按“列”构造 DolphinDB 向量字面量。

        这样可以正确处理 SYMBOL 等需要对整列做类型转换的场景，
        避免生成 `[symbol("x")]` 这种不兼容写法。
        """
        if dtype == "SYMBOL":
            raw_values = [DolphinDBWriter._format_string_value(value) for value in values]
            return f"symbol([{','.join(raw_values)}])"

        literals = [DolphinDBWriter._format_value(value, dtype) for value in values]
        return "[" + ",".join(literals) + "]"

    @staticmethod
    def _format_value(value: object, dtype: str) -> str:
        """
        把 Python 值转换为 DolphinDB 脚本中的字面量。

        这里按目标列类型输出，避免：
        - NULL 类型不明确
        - 时间列格式不稳定
        - SYMBOL / STRING 混用
        """
        if value is None:
            return DolphinDBWriter._null_literal(dtype)

        if dtype == "BOOL":
            return "true" if value else "false"

        if dtype in {"INT", "LONG"}:
            return str(int(value))

        if dtype == "DOUBLE":
            return repr(value)

        if dtype == "DATE":
            if isinstance(value, datetime):
                value = value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
            if not isinstance(value, date):
                raise TypeError(f"DATE 列需要 date 类型，当前值类型为 {type(value)!r}")
            return f'date("{value.strftime("%Y.%m.%d")}")'

        if dtype == "TIMESTAMP":
            if not isinstance(value, datetime):
                raise TypeError(f"TIMESTAMP 列需要 datetime 类型，当前值类型为 {type(value)!r}")
            dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
            ts = dt.strftime("%Y.%m.%dT%H:%M:%S.%f")[:-3]
            return f'timestamp("{ts}")'

        text = DolphinDBWriter._escape_string(str(value))
        return f'"{text}"'

    @staticmethod
    def _format_string_value(value: object) -> str:
        """把值格式化成 DolphinDB 字符串字面量。"""
        if value is None:
            return '""'
        text = DolphinDBWriter._escape_string(str(value))
        return f'"{text}"'

    @staticmethod
    def _null_literal(dtype: str) -> str:
        """返回对应 DolphinDB 类型的空值字面量。"""
        null_map = {
            "SYMBOL": "symbol(`)",
            "STRING": 'string(NULL)',
            "BOOL": "bool(NULL)",
            "INT": "int(NULL)",
            "LONG": "long(NULL)",
            "DOUBLE": "double(NULL)",
            "DATE": "date(NULL)",
            "TIMESTAMP": "timestamp(NULL)",
        }
        return null_map.get(dtype, "NULL")

    @staticmethod
    def _escape_string(value: str) -> str:
        """转义写入脚本中的字符串。"""
        return value.replace("\\", "\\\\").replace('"', '\\"')
