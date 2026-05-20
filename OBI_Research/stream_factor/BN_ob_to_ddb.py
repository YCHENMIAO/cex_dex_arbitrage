from __future__ import annotations
#自动创建两张 TSDB 分区表：
#bnDepthUpdates：保存 snapshot 和 diff 的逐价格档更新长表
#bnOrderBookTopN：保存每个时间点的盘口 Top N 条数据
#Python 内存维护当前 orderbook，DolphinDB 负责持久化事件和 topN 快照
import argparse
import asyncio
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pandas as pd
import websockets

try:
    import dolphindb as ddb
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: dolphindb. Install it before running.") from exc


DB_PATH = "dfs://BinanceFuturesOrderBook"
DEPTH_UPDATE_TABLE = "bnDepthUpdates"
TOPN_TABLE = "bnOrderBookTopN"


class OrderBookResyncRequired(RuntimeError):
    """Raised when Binance update IDs show a gap and the book must be rebuilt."""


@dataclass(frozen=True)
class BinanceConfig:
    symbols: tuple[str, ...]
    ws_base: str
    rest_base: str
    snapshot_limit: int
    update_speed: str

    @property
    def normalized_symbols(self) -> tuple[str, ...]:
        return tuple(symbol.upper() for symbol in self.symbols)

    @property
    def stream_symbols(self) -> tuple[str, ...]:
        return tuple(symbol.lower() for symbol in self.symbols)

    @property
    def ws_url(self) -> str:
        # 合约 diff depth 流，不要写成 depth20；depth20 是 partial book，不适合维护本地全量盘口。
        streams = "/".join(
            f"{symbol}@depth@{self.update_speed}" for symbol in self.stream_symbols
        )
        if len(self.symbols) == 1:
            return f"{self.ws_base.rstrip('/')}/ws/{streams}"
        return f"{self.ws_base.rstrip('/')}/stream?streams={streams}"

    def snapshot_url(self, symbol: str) -> str:
        query = urllib.parse.urlencode(
            {"symbol": symbol.upper(), "limit": self.snapshot_limit}
        )
        return f"{self.rest_base.rstrip('/')}/fapi/v1/depth?{query}"


@dataclass(frozen=True)
class DolphinDBConfig:
    host: str
    port: int
    user: str
    password: str
    db_path: str
    depth_update_table: str
    topn_table: str
    date_start: str
    date_end: str
    topn_depth: int
    batch_size: int
    flush_interval: float


def ddb_quote(value: str) -> str:
    """Escape Python strings before embedding them in DolphinDB scripts."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ddb_date(value: str) -> str:
    """Convert YYYY-MM-DD to DolphinDB date literal text YYYY.MM.DD."""
    return value.replace("-", ".")


def unix_ms_to_timestamp(value: int) -> pd.Timestamp:
    """Binance timestamps are Unix milliseconds. Store them as UTC-naive TIMESTAMP."""
    return pd.to_datetime(value, unit="ms")


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def make_trade_date(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.normalize()


def parse_levels(levels: list[list[str]]) -> dict[Decimal, Decimal]:
    """Convert Binance price/quantity strings to Decimal for stable price-key matching."""
    return {Decimal(price): Decimal(qty) for price, qty in levels if Decimal(qty) != 0}


class LocalOrderBook:
    """In-memory local order book maintained exactly from one REST snapshot plus WS diffs."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None

    def load_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.bids = parse_levels(snapshot["bids"])
        self.asks = parse_levels(snapshot["asks"])
        self.last_update_id = int(snapshot["lastUpdateId"])

    def apply_event(self, event: dict[str, Any]) -> None:
        """Apply one Binance depthUpdate after sequence checks have passed."""
        for price_text, qty_text in event["b"]:
            self._apply_level(self.bids, Decimal(price_text), Decimal(qty_text))
        for price_text, qty_text in event["a"]:
            self._apply_level(self.asks, Decimal(price_text), Decimal(qty_text))
        self.last_update_id = int(event["u"])

    @staticmethod
    def _apply_level(book_side: dict[Decimal, Decimal], price: Decimal, qty: Decimal) -> None:
        # Binance diff depth 里的 qty 是该价格档的绝对挂单量；0 表示该价位应删除。
        if qty == 0:
            book_side.pop(price, None)
            return
        book_side[price] = qty

    def topn(self, depth: int) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
        bids = sorted(self.bids.items(), key=lambda item: item[0], reverse=True)[:depth]
        asks = sorted(self.asks.items(), key=lambda item: item[0])[:depth]
        return bids, asks


class DolphinDBWriter:
    """Small buffered writer around DolphinDB tableAppender."""

    def __init__(self, session: ddb.Session, config: DolphinDBConfig) -> None:
        self.config = config
        self.depth_appender = ddb.tableAppender(
            dbPath=config.db_path,
            tableName=config.depth_update_table,
            ddbSession=session,
        )
        self.topn_appender = ddb.tableAppender(
            dbPath=config.db_path,
            tableName=config.topn_table,
            ddbSession=session,
        )
        self.depth_rows: list[dict[str, Any]] = []
        self.topn_rows: list[dict[str, Any]] = []
        self.last_flush_at = time.monotonic()

    def add_snapshot_rows(
        self,
        symbol: str,
        snapshot: dict[str, Any],
        recv_ts: pd.Timestamp,
    ) -> None:
        last_update_id = int(snapshot["lastUpdateId"])
        for side, levels in (("bid", snapshot["bids"]), ("ask", snapshot["asks"])):
            for price, qty in levels:
                self.depth_rows.append(
                    self._depth_row(
                        symbol=symbol,
                        event_ts=recv_ts,
                        transaction_ts=recv_ts,
                        recv_ts=recv_ts,
                        first_update_id=last_update_id,
                        final_update_id=last_update_id,
                        prev_final_update_id=-1,
                        side=side,
                        price=price,
                        quantity=qty,
                        is_snapshot=True,
                    )
                )

    def add_event_rows(self, symbol: str, event: dict[str, Any], recv_ts: pd.Timestamp) -> None:
        event_ts = unix_ms_to_timestamp(int(event["E"]))
        transaction_ts = unix_ms_to_timestamp(int(event.get("T", event["E"])))
        first_update_id = int(event["U"])
        final_update_id = int(event["u"])
        prev_final_update_id = int(event.get("pu", -1))

        for side, levels in (("bid", event["b"]), ("ask", event["a"])):
            for price, qty in levels:
                self.depth_rows.append(
                    self._depth_row(
                        symbol=symbol,
                        event_ts=event_ts,
                        transaction_ts=transaction_ts,
                        recv_ts=recv_ts,
                        first_update_id=first_update_id,
                        final_update_id=final_update_id,
                        prev_final_update_id=prev_final_update_id,
                        side=side,
                        price=price,
                        quantity=qty,
                        is_snapshot=False,
                    )
                )

    def add_topn_row(
        self,
        symbol: str,
        event: dict[str, Any] | None,
        book: LocalOrderBook,
        recv_ts: pd.Timestamp,
    ) -> None:
        event_ts = recv_ts if event is None else unix_ms_to_timestamp(int(event["E"]))
        transaction_ts = recv_ts if event is None else unix_ms_to_timestamp(int(event.get("T", event["E"])))
        first_update_id = book.last_update_id if event is None else int(event["U"])
        final_update_id = int(book.last_update_id or -1)
        prev_final_update_id = -1 if event is None else int(event.get("pu", -1))
        bids, asks = book.topn(self.config.topn_depth)

        row: dict[str, Any] = {
            "symbol": symbol,
            "tradeDate": make_trade_date(event_ts),
            "eventTime": event_ts,
            "transactionTime": transaction_ts,
            "recvTime": recv_ts,
            "firstUpdateId": first_update_id,
            "finalUpdateId": final_update_id,
            "prevFinalUpdateId": prev_final_update_id,
            "bidLevels": len(book.bids),
            "askLevels": len(book.asks),
            "isSnapshot": event is None,
        }

        for index in range(self.config.topn_depth):
            bid = bids[index] if index < len(bids) else (None, None)
            ask = asks[index] if index < len(asks) else (None, None)
            row[f"bidPx{index + 1}"] = None if bid[0] is None else float(bid[0])
            row[f"bidQty{index + 1}"] = None if bid[1] is None else float(bid[1])
            row[f"askPx{index + 1}"] = None if ask[0] is None else float(ask[0])
            row[f"askQty{index + 1}"] = None if ask[1] is None else float(ask[1])

        self.topn_rows.append(row)

    @staticmethod
    def _depth_row(
        symbol: str,
        event_ts: pd.Timestamp,
        transaction_ts: pd.Timestamp,
        recv_ts: pd.Timestamp,
        first_update_id: int,
        final_update_id: int,
        prev_final_update_id: int,
        side: str,
        price: str,
        quantity: str,
        is_snapshot: bool,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "tradeDate": make_trade_date(event_ts),
            "eventTime": event_ts,
            "transactionTime": transaction_ts,
            "recvTime": recv_ts,
            "firstUpdateId": first_update_id,
            "finalUpdateId": final_update_id,
            "prevFinalUpdateId": prev_final_update_id,
            "side": side,
            "price": float(price),
            "quantity": float(quantity),
            "isSnapshot": is_snapshot,
        }

    def flush_if_needed(self, force: bool = False) -> None:
        should_flush = (
            force
            or len(self.depth_rows) >= self.config.batch_size
            or len(self.topn_rows) >= self.config.batch_size
            or time.monotonic() - self.last_flush_at >= self.config.flush_interval
        )
        if not should_flush:
            return

        if self.depth_rows:
            self.depth_appender.append(pd.DataFrame(self.depth_rows))
            self.depth_rows.clear()
        if self.topn_rows:
            self.topn_appender.append(pd.DataFrame(self.topn_rows))
            self.topn_rows.clear()
        self.last_flush_at = time.monotonic()


def create_database_and_tables(session: ddb.Session, config: DolphinDBConfig) -> None:
    """Create a TSDB database and two partitioned tables used by this collector."""
    topn_cols = []
    topn_types = []
    for index in range(1, config.topn_depth + 1):
        topn_cols.extend([f"bidPx{index}", f"bidQty{index}", f"askPx{index}", f"askQty{index}"])
        topn_types.extend(["DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE"])

    topn_col_text = "`" + "`".join(
        [
            "symbol",
            "tradeDate",
            "eventTime",
            "transactionTime",
            "recvTime",
            "firstUpdateId",
            "finalUpdateId",
            "prevFinalUpdateId",
            "bidLevels",
            "askLevels",
            "isSnapshot",
            *topn_cols,
        ]
    )
    topn_type_text = ", ".join(
        [
            "SYMBOL",
            "DATE",
            "TIMESTAMP",
            "TIMESTAMP",
            "TIMESTAMP",
            "LONG",
            "LONG",
            "LONG",
            "INT",
            "INT",
            "BOOL",
            *topn_types,
        ]
    )

    script = f"""
dbPath = "{ddb_quote(config.db_path)}"

if(!existsDatabase(dbPath)){{
    // TSDB 必须在 database 上显式指定 engine="TSDB"。
    // 这里用 日期 VALUE 分区 + symbol HASH 分区，适合单机社区版按天/交易对查询。
    dateDim = database("", VALUE, {ddb_date(config.date_start)}..{ddb_date(config.date_end)})
    symbolDim = database("", HASH, [SYMBOL, 16])
    database(dbPath, COMPO, [dateDim, symbolDim], engine="TSDB")
}}

db = database(dbPath)

if(!existsTable(dbPath, "{ddb_quote(config.depth_update_table)}")){{
    depthSchema = table(
        1:0,
        `symbol`tradeDate`eventTime`transactionTime`recvTime`firstUpdateId`finalUpdateId`prevFinalUpdateId`side`price`quantity`isSnapshot,
        [SYMBOL, DATE, TIMESTAMP, TIMESTAMP, TIMESTAMP, LONG, LONG, LONG, SYMBOL, DOUBLE, DOUBLE, BOOL]
    )
    createPartitionedTable(
        dbHandle=db,
        table=depthSchema,
        tableName="{ddb_quote(config.depth_update_table)}",
        partitionColumns=`tradeDate`symbol,
        // TSDB 要求最后一个 sort column 必须是整型或时间类型，不能把 SYMBOL 类型的 side 放最后。
        sortColumns=`symbol`eventTime`side`finalUpdateId,
        keepDuplicates=ALL
    )
}}

if(!existsTable(dbPath, "{ddb_quote(config.topn_table)}")){{
    topnSchema = table(
        1:0,
        {topn_col_text},
        [{topn_type_text}]
    )
    createPartitionedTable(
        dbHandle=db,
        table=topnSchema,
        tableName="{ddb_quote(config.topn_table)}",
        partitionColumns=`tradeDate`symbol,
        sortColumns=`symbol`eventTime`finalUpdateId,
        keepDuplicates=ALL
    )
}}
"""
    session.run(script)


def fetch_snapshot(
    config: BinanceConfig,
    symbol: str,
    timeout: float,
) -> tuple[dict[str, Any], pd.Timestamp]:
    """Fetch one REST depth snapshot. Binance futures snapshot limit is normally up to 1000."""
    request = urllib.request.Request(
        config.snapshot_url(symbol),
        headers={"User-Agent": "ddb-orderbook-collector/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload, unix_ms_to_timestamp(now_ms())


async def ws_reader(ws: Any, queue: asyncio.Queue[tuple[dict[str, Any], pd.Timestamp]]) -> None:
    """Read WebSocket messages continuously so no updates are lost while REST snapshot is fetched."""
    async for message in ws:
        recv_ts = unix_ms_to_timestamp(now_ms())
        payload = json.loads(message)
        event = payload.get("data", payload)
        if event.get("e") == "depthUpdate":
            await queue.put((event, recv_ts))


def is_first_event_after_snapshot(event: dict[str, Any], last_update_id: int) -> bool:
    """Binance rule: first usable event must bridge local lastUpdateId."""
    first_update_id = int(event["U"])
    final_update_id = int(event["u"])
    return first_update_id <= last_update_id and final_update_id > last_update_id


def assert_next_event_is_continuous(event: dict[str, Any], expected_previous_u: int) -> None:
    """For futures depth streams, `pu` must equal the previous processed event's `u`."""
    if "pu" in event:
        previous_u = int(event["pu"])
        if previous_u != expected_previous_u:
            raise OrderBookResyncRequired(
                f"Depth gap detected: event pu={previous_u}, local lastUpdateId={expected_previous_u}"
            )
        return

    first_update_id = int(event["U"])
    final_update_id = int(event["u"])
    if not (first_update_id <= expected_previous_u + 1 <= final_update_id):
        raise OrderBookResyncRequired(
            "Depth gap detected: "
            f"event U={first_update_id}, u={final_update_id}, local lastUpdateId={expected_previous_u}"
        )


async def initialize_book_from_stream(
    config: BinanceConfig,
    writer: DolphinDBWriter,
    snapshot_timeout: float,
) -> tuple[
    Any,
    asyncio.Task[None],
    asyncio.Queue[tuple[dict[str, Any], pd.Timestamp]],
    dict[str, LocalOrderBook],
]:
    """Open WS first, buffer events, fetch REST snapshots, then initialize each symbol."""
    ws = await websockets.connect(
        config.ws_url,
        ping_interval=20,
        ping_timeout=20,
        max_queue=None,
    )
    queue: asyncio.Queue[tuple[dict[str, Any], pd.Timestamp]] = asyncio.Queue()
    reader_task = asyncio.create_task(ws_reader(ws, queue))

    loop = asyncio.get_running_loop()

    books: dict[str, LocalOrderBook] = {}
    pending_symbols = set(config.normalized_symbols)
    for symbol in config.normalized_symbols:
        snapshot, snapshot_recv_ts = await loop.run_in_executor(
            None,
            fetch_snapshot,
            config,
            symbol,
            snapshot_timeout,
        )

        book = LocalOrderBook(symbol)
        book.load_snapshot(snapshot)
        writer.add_snapshot_rows(symbol, snapshot, snapshot_recv_ts)
        writer.add_topn_row(symbol, None, book, snapshot_recv_ts)
        books[symbol] = book

        print(
            f"Loaded snapshot: symbol={symbol}, "
            f"lastUpdateId={book.last_update_id}, bids={len(book.bids)}, asks={len(book.asks)}"
        )

    # 按 Binance futures 手册：丢弃 u < lastUpdateId 的过期事件；
    # 第一个可应用事件必须满足 U <= lastUpdateId 且 u > lastUpdateId。
    while pending_symbols:
        event, recv_ts = await queue.get()
        symbol = str(event["s"]).upper()
        if symbol not in pending_symbols:
            continue

        book = books[symbol]
        last_update_id = int(book.last_update_id or -1)
        if int(event["u"]) < last_update_id:
            continue
        if not is_first_event_after_snapshot(event, last_update_id):
            raise OrderBookResyncRequired(
                "First event does not bridge snapshot: "
                f"U={event['U']}, u={event['u']}, lastUpdateId={last_update_id}"
            )

        book.apply_event(event)
        writer.add_event_rows(symbol, event, recv_ts)
        writer.add_topn_row(symbol, event, book, recv_ts)
        pending_symbols.remove(symbol)
        print(f"First diff applied: symbol={symbol}, u={book.last_update_id}")

    writer.flush_if_needed(force=True)
    return ws, reader_task, queue, books


async def collect_forever(
    binance_config: BinanceConfig,
    ddb_config: DolphinDBConfig,
    snapshot_timeout: float,
    reconnect_delay: float,
) -> None:
    session = ddb.Session()
    session.connect(ddb_config.host, ddb_config.port, ddb_config.user, ddb_config.password)
    writer: DolphinDBWriter | None = None
    try:
        create_database_and_tables(session, ddb_config)
        writer = DolphinDBWriter(session, ddb_config)

        while True:
            ws = None
            reader_task: asyncio.Task[None] | None = None
            try:
                ws, reader_task, queue, books = await initialize_book_from_stream(
                    binance_config,
                    writer,
                    snapshot_timeout,
                )

                while True:
                    event, recv_ts = await queue.get()
                    symbol = str(event["s"]).upper()
                    book = books.get(symbol)
                    if book is None:
                        continue

                    local_update_id = int(book.last_update_id or -1)
                    if int(event["u"]) <= local_update_id:
                        continue
                    assert_next_event_is_continuous(event, local_update_id)

                    book.apply_event(event)
                    writer.add_event_rows(symbol, event, recv_ts)
                    writer.add_topn_row(symbol, event, book, recv_ts)
                    writer.flush_if_needed()

            except OrderBookResyncRequired as exc:
                writer.flush_if_needed(force=True)
                print(f"{exc}. Rebuilding local order book after {reconnect_delay}s ...")
            except Exception as exc:
                writer.flush_if_needed(force=True)
                print(f"Collector error: {type(exc).__name__}: {exc}. Reconnecting after {reconnect_delay}s ...")
            finally:
                if reader_task is not None:
                    reader_task.cancel()
                if ws is not None:
                    await ws.close()
                await asyncio.sleep(reconnect_delay)
    except asyncio.CancelledError:
        # Ctrl+C 会取消 asyncio 主任务；退出前尽量把缓存中的盘口数据写入 DolphinDB。
        if writer is not None:
            writer.flush_if_needed(force=True)
        raise
    finally:
        if writer is not None:
            writer.flush_if_needed(force=True)
        session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Maintain a Binance USD-M futures local order book and write depth data to DolphinDB TSDB."
    )
    parser.add_argument(
        "--symbols",
        default="BTCUSDT",
        help="Comma-separated futures symbols, e.g. BTCUSDT,ETHUSDT.",
    )
    parser.add_argument("--ws-base", default="wss://fstream.binance.com")
    parser.add_argument("--rest-base", default="https://fapi.binance.com")
    parser.add_argument("--snapshot-limit", type=int, default=1000)
    parser.add_argument("--update-speed", default="100ms", choices=["100ms", "250ms", "500ms"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8848)
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="123456")
    parser.add_argument("--db-path", default=DB_PATH)
    parser.add_argument("--depth-update-table", default=DEPTH_UPDATE_TABLE)
    parser.add_argument("--topn-table", default=TOPN_TABLE)
    parser.add_argument("--date-start", default="2026-01-01")
    parser.add_argument("--date-end", default="2035-12-31")
    parser.add_argument("--topn-depth", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--flush-interval", type=float, default=0.5)
    parser.add_argument("--snapshot-timeout", type=float, default=10.0)
    parser.add_argument("--reconnect-delay", type=float, default=3.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.snapshot_limit > 1000:
        raise SystemExit("USD-M futures /fapi/v1/depth snapshot limit should not exceed 1000.")
    if args.topn_depth <= 0:
        raise SystemExit("--topn-depth must be positive.")

    symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    if not symbols:
        raise SystemExit("--symbols must contain at least one symbol.")

    binance_config = BinanceConfig(
        symbols=symbols,
        ws_base=args.ws_base,
        rest_base=args.rest_base,
        snapshot_limit=args.snapshot_limit,
        update_speed=args.update_speed,
    )
    ddb_config = DolphinDBConfig(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        db_path=args.db_path,
        depth_update_table=args.depth_update_table,
        topn_table=args.topn_table,
        date_start=args.date_start,
        date_end=args.date_end,
        topn_depth=args.topn_depth,
        batch_size=args.batch_size,
        flush_interval=args.flush_interval,
    )

    print(f"Binance WS: {binance_config.ws_url}")
    for symbol in binance_config.normalized_symbols:
        print(f"Binance REST snapshot [{symbol}]: {binance_config.snapshot_url(symbol)}")
    print(f"DolphinDB: {ddb_config.host}:{ddb_config.port}, db={ddb_config.db_path}")
    try:
        asyncio.run(
            collect_forever(
                binance_config=binance_config,
                ddb_config=ddb_config,
                snapshot_timeout=args.snapshot_timeout,
                reconnect_delay=args.reconnect_delay,
            )
        )
    except KeyboardInterrupt:
        print("停止调试，退出DolphinDB。Manual stop received. Collector exited cleanly.")


if __name__ == "__main__":
    main()
