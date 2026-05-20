"""
统一市场数据模型。

这个文件的目标是把不同交易所、不同时点、不同原始格式的订单簿数据，
统一整理成同一种 Python 数据结构，方便后续做三件事：

1. 实时策略读取
2. DolphinDB 落库
3. 后续特征工程与机器学习训练

当前项目里 Binance 和 Hyperliquid 的订单簿来源不同，字段命名也不同。
如果后续继续直接在各个回调里分别处理，会导致：

1. 存储逻辑和交易逻辑耦合
2. 后面新增交易所时重复写解析代码
3. 训练数据字段不统一，难以做特征工程

因此这里抽象出统一的两层模型：

1. `BookLevel`
   表示订单簿的一档盘口
2. `OrderBookSnapshot`
   表示某个时刻某个交易所在某个交易对上的完整 L2 快照
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional
import time


DEFAULT_BOOK_DEPTH = 5


@dataclass(frozen=True)
class BookLevel:
    """
    表示订单簿中的单档盘口。

    字段说明：
    - `price`: 该档位的价格
    - `size`: 该档位的挂单数量
    - `orders`: 该档位包含的订单数

    说明：
    - 某些交易所会直接提供订单数，例如 Hyperliquid 的 `n`
    - 某些交易所不会提供订单数，例如 Binance 深度流，这时通常记为 0 或默认值
    """

    price: float
    size: float
    orders: int = 0

    @classmethod
    def from_any(cls, level: Any) -> "BookLevel":
        """
        把不同格式的“单档盘口”统一转换成 `BookLevel`。

        支持的输入格式：
        1. 现有项目里的 `DataBuffer.Level(price, size, orders)`
        2. 字典类型，例如：
           `{"price": 100, "size": 1}`
           或
           `{"px": 100, "sz": 1, "n": 3}`
        3. 列表/元组类型，例如：
           `[price, size]`
           或
           `[price, size, orders]`

        这么设计的原因是：
        - 你当前代码里已经有 `Level` 这种结构
        - 不同交易所的原始 WebSocket 消息字段不一样
        - 后续落库和特征工程只想面对统一对象，不想每次判断来源

        参数：
        - `level`: 任意一种可识别的盘口档位数据

        返回：
        - 标准化后的 `BookLevel`

        异常：
        - 当输入格式无法识别时，抛出 `TypeError`
        """
        if hasattr(level, "price") and hasattr(level, "size"):
            return cls(
                price=float(level.price),
                size=float(level.size),
                orders=int(getattr(level, "orders", 0) or 0),
            )

        if isinstance(level, dict):
            return cls(
                price=float(level.get("price", level.get("px", 0.0))),
                size=float(level.get("size", level.get("sz", 0.0))),
                orders=int(level.get("orders", level.get("n", 0)) or 0),
            )

        if isinstance(level, (list, tuple)) and len(level) >= 2:
            orders = level[2] if len(level) >= 3 else 0
            return cls(price=float(level[0]), size=float(level[1]), orders=int(orders or 0))

        raise TypeError(f"Unsupported level payload: {type(level)!r}")


@dataclass
class OrderBookSnapshot:
    """
    统一的 L2 订单簿快照对象。

    这是后续“实时缓存、数据库落库、特征计算”的核心中间层对象。
    无论数据来自 Binance 还是 Hyperliquid，只要先转成这个对象，
    后面的处理逻辑就可以统一。

    字段说明：
    - `exchange`:
      交易所名称，例如 `"Binance"`、`"Hyperliquid"`
    - `symbol`:
      交易对名称，例如 `"BTCUSDT"` 或 `"BTC"`
    - `event_time`:
      交易所原始消息携带的事件时间
      如果上游提供的是毫秒时间戳，会在这里转成 UTC 的 `datetime`
    - `receive_time`:
      本地程序收到并完成标准化的时间
      这个字段很重要，后面可以用来分析采集延迟、网络延迟、写库延迟
    - `bids`:
      买盘列表，约定按价格从高到低排序
    - `asks`:
      卖盘列表，约定按价格从低到高排序
    - `source`:
      数据来源描述，默认是 `"websocket"`
      后续如果补 REST 快照、回放文件、重建簿，都可以用这个字段区分
    - `sequence_id`:
      可选的序列号或更新编号
      某些交易所会提供 update id，这个字段预留给后续做数据校验和重建簿
    - `is_snapshot`:
      是否为完整快照
      目前你的项目里主要处理的是可直接使用的订单簿快照，因此默认值为 `True`
    - `raw_timestamp_ms`:
      保留原始毫秒时间戳，方便直接落库或排查问题

    约定：
    - `bids` 应从高价到低价
    - `asks` 应从低价到高价
    - 如果收到的原始数据顺序不可靠，会在初始化后自动排序
    """

    exchange: str
    symbol: str
    event_time: datetime
    receive_time: datetime
    bids: List[BookLevel] = field(default_factory=list)
    asks: List[BookLevel] = field(default_factory=list)
    source: str = "websocket"
    sequence_id: Optional[str] = None
    is_snapshot: bool = True
    raw_timestamp_ms: Optional[int] = None

    def __post_init__(self) -> None:
        """
        dataclass 初始化完成后的标准化入口。

        这里做三件事：
        1. 保证 `exchange` 和 `symbol` 一定是字符串
        2. 把 `bids` / `asks` 中的元素统一转换为 `BookLevel`
        3. 对买卖盘重新排序，保证后续计算稳定
        """
        self.exchange = str(self.exchange)
        self.symbol = str(self.symbol)
        self.bids = [BookLevel.from_any(level) for level in self.bids]
        self.asks = [BookLevel.from_any(level) for level in self.asks]
        self._sort_levels()

    def _sort_levels(self) -> None:
        """
        对订单簿档位进行标准排序。

        排序规则：
        - 买盘 `bids`: 按价格从高到低
        - 卖盘 `asks`: 按价格从低到高

        这样可以保证：
        - `bids[0]` 一定是买一
        - `asks[0]` 一定是卖一
        """
        self.bids.sort(key=lambda item: item.price, reverse=True)
        self.asks.sort(key=lambda item: item.price)

    @property
    def bid1(self) -> Optional[BookLevel]:
        """返回买一档。如果买盘为空，则返回 `None`。"""
        return self.bids[0] if self.bids else None

    @property
    def ask1(self) -> Optional[BookLevel]:
        """返回卖一档。如果卖盘为空，则返回 `None`。"""
        return self.asks[0] if self.asks else None

    def mid_price(self) -> Optional[float]:
        """
        计算中间价。

        公式：
        `(买一价 + 卖一价) / 2`

        返回：
        - 如果买一和卖一都存在，返回浮点数
        - 如果订单簿不完整，返回 `None`
        """
        if not self.bid1 or not self.ask1:
            return None
        return (self.bid1.price + self.ask1.price) / 2.0

    def spread(self) -> Optional[float]:
        """
        计算最优买卖价差。

        公式：
        `卖一价 - 买一价`

        返回：
        - 如果买一和卖一都存在，返回浮点数
        - 如果订单簿不完整，返回 `None`
        """
        if not self.bid1 or not self.ask1:
            return None
        return self.ask1.price - self.bid1.price

    def depth(self, side: str, levels: int = DEFAULT_BOOK_DEPTH) -> float:
        """
        计算某一侧前 N 档的累计挂单量。

        参数：
        - `side`: `"bid"` 表示买盘，其他值按卖盘处理
        - `levels`: 统计前多少档

        返回：
        - 前 N 档的 `size` 总和

        这个函数后续可以直接用于构造一些很常见的盘口特征，
        比如 depth imbalance、top5 depth、top10 depth 等。
        """
        book_side = self.bids if side.lower() == "bid" else self.asks
        return sum(level.size for level in book_side[:levels])

    def top_n(self, depth: int = DEFAULT_BOOK_DEPTH) -> "OrderBookSnapshot":
        """
        返回一个只保留前 N 档盘口的新快照对象。

        为什么不直接在原对象上截断：
        - 原对象可能还要继续给别的逻辑使用
        - 落库通常只需要前 5 档或前 10 档
        - 保持原始对象完整，更便于调试和后续扩展

        参数：
        - `depth`: 保留的盘口档数

        返回：
        - 一个新的 `OrderBookSnapshot`
        """
        return OrderBookSnapshot(
            exchange=self.exchange,
            symbol=self.symbol,
            event_time=self.event_time,
            receive_time=self.receive_time,
            bids=self.bids[:depth],
            asks=self.asks[:depth],
            source=self.source,
            sequence_id=self.sequence_id,
            is_snapshot=self.is_snapshot,
            raw_timestamp_ms=self.raw_timestamp_ms,
        )

    def to_storage_dict(self, depth: int = DEFAULT_BOOK_DEPTH) -> Dict[str, Any]:
        """
        将订单簿快照转换为适合 DolphinDB 宽表写入的字典结构。

        这是后续写库最重要的接口之一。

        输出字段分为几类：
        1. 元信息字段
           - `exchange`
           - `symbol`
           - `event_time`
           - `receive_time`
           - `source`
           - `sequence_id`
           - `is_snapshot`
           - `raw_timestamp_ms`
        2. 衍生指标字段
           - `mid_price`
           - `spread`
        3. 宽表盘口字段
           - `bid_px_1 ~ bid_px_N`
           - `bid_sz_1 ~ bid_sz_N`
           - `bid_orders_1 ~ bid_orders_N`
           - `ask_px_1 ~ ask_px_N`
           - `ask_sz_1 ~ ask_sz_N`
           - `ask_orders_1 ~ ask_orders_N`

        之所以选择宽表而不是“一档一行”的长表，是因为：
        - 你后续要做特征工程和监督学习
        - 宽表更适合直接构造 snapshot 特征
        - DolphinDB 批量 append 宽表也比较直接

        参数：
        - `depth`: 输出前多少档盘口

        返回：
        - 一个可以直接交给 writer 的字典
        """
        snapshot = self.top_n(depth)
        row: Dict[str, Any] = {
            "exchange": snapshot.exchange,
            "symbol": snapshot.symbol,
            "event_date": snapshot.event_time.astimezone(timezone.utc).date(),
            "event_time": snapshot.event_time,
            "receive_time": snapshot.receive_time,
            "source": snapshot.source,
            "sequence_id": snapshot.sequence_id or "",
            "is_snapshot": snapshot.is_snapshot,
            "raw_timestamp_ms": snapshot.raw_timestamp_ms if snapshot.raw_timestamp_ms is not None else -1,
            "mid_price": snapshot.mid_price(),
            "spread": snapshot.spread(),
        }

        for index in range(depth):
            bid = snapshot.bids[index] if index < len(snapshot.bids) else None
            ask = snapshot.asks[index] if index < len(snapshot.asks) else None
            level_no = index + 1

            row[f"bid_px_{level_no}"] = bid.price if bid else None
            row[f"bid_sz_{level_no}"] = bid.size if bid else None
            row[f"bid_orders_{level_no}"] = bid.orders if bid else None

            row[f"ask_px_{level_no}"] = ask.price if ask else None
            row[f"ask_sz_{level_no}"] = ask.size if ask else None
            row[f"ask_orders_{level_no}"] = ask.orders if ask else None

        return row

    @classmethod
    def from_l2book(
        cls,
        exchange: str,
        symbol: Optional[str],
        book: Any,
        source: str = "websocket",
        receive_time: Optional[datetime] = None,
        sequence_id: Optional[str] = None,
        is_snapshot: bool = True,
    ) -> "OrderBookSnapshot":
        """
        从当前项目里已有的 `DataBuffer.L2Book` 构造统一快照对象。

        这个方法是为了平滑接入你当前的代码结构。
        你现在的 WebSocket 回调已经会先解析成 `L2Book`，
        所以后面只要在回调里调用这个方法，就能把旧结构无缝转成新结构。

        参数：
        - `exchange`: 交易所名称
        - `symbol`: 交易对名称；如果不传，会尝试从 `book.coin` 读取
        - `book`: 现有的 `L2Book` 对象
        - `source`: 数据来源，默认 `"websocket"`
        - `receive_time`: 本地接收时间；如果不传，使用当前 UTC 时间
        - `sequence_id`: 可选序列号
        - `is_snapshot`: 是否视为快照

        返回：
        - 标准化后的 `OrderBookSnapshot`
        """
        raw_timestamp_ms = getattr(book, "timestamp", None)
        event_time = _to_datetime(raw_timestamp_ms)
        receive_dt = receive_time or datetime.now(timezone.utc)

        resolved_symbol = symbol or getattr(book, "coin", "")
        bids = [BookLevel.from_any(level) for level in getattr(book, "bids", [])]
        asks = [BookLevel.from_any(level) for level in getattr(book, "asks", [])]

        return cls(
            exchange=exchange,
            symbol=resolved_symbol,
            event_time=event_time,
            receive_time=receive_dt,
            bids=bids,
            asks=asks,
            source=source,
            sequence_id=sequence_id,
            is_snapshot=is_snapshot,
            raw_timestamp_ms=raw_timestamp_ms,
        )

    @classmethod
    def from_raw_levels(
        cls,
        exchange: str,
        symbol: str,
        bids: List[Any],
        asks: List[Any],
        event_timestamp_ms: Optional[int] = None,
        receive_timestamp_ms: Optional[int] = None,
        source: str = "websocket",
        sequence_id: Optional[str] = None,
        is_snapshot: bool = True,
    ) -> "OrderBookSnapshot":
        """
        直接从原始买卖盘列表构造统一快照。

        适用场景：
        - 某些回调还没有先转成 `L2Book`
        - 将来新增交易所时，想直接从原始字段构建统一对象
        - 回测/重放时从本地文件读到原始盘口数组

        参数：
        - `exchange`: 交易所名称
        - `symbol`: 交易对
        - `bids`: 买盘数组
        - `asks`: 卖盘数组
        - `event_timestamp_ms`: 交易所事件时间（毫秒）
        - `receive_timestamp_ms`: 本地接收时间（毫秒）
        - `source`: 数据来源标签
        - `sequence_id`: 可选序列号
        - `is_snapshot`: 是否为快照

        返回：
        - 标准化后的 `OrderBookSnapshot`
        """
        return cls(
            exchange=exchange,
            symbol=symbol,
            event_time=_to_datetime(event_timestamp_ms),
            receive_time=_to_datetime(receive_timestamp_ms, default_now=True),
            bids=[BookLevel.from_any(level) for level in bids],
            asks=[BookLevel.from_any(level) for level in asks],
            source=source,
            sequence_id=sequence_id,
            is_snapshot=is_snapshot,
            raw_timestamp_ms=event_timestamp_ms,
        )


def _to_datetime(timestamp_ms: Optional[int], default_now: bool = False) -> datetime:
    """
    把毫秒时间戳转换为 UTC `datetime`。

    参数：
    - `timestamp_ms`: 毫秒时间戳
    - `default_now`:
      当时间戳为空或为 0 时，是否回退到“当前时间”

    返回规则：
    - 如果 `timestamp_ms` 有效，返回对应 UTC 时间
    - 如果为空且 `default_now=True`，返回当前 UTC 时间
    - 如果为空且 `default_now=False`，返回 Unix epoch 起点时间

    这里保留 epoch 起点而不是直接返回 `None`，是为了减少后续
    存储层和计算层的空值判断复杂度。
    """
    if timestamp_ms in (None, 0):
        return datetime.now(timezone.utc) if default_now else datetime.fromtimestamp(0, tz=timezone.utc)
    return datetime.fromtimestamp(float(timestamp_ms) / 1000.0, tz=timezone.utc)


def utc_now_ms() -> int:
    """
    返回当前 UTC 时间对应的毫秒时间戳。

    这个函数主要给以下场景使用：
    - 补充本地接收时间
    - 生成写库时间
    - 生成无交易所时间戳时的默认事件时间
    """
    return int(time.time() * 1000)
