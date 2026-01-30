"""
数据缓冲模块：定义订单簿数据结构和价格板
"""
import threading
import time
from typing import List, NamedTuple, Tuple, Optional
from dataclasses import dataclass


# ==================== 数据结构 ====================
class Level(NamedTuple):
    price: float
    size: float
    orders: int  # n: 订单数量


class L2Book:
    def __init__(self, coin: str, bids: List[Level], asks: List[Level], timestamp: int):
        self.coin = coin
        self.bids = bids
        self.asks = asks
        self.timestamp = timestamp

    def mid_price(self) -> float:
        """中间价"""
        if self.bids and self.asks:
            return (self.bids[0].price + self.asks[0].price) / 2
        return 0.0

    def spread(self) -> float:
        """买卖价差"""
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return 0.0

    def depth(self, side: str, levels: int = 5) -> float:
        """累计深度（前 N 档总挂单量）"""
        side_list = self.bids if side.lower() == "buy" else self.asks
        return sum(l.size for l in side_list[:levels])

    def __str__(self):
        return (f"L2Book[{self.coin}] mid={self.mid_price():.6f} "
                f"spread={self.spread():.6f} "
                f"bid_depth5={self.depth('buy', 5):.1f} "
                f"ask_depth5={self.depth('ask', 5):.1f}")


# ==================== 数据存储 ====================
# 实时交易系统只需要最新价格，不需要存储历史数据
# 如需历史数据，可以使用轻量级的 collections.deque 实现固定大小的循环缓冲区


# ==================== 轻量级价差计算容器 ====================
@dataclass
class Ticker:
    """单个交易所的价格快照"""
    bid_price: float = 0.0
    ask_price: float = 0.0
    timestamp: float = 0.0


class PriceBoard:
    """轻量级价格板，用于实时价差计算"""
    def __init__(self):
        self.lock = threading.Lock()  # 线程锁
        self.prices = {
            "Binance": Ticker(),
            "Hyperliquid": Ticker()
        }
        self.max_delay_seconds = 1.0
        # 手续费配置（从 config 导入，避免循环依赖）
        try:
            import config as cfg
            self.binance_maker_fee = cfg.BINANCE_MAKER_FEE
            self.binance_taker_fee = cfg.BINANCE_TAKER_FEE
            self.hyper_maker_fee = cfg.HYPER_MAKER_FEE
            self.hyper_taker_fee = cfg.HYPER_TAKER_FEE
        except (ImportError, AttributeError):
            # 默认值（如果 config 未配置）
            self.binance_maker_fee = 0.0002
            self.binance_taker_fee = 0.0004
            self.hyper_maker_fee = 0.0002
            self.hyper_taker_fee = 0.0004

    def update(self, exchange: str, bid: float, ask: float):
        """收到WebSocket推送时更新（静默更新，不计算）"""
        with self.lock:  # 写操作加锁
            ticker = self.prices[exchange]
            ticker.bid_price = bid
            ticker.ask_price = ask
            ticker.timestamp = time.time()

    def get_price(self, exchange: str, side: str) -> Optional[float]:
        """
        获取指定交易所和方向的价格
        
        参数:
            exchange: 交易所名称 ("Binance" 或 "Hyperliquid")
            side: 方向 ("bid" 或 "ask")
        
        返回:
            价格值，如果数据无效或过期返回 None
        """
        with self.lock:
            if exchange not in self.prices:
                return None
            ticker = self.prices[exchange]
            
            now = time.time()
            # 检查数据有效性
            if ticker.bid_price == 0 or (now - ticker.timestamp) > self.max_delay_seconds:
                return None
            
            if side.lower() == "bid":
                return ticker.bid_price
            elif side.lower() == "ask":
                return ticker.ask_price
            else:
                return None

    def get_spread(self) -> Tuple[Optional[float], Optional[float]]:
        """
        获取实时价差
        
        返回:
            (spread_buy_bin, spread_buy_hyp)
            - spread_buy_bin: Binance 买 (Ask) -> Hyper 卖 (Bid) 的价差
            - spread_buy_hyp: Hyper 买 (Ask) -> Binance 卖 (Bid) 的价差
            如果数据无效或过期，返回 (None, None)
        """
        with self.lock:  # 读操作加锁，保证读取的一瞬间数据是一致的
            binance = self.prices["Binance"]
            hyper = self.prices["Hyperliquid"]
            
            now = time.time()

            # 初始化检查：防止刚启动时价格为 0
            if binance.bid_price == 0 or hyper.bid_price == 0:
                return None, None

            # 风控：过期检查
            if (now - binance.timestamp) > self.max_delay_seconds:
                return None, None
            
            if (now - hyper.timestamp) > self.max_delay_seconds:
                return None, None

            # 计算价差
            # 方向 A: Binance 买 (Ask) -> Hyper 卖 (Bid)
            spread_buy_bin = hyper.bid_price - binance.ask_price
            
            # 方向 B: Hyper 买 (Ask) -> Binance 卖 (Bid)
            spread_buy_hyp = binance.bid_price - hyper.ask_price

            return spread_buy_bin, spread_buy_hyp
    
    def get_spread_with_fees(self) -> Tuple[Optional[float], Optional[float]]:
        """
        获取考虑手续费后的净价差
        
        返回:
            (net_spread_buy_bin, net_spread_buy_hyp)
            - net_spread_buy_bin: Binance 买 (taker) -> Hyper 卖 (maker) 的净价差（扣除手续费）
            - net_spread_buy_hyp: Hyper 买 (taker) -> Binance 卖 (maker) 的净价差（扣除手续费）
            如果数据无效或过期，返回 (None, None)
        
        注意：
            - 开仓：Binance 买入（taker）+ Hyper 卖出（maker）
            - 平仓：Binance 卖出（maker）+ Hyper 买入（taker）
        """
        with self.lock:
            binance = self.prices["Binance"]
            hyper = self.prices["Hyperliquid"]
            
            now = time.time()
            
            # 数据有效性检查
            if (binance.bid_price == 0 or hyper.bid_price == 0 or
                (now - binance.timestamp) > self.max_delay_seconds or
                (now - hyper.timestamp) > self.max_delay_seconds):
                return None, None
            
            # 计算考虑手续费后的净价差
            # 方向 A: Binance 买 (taker) -> Hyper 卖 (maker)
            # 成本：Binance ask * (1 + taker_fee)
            # 收入：Hyper bid * (1 - maker_fee)
            cost_buy_bin = binance.ask_price * (1 + self.binance_taker_fee)
            revenue_sell_hyper = hyper.bid_price * (1 - self.hyper_maker_fee)
            net_spread_buy_bin = revenue_sell_hyper - cost_buy_bin
            
            # 方向 B: Hyper 买 (taker) -> Binance 卖 (maker)
            # 成本：Hyper ask * (1 + taker_fee)
            # 收入：Binance bid * (1 - maker_fee)
            cost_buy_hyper = hyper.ask_price * (1 + self.hyper_taker_fee)
            revenue_sell_bin = binance.bid_price * (1 - self.binance_maker_fee)
            net_spread_buy_hyp = revenue_sell_bin - cost_buy_hyper
            
            return net_spread_buy_bin, net_spread_buy_hyp


# 全局价格板实例
price_board = PriceBoard()


# ==================== 数据缓冲处理类 ====================
class DataBuffer:
    """数据缓冲处理类：封装开平仓信号和订单簿数据推送逻辑"""

    @classmethod
    def open_signal(cls, coin: str) -> bool:
        """
        开仓信号：当考虑手续费后的价差大于阈值时返回 True
        
        套利方向：Binance 买入（taker）+ Hyper 卖出（maker）
        只有当扣除手续费后的净价差大于最小阈值时，才触发开仓信号
        """
        net_spread_buy_bin, _ = price_board.get_spread_with_fees()
        if net_spread_buy_bin is None:
            return False
        
        # 获取最小价差阈值
        try:
            import config as cfg
            min_threshold = cfg.MIN_SPREAD_THRESHOLD
        except (ImportError, AttributeError):
            min_threshold = 0.0
        
        # 只有当净价差（扣除手续费后）大于阈值时，才触发开仓
        return net_spread_buy_bin > min_threshold

    @classmethod
    def close_signal(cls, coin: str) -> bool:
        """
        平仓信号：当考虑手续费后的价差大于阈值时返回 True
        
        平仓方向：Binance 卖出（maker）+ Hyper 买入（taker）
        只有当扣除手续费后的净价差大于最小阈值时，才触发平仓信号
        """
        _, net_spread_buy_hyp = price_board.get_spread_with_fees()
        if net_spread_buy_hyp is None:
            return False
        
        # 获取最小价差阈值
        try:
            import config as cfg
            min_threshold = cfg.MIN_SPREAD_THRESHOLD
        except (ImportError, AttributeError):
            min_threshold = 0.0
        
        # 只有当净价差（扣除手续费后）大于阈值时，才触发平仓
        return net_spread_buy_hyp > min_threshold

    @classmethod
    def push_neworder_book(cls, exchange: str, book: L2Book):
        """
        更新订单簿数据到价格板
        
        注意：不再存储历史数据，只更新实时价格板用于价差计算
        实时交易系统只需要最新价格，不需要历史数据存储
        """
        if book.bids and book.asks:
            bid1 = book.bids[0].price
            ask1 = book.asks[0].price
            price_board.update(exchange, bid1, ask1)
