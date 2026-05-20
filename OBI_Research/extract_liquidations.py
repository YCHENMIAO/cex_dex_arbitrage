#我们用 10 毫秒的窗口，足以把同一个强平动作引发的所有连续 aggTrade 重新聚合成一条记录。
#计算吃单起始价；吃单结束价；滑点(插针幅度)；总成交数量；总成交金额(USDT)；击穿了多少个聚合价格档位；实际撮合的底层原始订单数


from __future__ import annotations
import argparse
from datetime import datetime, timedelta
try:
    import dolphindb as ddb
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: dolphindb. Install it before running.") from exc

DB_PATH = "dfs://BinanceMarket"
SOURCE_TABLE = "aggTrades"
LONG_TABLE = "longLiquidationSignals"
SHORT_TABLE = "shortLiquidationSignals"


def ddb_quote(value: str) -> str:
    """Escape Python strings before inserting them into DolphinDB scripts."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ddb_date(value: str) -> str:
    """Convert YYYY-MM-DD to DolphinDB date literal text YYYY.MM.DD."""
    return value.replace("-", ".")


def create_signal_tables(
    session: ddb.Session,
    db_path: str,
    long_table: str,
    short_table: str,
) -> None:
    """Create two DFS tables for inferred long/short liquidation signals."""
    script = f"""
dbPath = "{ddb_quote(db_path)}"
if(!existsDatabase(dbPath)){{
    throw "Database does not exist: " + dbPath
}}

db = database(dbPath)

schema = table(
    1:0,
    `symbol`tradeDate`timeWindow`startTime`endTime`startPrice`endPrice`slippage`totalQty`totalUSDT`sweptLevels`rawTradeCount`isBuyerMaker`liquidationSide,
    [SYMBOL, DATE, TIMESTAMP, TIMESTAMP, TIMESTAMP, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, INT, LONG, BOOL, SYMBOL]
)

if(!existsTable(dbPath, "{long_table}")){{
    createPartitionedTable(
        dbHandle=db,
        table=schema,
        tableName="{long_table}",
        partitionColumns=`tradeDate`symbol,
        sortColumns=`symbol`startTime,
        keepDuplicates=LAST
    )
}}

if(!existsTable(dbPath, "{short_table}")){{
    createPartitionedTable(
        dbHandle=db,
        table=schema,
        tableName="{short_table}",
        partitionColumns=`tradeDate`symbol,
        sortColumns=`symbol`startTime,
        keepDuplicates=LAST
    )
}}
"""
    session.run(script)


def build_extract_script(
    db_path: str,
    source_table: str,
    long_table: str,
    short_table: str,
    symbol: str,
    trade_date: str,
    notional_threshold: float,
    window_ms: int,
) -> str:
    """Build DolphinDB script that infers liquidation-like bursts from aggTrades."""
    trade_ddb_date = ddb_date(trade_date)
    threshold = f"{notional_threshold:.12g}"
    window = f"{window_ms}ms"

    return f"""
dbPath = "{ddb_quote(db_path)}"
trades = loadTable(dbPath, "{source_table}")
longSignals = loadTable(dbPath, "{long_table}")
shortSignals = loadTable(dbPath, "{short_table}")

// 提取指定日期和币种的底表数据
baseTrades = select
    symbol,
    tradeDate,
    aggTradeId,
    price,
    quantity,
    firstTradeId,
    lastTradeId,
    transactTime,
    isBuyerMaker
from trades
where symbol=`{symbol},
      tradeDate = {trade_ddb_date}

// 修正：DolphinDB会自动将group by的列放在结果的前几列
// 不要在select中重复写symbol, isBuyerMaker和bar(...)
potential = select
    first(tradeDate) as tradeDate,
    first(transactTime) as startTime,
    last(transactTime) as endTime,
    first(price) as startPrice,
    last(price) as endPrice,
    abs(last(price) - first(price)) as slippage,
    sum(quantity) as totalQty,
    sum(price * quantity) as totalUSDT,
    count(aggTradeId) as sweptLevels,
    sum(lastTradeId - firstTradeId + 1) as rawTradeCount
from baseTrades
group by 
    symbol, 
    isBuyerMaker, 
    bar(transactTime, {window}) as timeWindow  // 在这里定义别名
having sum(price * quantity) > {threshold}

// potential 表现在包含了 symbol, isBuyerMaker, timeWindow 以及上面 select 聚合的结果
longLiquidations = select
    symbol,
    tradeDate,
    timeWindow,
    startTime,
    endTime,
    startPrice,
    endPrice,
    slippage,
    totalQty,
    totalUSDT,
    int(sweptLevels) as sweptLevels,
    long(rawTradeCount) as rawTradeCount,
    isBuyerMaker,
    `long as liquidationSide
from potential
where isBuyerMaker = true

shortLiquidations = select
    symbol,
    tradeDate,
    timeWindow,
    startTime,
    endTime,
    startPrice,
    endPrice,
    slippage,
    totalQty,
    totalUSDT,
    int(sweptLevels) as sweptLevels,
    long(rawTradeCount) as rawTradeCount,
    isBuyerMaker,
    `short as liquidationSide
from potential
where isBuyerMaker = false

tableInsert(longSignals, longLiquidations)
tableInsert(shortSignals, shortLiquidations)

longRows = exec count(*) from longLiquidations
shortRows = exec count(*) from shortLiquidations
table(longRows as longRows, shortRows as shortRows)
"""


def extract_liquidations(
    session: ddb.Session,
    db_path: str,
    source_table: str,
    long_table: str,
    short_table: str,
    symbol: str,
    trade_date: str,
    notional_threshold: float,
    window_ms: int,
):
    """Run the DolphinDB extraction script."""
    script = build_extract_script(
        db_path=db_path,
        source_table=source_table,
        long_table=long_table,
        short_table=short_table,
        symbol=symbol,
        trade_date=trade_date,
        notional_threshold=notional_threshold,
        window_ms=window_ms,
    )
    return session.run(script)


def iter_dates(start_date: str, end_date: str):
    """Yield YYYY-MM-DD date strings from start_date to end_date inclusive."""
    current = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if current > end:
        raise ValueError("start_date cannot be later than end_date")

    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def delete_existing_signals(
    session: ddb.Session,
    db_path: str,
    long_table: str,
    short_table: str,
    symbol: str,
    start_date: str,
    end_date: str,
) -> None:
    """Delete existing inferred signals for a symbol/date range before recomputing."""
    start_ddb_date = ddb_date(start_date)
    end_ddb_date = ddb_date(end_date)
    script = f"""
dbPath = "{ddb_quote(db_path)}"
longSignals = loadTable(dbPath, "{long_table}")
shortSignals = loadTable(dbPath, "{short_table}")

delete from longSignals
where symbol=`{symbol}, tradeDate between {start_ddb_date} : {end_ddb_date}

delete from shortSignals
where symbol=`{symbol}, tradeDate between {start_ddb_date} : {end_ddb_date}
"""
    session.run(script)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Infer liquidation-like signals from Binance aggTrades in DolphinDB."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--source-table", default=SOURCE_TABLE)
    parser.add_argument("--long-table", default=LONG_TABLE)
    parser.add_argument("--short-table", default=SHORT_TABLE)
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--start-date", default="2026-04-20")
    parser.add_argument("--end-date", default="2026-04-28")
    parser.add_argument("--notional-threshold", type=float, default=50_000.0)
    parser.add_argument("--window-ms", type=int, default=10)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete existing inferred signals for the same symbol/date range before inserting.",
    )
    args = parser.parse_args()

    if args.window_ms <= 0:
        raise SystemExit("--window-ms must be positive")
    if args.notional_threshold <= 0:
        raise SystemExit("--notional-threshold must be positive")

    session = ddb.Session()
    try:
        session.connect(args.host, args.port, args.user, args.password)
        create_signal_tables(session, args.db_path, args.long_table, args.short_table)
        if args.overwrite:
            delete_existing_signals(
                session=session,
                db_path=args.db_path,
                long_table=args.long_table,
                short_table=args.short_table,
                symbol=args.symbol,
                start_date=args.start_date,
                end_date=args.end_date,
            )

        total_long_rows = 0
        total_short_rows = 0
        for trade_date in iter_dates(args.start_date, args.end_date):
            result = extract_liquidations(
                session=session,
                db_path=args.db_path,
                source_table=args.source_table,
                long_table=args.long_table,
                short_table=args.short_table,
                symbol=args.symbol,
                trade_date=trade_date,
                notional_threshold=args.notional_threshold,
                window_ms=args.window_ms,
            )
            long_rows = int(result["longRows"].iloc[0])
            short_rows = int(result["shortRows"].iloc[0])
            total_long_rows += long_rows
            total_short_rows += short_rows
            print(f"{trade_date}: long={long_rows:,}, short={short_rows:,}")

        print(f"Inserted inferred signals: long={total_long_rows:,}, short={total_short_rows:,}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
