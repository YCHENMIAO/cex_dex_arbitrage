from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

import pandas as pd

try:
    import dolphindb as ddb
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: dolphindb. Install it before running.") from exc


DB_PATH = "dfs://BinanceMarket"
TABLE_NAME = "aggTrades"


def ddb_quote(value: str) -> str:
    """Escape Python strings before interpolating them into DolphinDB scripts."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def create_aggtrades_table(session: ddb.Session, db_path: str) -> None:
    """Create the DFS aggTrades table if it does not already exist."""
    script = f"""
dbPath = "{ddb_quote(db_path)}"
if(!existsDatabase(dbPath)){{
    throw "Database does not exist: " + dbPath
}}

db = database(dbPath)

if(!existsTable(dbPath, "{TABLE_NAME}")){{
    schema = table(
        1:0,
        `symbol`tradeDate`aggTradeId`price`quantity`firstTradeId`lastTradeId`transactTime`isBuyerMaker,
        [SYMBOL, DATE, LONG, DOUBLE, DOUBLE, LONG, LONG, TIMESTAMP, BOOL]
    )
    createPartitionedTable(
        dbHandle=db,
        table=schema,
        tableName="{TABLE_NAME}",
        partitionColumns=`tradeDate`symbol,
        sortColumns=`symbol`transactTime`aggTradeId,
        keepDuplicates=ALL
    )
}}
"""
    session.run(script)


def parse_aggtrades_file(path: Path) -> tuple[str, str]:
    """Parse file names like ETHUSDT-aggTrades-2026-04-20.csv."""
    match = re.match(r"^([A-Z0-9]+)-aggTrades-(\d{4}-\d{2}-\d{2})\.csv$", path.name)
    if not match:
        raise ValueError(f"Unexpected aggTrades file name: {path.name}")
    return match.group(1), match.group(2)


def iter_aggtrades_files(data_dir: Path, symbol: str | None) -> Iterable[Path]:
    """Return aggTrades CSV files sorted by name."""
    files = sorted(data_dir.glob("*-aggTrades-*.csv"))
    if symbol:
        files = [path for path in files if path.name.startswith(f"{symbol}-")]
    return files


def prepare_chunk(chunk: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Convert one raw Binance aggTrades CSV chunk to the DolphinDB table schema."""
    chunk = chunk.rename(
        columns={
            "agg_trade_id": "aggTradeId",
            "first_trade_id": "firstTradeId",
            "last_trade_id": "lastTradeId",
            "transact_time": "transactTime",
            "is_buyer_maker": "isBuyerMaker",
        }
    ).copy()

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


def count_existing_rows(
    session: ddb.Session,
    db_path: str,
    symbol: str,
    file_date: str,
) -> int:
    """Check whether this symbol/date has already been imported."""
    ddb_date = file_date.replace("-", ".")
    script = f"""
select count(*) as rows
from loadTable("{ddb_quote(db_path)}", "{TABLE_NAME}")
where symbol=`{symbol}, tradeDate={ddb_date}
"""
    result = session.run(script)
    return int(result["rows"].iloc[0])


def import_files(
    session: ddb.Session,
    db_path: str,
    files: Iterable[Path],
    chunksize: int,
    append_existing: bool,
) -> int:
    """Read raw CSV files in chunks and append them to the DFS table."""
    appender = ddb.tableAppender(dbPath=db_path, tableName=TABLE_NAME, ddbSession=session)
    total_rows = 0

    for path in files:
        symbol, file_date = parse_aggtrades_file(path)
        existing_rows = count_existing_rows(session, db_path, symbol, file_date)
        if existing_rows and not append_existing:
            print(f"Skipping {path.name}: {existing_rows:,} rows already exist")
            continue

        file_rows = 0
        print(f"Importing {path.name} ...")
        for chunk in pd.read_csv(path, chunksize=chunksize):
            prepared = prepare_chunk(chunk, symbol)
            appender.append(prepared)
            file_rows += len(prepared)
            total_rows += len(prepared)

        print(f"  {file_date} {symbol}: {file_rows:,} rows")

    return total_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Import raw Binance aggTrades CSV files to DolphinDB.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parents[1] / "coindata"))
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--chunksize", type=int, default=200_000)
    parser.add_argument(
        "--append-existing",
        action="store_true",
        help="Append even when rows already exist for the same symbol/date.",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not data_dir.exists():
        raise SystemExit(f"Data directory does not exist: {data_dir}")

    files = list(iter_aggtrades_files(data_dir, args.symbol))
    if not files:
        raise SystemExit(f"No aggTrades CSV files found in {data_dir} for symbol={args.symbol}")

    session = ddb.Session()
    try:
        session.connect(args.host, args.port, args.user, args.password)
        create_aggtrades_table(session, args.db_path)
        imported_rows = import_files(
            session=session,
            db_path=args.db_path,
            files=files,
            chunksize=args.chunksize,
            append_existing=args.append_existing,
        )
        print(f"Imported {imported_rows:,} aggTrades rows")
    finally:
        session.close()


if __name__ == "__main__":
    main()
