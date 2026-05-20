from __future__ import annotations

import argparse
from datetime import datetime, timedelta

try:
    import dolphindb as ddb
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: dolphindb. Install it before running.") from exc


DB_PATH = "dfs://BinanceMarket"
FACTOR_TABLE = "depthFactors"
LONG_LIQ_TABLE = "longLiquidationSignals"
SHORT_LIQ_TABLE = "shortLiquidationSignals"
DATASET_TABLE = "obiLiqPredictionDataset"

ROLLING_FEATURE_COLS = [
    "dynamicWeightOBIMean10",
    "dynamicWeightOBIMax10",
    "dynamicWeightOBIMin10",
    "dynamicWeightOBIChange10",
    "dynamicWeightOBIMean15",
    "dynamicWeightOBIChange15",
]

DATASET_COLUMNS = [
    "symbol",
    "tradeDate",
    "factorTime",
    "windowStart",
    "windowEnd",
    "dynamicWeightOBI",
    "cumulativeOFI1s",
    "avgFirstGradient",
    "avgSecondCurvature",
    *ROLLING_FEATURE_COLS,
    "longLiqUSDT",
    "shortLiqUSDT",
    "netLiqUSDT",
    "longLiqQty",
    "shortLiqQty",
    "netLiqQty",
    "longLiqCount",
    "shortLiqCount",
    "totalLiqCount",
    "liqDirection",
    "hasLiquidation",
]


def ddb_quote(value: str) -> str:
    """Escape Python strings before inserting them into DolphinDB scripts."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ddb_date(value: str) -> str:
    """Convert YYYY-MM-DD to DolphinDB date literal text YYYY.MM.DD."""
    return value.replace("-", ".")


def iter_dates(start_date: str, end_date: str):
    """Yield YYYY-MM-DD date strings from start_date to end_date inclusive."""
    current = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if current > end:
        raise ValueError("start_date cannot be later than end_date")

    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def create_dataset_table(
    session: ddb.Session,
    db_path: str,
    dataset_table: str,
    recreate: bool = False,
) -> None:
    """Create the prediction dataset table if it does not already exist."""
    recreate_script = (
        f'if(existsTable(dbPath, "{dataset_table}")){{\n'
        f'    dropTable(db, "{dataset_table}")\n'
        "}\n"
        if recreate
        else ""
    )
    script = f"""
dbPath = "{ddb_quote(db_path)}"
if(!existsDatabase(dbPath)){{
    throw "Database does not exist: " + dbPath
}}

db = database(dbPath)

{recreate_script}
if(!existsTable(dbPath, "{dataset_table}")){{
    schema = table(
        1:0,
        `symbol`tradeDate`factorTime`windowStart`windowEnd`dynamicWeightOBI`cumulativeOFI1s`avgFirstGradient`avgSecondCurvature`dynamicWeightOBIMean10`dynamicWeightOBIMax10`dynamicWeightOBIMin10`dynamicWeightOBIChange10`dynamicWeightOBIMean15`dynamicWeightOBIChange15`longLiqUSDT`shortLiqUSDT`netLiqUSDT`longLiqQty`shortLiqQty`netLiqQty`longLiqCount`shortLiqCount`totalLiqCount`liqDirection`hasLiquidation,
        [SYMBOL, DATE, TIMESTAMP, TIMESTAMP, TIMESTAMP, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, DOUBLE, INT, INT, INT, INT, BOOL]
    )
    createPartitionedTable(
        dbHandle=db,
        table=schema,
        tableName="{dataset_table}",
        partitionColumns=`tradeDate`symbol,
        sortColumns=`symbol`factorTime,
        keepDuplicates=LAST
    )
}}
"""
    session.run(script)


def get_table_columns(session: ddb.Session, db_path: str, table_name: str) -> list[str]:
    script = f"""
target = loadTable("{ddb_quote(db_path)}", "{ddb_quote(table_name)}")
select top 0 * from target
"""
    empty_frame = session.run(script)
    return list(empty_frame.columns)


def add_missing_dataset_columns(
    session: ddb.Session,
    db_path: str,
    dataset_table: str,
) -> None:
    """Add new rolling feature columns to an existing DFS dataset table."""
    actual_columns = get_table_columns(session, db_path, dataset_table)
    missing_columns = [col for col in ROLLING_FEATURE_COLS if col not in actual_columns]
    if not missing_columns:
        return

    unsupported_missing = [col for col in DATASET_COLUMNS if col not in actual_columns and col not in missing_columns]
    if unsupported_missing:
        raise SystemExit(
            "Dataset table is missing columns that cannot be auto-migrated safely:\n"
            f"{unsupported_missing}\n"
            "Use --recreate-table, or use a new --dataset-table name."
        )

    col_names = "`" + "`".join(missing_columns)
    col_types = ", ".join(["DOUBLE"] * len(missing_columns))
    script = f"""
dataset = loadTable("{ddb_quote(db_path)}", "{ddb_quote(dataset_table)}")
addColumn(dataset, {col_names}, [{col_types}])
"""
    session.run(script)


def validate_dataset_schema(session: ddb.Session, db_path: str, dataset_table: str) -> None:
    """Fail early if an old dataset table schema is still present."""
    actual_columns = get_table_columns(session, db_path, dataset_table)
    if actual_columns == DATASET_COLUMNS:
        return

    missing_columns = [col for col in DATASET_COLUMNS if col not in actual_columns]
    extra_columns = [col for col in actual_columns if col not in DATASET_COLUMNS]
    raise SystemExit(
        "Dataset table schema does not match the rolling-feature version.\n"
        f"Expected {len(DATASET_COLUMNS)} columns, got {len(actual_columns)} columns.\n"
        f"Missing columns: {missing_columns}\n"
        f"Extra columns: {extra_columns}\n"
        "Re-run OBI_dataset.py with --recreate-table, or use a new --dataset-table name."
    )


def delete_existing_dataset(
    session: ddb.Session,
    db_path: str,
    dataset_table: str,
    symbol: str,
    start_date: str,
    end_date: str,
) -> None:
    """Delete existing dataset rows for a symbol/date range before recomputing."""
    start_ddb_date = ddb_date(start_date)
    end_ddb_date = ddb_date(end_date)
    script = f"""
dbPath = "{ddb_quote(db_path)}"
dataset = loadTable(dbPath, "{dataset_table}")

delete from dataset
where symbol=`{symbol}, tradeDate between {start_ddb_date} : {end_ddb_date}
"""
    session.run(script)


def build_dataset_script(
    db_path: str,
    factor_table: str,
    long_liq_table: str,
    short_liq_table: str,
    dataset_table: str,
    symbol: str,
    trade_date: str,
    window_start_ms: int,
    window_end_ms: int,
    direction_threshold: float,
) -> str:
    """Build one-day DolphinDB script using wj to avoid symbol-level cross joins."""
    trade_ddb_date = ddb_date(trade_date)
    threshold = f"{direction_threshold:.12g}"

    return f"""
dbPath = "{ddb_quote(db_path)}"
factors = loadTable(dbPath, "{factor_table}")
longSignals = loadTable(dbPath, "{long_liq_table}")
shortSignals = loadTable(dbPath, "{short_liq_table}")
dataset = loadTable(dbPath, "{dataset_table}")

// X: 当前 OBI 因子快照。
// Y: 未来 [factorTime + {window_start_ms}ms, factorTime + {window_end_ms}ms] 的强平候选聚合。
factorRaw = select
    symbol,
    tradeDate,
    Time,
    dynamicWeightOBI,
    cumulativeOFI1s,
    avgFirstGradient,
    avgSecondCurvature
from factors
where symbol=`{symbol}, tradeDate = {trade_ddb_date}
order by symbol, Time

factorFeatures = select
    symbol,
    tradeDate,
    Time,
    dynamicWeightOBI,
    cumulativeOFI1s,
    avgFirstGradient,
    avgSecondCurvature,
    mavg(dynamicWeightOBI, 10) as dynamicWeightOBIMean10,
    mmax(dynamicWeightOBI, 10) as dynamicWeightOBIMax10,
    mmin(dynamicWeightOBI, 10) as dynamicWeightOBIMin10,
    dynamicWeightOBI - move(dynamicWeightOBI, 10) as dynamicWeightOBIChange10,
    mavg(dynamicWeightOBI, 15) as dynamicWeightOBIMean15,
    dynamicWeightOBI - move(dynamicWeightOBI, 15) as dynamicWeightOBIChange15
from factorRaw
context by symbol

baseFactors = select
    symbol,
    tradeDate,
    Time as factorTime,
    temporalAdd(Time, {window_start_ms}, `ms) as windowStart,
    temporalAdd(Time, {window_end_ms}, `ms) as windowEnd,
    dynamicWeightOBI,
    cumulativeOFI1s,
    avgFirstGradient,
    avgSecondCurvature,
    dynamicWeightOBIMean10,
    dynamicWeightOBIMax10,
    dynamicWeightOBIMin10,
    dynamicWeightOBIChange10,
    dynamicWeightOBIMean15,
    dynamicWeightOBIChange15
from factorFeatures
order by symbol, Time

longBase = select symbol, startTime, totalUSDT, totalQty
from longSignals
where symbol=`{symbol}, tradeDate = {trade_ddb_date}
order by symbol, startTime

shortBase = select symbol, startTime, totalUSDT, totalQty
from shortSignals
where symbol=`{symbol}, tradeDate = {trade_ddb_date}
order by symbol, startTime

// wj 使用左表 factorTime 和右表 startTime 做窗口连接。
// 这避免了 left join on symbol 造成的大中间表和 OOM。
longJoined = wj(baseFactors, longBase, {window_start_ms}:{window_end_ms}, <[sum(totalUSDT), sum(totalQty), count(startTime)]>, `symbol`factorTime, `symbol`startTime)

withLong = select
    symbol,
    tradeDate,
    factorTime,
    windowStart,
    windowEnd,
    dynamicWeightOBI,
    cumulativeOFI1s,
    avgFirstGradient,
    avgSecondCurvature,
    dynamicWeightOBIMean10,
    dynamicWeightOBIMax10,
    dynamicWeightOBIMin10,
    dynamicWeightOBIChange10,
    dynamicWeightOBIMean15,
    dynamicWeightOBIChange15,
    nullFill(sum_totalUSDT, 0.0) as longLiqUSDT,
    nullFill(sum_totalQty, 0.0) as longLiqQty,
    int(count_startTime) as longLiqCount
from longJoined

shortJoined = wj(withLong, shortBase, {window_start_ms}:{window_end_ms}, <[sum(totalUSDT), sum(totalQty), count(startTime)]>, `symbol`factorTime, `symbol`startTime)

result = select
    symbol,
    tradeDate,
    factorTime,
    windowStart,
    windowEnd,
    dynamicWeightOBI,
    cumulativeOFI1s,
    avgFirstGradient,
    avgSecondCurvature,
    dynamicWeightOBIMean10,
    dynamicWeightOBIMax10,
    dynamicWeightOBIMin10,
    dynamicWeightOBIChange10,
    dynamicWeightOBIMean15,
    dynamicWeightOBIChange15,
    longLiqUSDT,
    nullFill(sum_totalUSDT, 0.0) as shortLiqUSDT,
    nullFill(sum_totalUSDT, 0.0) - longLiqUSDT as netLiqUSDT,
    longLiqQty,
    nullFill(sum_totalQty, 0.0) as shortLiqQty,
    nullFill(sum_totalQty, 0.0) - longLiqQty as netLiqQty,
    longLiqCount,
    int(count_startTime) as shortLiqCount,
    longLiqCount + int(count_startTime) as totalLiqCount,
    iif(nullFill(sum_totalUSDT, 0.0) - longLiqUSDT > {threshold}, 1,
        iif(nullFill(sum_totalUSDT, 0.0) - longLiqUSDT < -{threshold}, -1, 0)) as liqDirection,
    longLiqCount + int(count_startTime) > 0 as hasLiquidation
from shortJoined

tableInsert(dataset, result)

select count(*) as rows from result
"""


def build_dataset_for_date(
    session: ddb.Session,
    db_path: str,
    factor_table: str,
    long_liq_table: str,
    short_liq_table: str,
    dataset_table: str,
    symbol: str,
    trade_date: str,
    window_start_ms: int,
    window_end_ms: int,
    direction_threshold: float,
):
    """Run the one-day DolphinDB dataset construction script."""
    script = build_dataset_script(
        db_path=db_path,
        factor_table=factor_table,
        long_liq_table=long_liq_table,
        short_liq_table=short_liq_table,
        dataset_table=dataset_table,
        symbol=symbol,
        trade_date=trade_date,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        direction_threshold=direction_threshold,
    )
    return session.run(script)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build OBI factor -> future liquidation prediction dataset."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--factor-table", default=FACTOR_TABLE)
    parser.add_argument("--long-liq-table", default=LONG_LIQ_TABLE)
    parser.add_argument("--short-liq-table", default=SHORT_LIQ_TABLE)
    parser.add_argument("--dataset-table", default=DATASET_TABLE)
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--start-date", default="2026-04-20")
    parser.add_argument("--end-date", default="2026-04-28")
    parser.add_argument("--window-start-ms", type=int, default=15)
    parser.add_argument("--window-end-ms", type=int, default=5000)
    parser.add_argument(
        "--direction-threshold",
        type=float,
        default=0.0,
        help="Minimum absolute net liquidation USDT for non-zero direction labels.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Delete existing dataset rows for the same symbol/date range before inserting.",
    )
    parser.add_argument(
        "--recreate-table",
        action="store_true",
        help="Drop and recreate the dataset table before inserting. Use this after schema changes.",
    )
    parser.add_argument(
        "--no-auto-migrate",
        action="store_true",
        help="Do not add missing rolling feature columns to an existing dataset table.",
    )
    args = parser.parse_args()

    if args.window_start_ms < 0:
        raise SystemExit("--window-start-ms cannot be negative")
    if args.window_end_ms <= args.window_start_ms:
        raise SystemExit("--window-end-ms must be greater than --window-start-ms")
    if args.direction_threshold < 0:
        raise SystemExit("--direction-threshold cannot be negative")

    session = ddb.Session()
    try:
        session.connect(args.host, args.port, args.user, args.password)
        create_dataset_table(
            session,
            args.db_path,
            args.dataset_table,
            recreate=args.recreate_table,
        )
        if not args.no_auto_migrate:
            add_missing_dataset_columns(session, args.db_path, args.dataset_table)
        validate_dataset_schema(session, args.db_path, args.dataset_table)
        if args.overwrite:
            delete_existing_dataset(
                session=session,
                db_path=args.db_path,
                dataset_table=args.dataset_table,
                symbol=args.symbol,
                start_date=args.start_date,
                end_date=args.end_date,
            )

        total_rows = 0
        for trade_date in iter_dates(args.start_date, args.end_date):
            result = build_dataset_for_date(
                session=session,
                db_path=args.db_path,
                factor_table=args.factor_table,
                long_liq_table=args.long_liq_table,
                short_liq_table=args.short_liq_table,
                dataset_table=args.dataset_table,
                symbol=args.symbol,
                trade_date=trade_date,
                window_start_ms=args.window_start_ms,
                window_end_ms=args.window_end_ms,
                direction_threshold=args.direction_threshold,
            )
            rows = int(result["rows"].iloc[0])
            total_rows += rows
            print(f"{trade_date}: inserted {rows:,} rows")

        print(f"Inserted dataset rows: {total_rows:,}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
