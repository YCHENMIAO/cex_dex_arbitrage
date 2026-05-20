"""
DolphinDB 建表脚本生成器。

当前阶段先围绕订单簿快照宽表 `orderbook_l5` 提供建表脚本，
方便 writer 在首次启动时自动初始化表结构。
"""

from __future__ import annotations

from typing import Dict, List


def get_orderbook_columns(depth: int = 5) -> List[str]:
    """
    返回订单簿宽表字段列表。

    字段分为：
    1. 基础元信息
    2. 衍生指标
    3. 前 N 档盘口字段
    """
    columns = [
        "exchange",
        "symbol",
        "event_date",
        "event_time",
        "receive_time",
        "source",
        "sequence_id",
        "is_snapshot",
        "raw_timestamp_ms",
        "mid_price",
        "spread",
    ]

    for index in range(1, depth + 1):
        columns.extend(
            [
                f"bid_px_{index}",
                f"bid_sz_{index}",
                f"bid_orders_{index}",
                f"ask_px_{index}",
                f"ask_sz_{index}",
                f"ask_orders_{index}",
            ]
        )

    return columns


def get_orderbook_column_types(depth: int = 5) -> Dict[str, str]:
    """
    返回订单簿宽表字段与 DolphinDB 类型的映射。

    这个映射主要给 writer 使用，保证：
    1. 列顺序固定
    2. 空值能按正确类型输出
    3. Python 值能按字段类型序列化为 DolphinDB 字面量
    """
    column_types: Dict[str, str] = {
        "exchange": "SYMBOL",
        "symbol": "SYMBOL",
        "event_date": "DATE",
        "event_time": "TIMESTAMP",
        "receive_time": "TIMESTAMP",
        "source": "SYMBOL",
        "sequence_id": "STRING",
        "is_snapshot": "BOOL",
        "raw_timestamp_ms": "LONG",
        "mid_price": "DOUBLE",
        "spread": "DOUBLE",
    }

    for index in range(1, depth + 1):
        column_types[f"bid_px_{index}"] = "DOUBLE"
        column_types[f"bid_sz_{index}"] = "DOUBLE"
        column_types[f"bid_orders_{index}"] = "INT"
        column_types[f"ask_px_{index}"] = "DOUBLE"
        column_types[f"ask_sz_{index}"] = "DOUBLE"
        column_types[f"ask_orders_{index}"] = "INT"

    return column_types


def get_create_orderbook_table_script(
    db_path: str = "dfs://cex_dex_arbitrage",
    table_name: str = "orderbook_l5",
    depth: int = 5,
) -> str:
    """
    生成创建订单簿宽表的 DolphinDB 脚本。

    设计选择：
    - 使用现有 VALUE/DATE 分区库
    - 用 `event_date` 作为分区列
    - 当前数据库引擎是 OLAP，因此不传 TSDB 专属参数
    - 使用宽表存前 N 档盘口，方便后续直接做特征工程
    """
    if depth <= 0:
        raise ValueError("depth 必须大于 0")

    column_names = get_orderbook_columns(depth=depth)
    column_types = get_orderbook_column_types(depth=depth)

    schema_fields = "`" + "`".join(column_names)
    schema_types = "[" + ", ".join(column_types[column] for column in column_names) + "]"

    return f"""
if(!existsDatabase("{db_path}")){{
    throw "DolphinDB 数据库不存在，请先手动创建库: {db_path}"
}}

db = database("{db_path}")

if(!existsTable("{db_path}", "{table_name}")){{
    schemaTb = table(1:0, {schema_fields}, {schema_types})
    pt = createPartitionedTable(
        db,
        schemaTb,
        "{table_name}",
        `event_date
    )
}}
""".strip()
