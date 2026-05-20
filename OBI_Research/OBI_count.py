from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

try:
    import dolphindb as ddb
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Missing dependency: dolphindb. Install it in this venv before running."
    ) from exc


DB_PATH = "dfs://BinanceMarket"
BOOK_DEPTH_TABLE = "bookDepth"
AGG_TRADES_TABLE = "aggTrades"


def ddb_quote(value: str) -> str:
    """转义传给 DolphinDB 脚本的字符串，避免路径里的反斜杠或引号破坏脚本。"""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def create_tables(session: ddb.Session, db_path: str = DB_PATH) -> None:
    """在已有 DolphinDB 数据库中创建 bookDepth 和 aggTrades 两张 TSDB 分区表。"""
    script = f"""
dbPath = "{ddb_quote(db_path)}"
if(!existsDatabase(dbPath)){{
    throw "Database does not exist: " + dbPath
}}

db = database(dbPath)

if(!existsTable(dbPath, "{BOOK_DEPTH_TABLE}")){{
    bookDepthSchema = table(
        1:0,
        // tradeDate 和 symbol 对应你建库时的两个 VALUE 分区字段。
        // eventTime 是盘口快照时间；percentage 是盘口深度区间，比如 -5、-4、...、5。
        `symbol`tradeDate`eventTime`percentage`depth`notional,
        [SYMBOL, DATE, TIMESTAMP, DOUBLE, DOUBLE, DOUBLE]
    )
    createPartitionedTable(
        dbHandle=db,
        table=bookDepthSchema,
        tableName="{BOOK_DEPTH_TABLE}",
        partitionColumns=`tradeDate`symbol,
        // TSDB sortColumns 不能包含 DOUBLE，所以 percentage 不放进排序列。
        sortColumns=`symbol`eventTime,
        keepDuplicates=ALL
    )
}}

if(!existsTable(dbPath, "{AGG_TRADES_TABLE}")){{
    aggTradesSchema = table(
        1:0,
        // aggTrades 后续用于和 OBI 按 transactTime / eventTime 做对齐分析。
        `symbol`tradeDate`aggTradeId`price`quantity`firstTradeId`lastTradeId`transactTime`isBuyerMaker,
        [SYMBOL, DATE, LONG, DOUBLE, DOUBLE, LONG, LONG, TIMESTAMP, BOOL]
    )
    createPartitionedTable(
        dbHandle=db,
        table=aggTradesSchema,
        tableName="{AGG_TRADES_TABLE}",
        partitionColumns=`tradeDate`symbol,
        sortColumns=`symbol`transactTime`aggTradeId,
        keepDuplicates=ALL
    )
}}
"""
    session.run(script)


def parse_market_file(path: Path, data_type: str) -> tuple[str, str]:
    """从 Binance 文件名中解析交易对和日期，例如 ETHUSDT-bookDepth-2026-04-20.csv。"""
    pattern = re.compile(rf"^([A-Z0-9]+)-{re.escape(data_type)}-(\d{{4}}-\d{{2}}-\d{{2}})\.csv$")
    match = pattern.match(path.name)
    if not match:
        raise ValueError(f"Unexpected {data_type} file name: {path.name}")
    return match.group(1), match.group(2)


def iter_files(data_dir: Path, data_type: str, symbol: str | None) -> Iterable[Path]:
    """按数据类型和交易对筛选待导入 CSV 文件。"""
    files = sorted(data_dir.glob(f"*-{data_type}-*.csv"))
    if symbol:
        files = [path for path in files if path.name.startswith(f"{symbol}-")]
    return files


def prepare_book_depth_chunk(chunk: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """把 bookDepth CSV 分块整理成 DolphinDB 表的列顺序和类型。"""
    chunk = chunk.rename(columns={"timestamp": "eventTime"}).copy()
    # 原始 bookDepth 文件没有 symbol 和 tradeDate；这里补齐分区列。
    chunk["symbol"] = symbol
    chunk["eventTime"] = pd.to_datetime(chunk["eventTime"], errors="raise")
    chunk["tradeDate"] = chunk["eventTime"].dt.normalize()
    for col in ["percentage", "depth", "notional"]:
        chunk[col] = pd.to_numeric(chunk[col], errors="raise")
    return chunk[["symbol", "tradeDate", "eventTime", "percentage", "depth", "notional"]]


def prepare_agg_trades_chunk(chunk: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """把 aggTrades CSV 分块整理成 DolphinDB 表的列顺序和类型。"""
    chunk = chunk.rename(
        columns={
            "agg_trade_id": "aggTradeId",
            "first_trade_id": "firstTradeId",
            "last_trade_id": "lastTradeId",
            "transact_time": "transactTime",
            "is_buyer_maker": "isBuyerMaker",
        }
    ).copy()
    # Binance aggTrades 的成交时间是毫秒时间戳。
    chunk["symbol"] = symbol
    chunk["transactTime"] = pd.to_datetime(chunk["transactTime"], unit="ms", errors="raise")
    chunk["tradeDate"] = chunk["transactTime"].dt.normalize()
    for col in ["aggTradeId", "firstTradeId", "lastTradeId"]:
        chunk[col] = pd.to_numeric(chunk[col], errors="raise").astype("int64")
    for col in ["price", "quantity"]:
        chunk[col] = pd.to_numeric(chunk[col], errors="raise")
    chunk["isBuyerMaker"] = chunk["isBuyerMaker"].astype(bool)
    return chunk[
        [
            "symbol",
            "tradeDate",
            "aggTradeId",
            "price",
            "quantity",
            "firstTradeId",
            "lastTradeId",
            "transactTime",
            "isBuyerMaker",
        ]
    ]


def append_csv_files(
    session: ddb.Session,
    db_path: str,
    table_name: str,
    files: Iterable[Path],
    data_type: str,
    chunksize: int,
    append_existing: bool,
) -> int:
    """分块读取 CSV 并追加到 DolphinDB，避免一次性把大文件全部读进内存。"""
    appender = ddb.tableAppender(dbPath=db_path, tableName=table_name, ddbSession=session)
    total_rows = 0

    for path in files:
        symbol, file_date = parse_market_file(path, data_type)
        # 默认跳过已经导入过的 symbol/date，防止重复运行脚本导致重复数据。
        existing_rows = count_existing_file_rows(session, db_path, table_name, symbol, file_date)
        if existing_rows and not append_existing:
            print(f"Skipping {path.name}: {existing_rows:,} rows already exist")
            continue

        file_rows = 0
        print(f"Importing {path.name} ...")
        for chunk in pd.read_csv(path, chunksize=chunksize):
            if data_type == "bookDepth":
                prepared = prepare_book_depth_chunk(chunk, symbol)
            elif data_type == "aggTrades":
                prepared = prepare_agg_trades_chunk(chunk, symbol)
            else:
                raise ValueError(f"Unsupported data_type: {data_type}")

            appender.append(prepared)
            rows = len(prepared)
            file_rows += rows
            total_rows += rows

        print(f"  {file_date} {symbol}: {file_rows:,} rows")

    return total_rows


def count_rows(session: ddb.Session, db_path: str, table_name: str) -> int:
    """查询整张表当前行数，用于导入后的快速校验。"""
    script = f'select count(*) as rows from loadTable("{ddb_quote(db_path)}", "{table_name}")'
    result = session.run(script)
    return int(result["rows"].iloc[0])


def count_existing_file_rows(
    session: ddb.Session,
    db_path: str,
    table_name: str,
    symbol: str,
    file_date: str,
) -> int:
    """查询某个交易对某一天是否已有数据，用于防重复导入。"""
    ddb_date = file_date.replace("-", ".")
    script = f"""
select count(*) as rows
from loadTable("{ddb_quote(db_path)}", "{table_name}")
where symbol=`{symbol}, tradeDate={ddb_date}
"""
    result = session.run(script)
    return int(result["rows"].iloc[0])


def main() -> None:
    """命令行入口：连接 DolphinDB、建表、导入 bookDepth，并可选导入 aggTrades。"""
    parser = argparse.ArgumentParser(
        description="Create DolphinDB market tables and import Binance bookDepth CSV files."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "coindata"))
    parser.add_argument("--symbol", default="ETHUSDT")
    # aggTrades 文件较大，chunksize 可以按内存大小调小或调大。
    parser.add_argument("--chunksize", type=int, default=200_000)
    parser.add_argument(
        "--with-aggtrades",
        action="store_true",
        help="Also import aggTrades CSV files. These files are much larger than bookDepth.",
    )
    parser.add_argument(
        "--append-existing",
        action="store_true",
        help="Append even when rows already exist for the same symbol/date.",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        raise SystemExit(f"Data directory does not exist: {data_dir}")

    session = ddb.Session()
    try:
        session.connect(args.host, args.port, args.user, args.password)
        print(f"Connected to DolphinDB {args.host}:{args.port}, db={args.db_path}")
        create_tables(session, args.db_path)
        print(f"Tables are ready: {BOOK_DEPTH_TABLE}, {AGG_TRADES_TABLE}")

        book_depth_files = list(iter_files(data_dir, "bookDepth", args.symbol))
        if not book_depth_files:
            raise SystemExit(f"No bookDepth CSV files found in {data_dir} for symbol={args.symbol}")

        imported = append_csv_files(
            session=session,
            db_path=args.db_path,
            table_name=BOOK_DEPTH_TABLE,
            files=book_depth_files,
            data_type="bookDepth",
            chunksize=args.chunksize,
            append_existing=args.append_existing,
        )
        print(f"Imported {imported:,} bookDepth rows")

        if args.with_aggtrades:
            agg_trade_files = list(iter_files(data_dir, "aggTrades", args.symbol))
            if not agg_trade_files:
                print(f"No aggTrades CSV files found in {data_dir} for symbol={args.symbol}")
            else:
                imported = append_csv_files(
                    session=session,
                    db_path=args.db_path,
                    table_name=AGG_TRADES_TABLE,
                    files=agg_trade_files,
                    data_type="aggTrades",
                    chunksize=args.chunksize,
                    append_existing=args.append_existing,
                )
                print(f"Imported {imported:,} aggTrades rows")

        print(f"{BOOK_DEPTH_TABLE} total rows: {count_rows(session, args.db_path, BOOK_DEPTH_TABLE):,}")
        print(f"{AGG_TRADES_TABLE} total rows: {count_rows(session, args.db_path, AGG_TRADES_TABLE):,}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
