"""
行情数据处理模块：处理深度行情数据回调
"""
import logging
from typing import Optional

from DataBuffer import L2Book, DataBuffer, price_board
from models import OrderBookSnapshot
from storage.orderbook_writer import DolphinDBWriter
from websocket_cex_dex import HyperliquidWebSocket
import config as cfg


class Depth_Marketdata:
    """深度行情数据处理类"""

    writer: Optional[DolphinDBWriter] = None

    @classmethod
    def set_writer(cls, writer: Optional[DolphinDBWriter]) -> None:
        """
        注入订单簿写入器。

        这样 WebSocket 回调就不需要自己关心 DolphinDB 细节，
        只要统一把标准化快照分发给 writer 即可。
        """
        cls.writer = writer

    @classmethod
    def _build_snapshot(cls, exchange: str, book: L2Book) -> OrderBookSnapshot:
        """
        把现有的 `L2Book` 转换成统一的 `OrderBookSnapshot`。
        """
        return OrderBookSnapshot.from_l2book(
            exchange=exchange,
            symbol=book.coin,
            book=book,
            source="websocket",
        )

    @classmethod
    def _dispatch_snapshot(cls, snapshot: OrderBookSnapshot) -> None:
        """
        统一分发标准化后的订单簿快照。

        当前阶段先做一件事：
        - 如果已配置 writer，则异步放入待写库队列

        这样后续如果要继续扩展：
        - feature engine
        - 实时监控
        - 数据质量检测
        都可以继续在这个分发点扩展。
        """
        if cls.writer is None:
            return

        try:
            cls.writer.enqueue(snapshot)
        except Exception as exc:
            logging.exception("[Depth_Marketdata] 订单簿写入队列失败: %s", exc)
    
    @classmethod
    def _handle_orderbook(cls, exchange: str, book: L2Book, calculate_signal: bool) -> None:
        """
        统一处理来自不同交易所的订单簿数据。

        当前统一入口负责：
        1. 把 `L2Book` 转换为 `OrderBookSnapshot`
        2. 分发给 DolphinDB writer
        3. 更新实时价格板
        4. 对需要驱动策略的交易所执行价差与信号检查

        参数：
        - `exchange`: 交易所名称
        - `book`: 已经标准化后的订单簿对象
        - `calculate_signal`: 是否在本次回调后触发价差和策略检查
        """
        snapshot = cls._build_snapshot(exchange, book)
        cls._dispatch_snapshot(snapshot)

        DataBuffer.push_neworder_book(exchange, book)

        if calculate_signal and book.bids and book.asks:
            spread_buy_bin, spread_buy_hyp = price_board.get_spread()
            
            if spread_buy_bin is not None and spread_buy_hyp is not None:
                # 价差计算成功，可以触发交易信号
                logging.info(f"[价差] Binance买->Hyper卖: {spread_buy_bin:.6f}, Hyper买->Binance卖: {spread_buy_hyp:.6f}")
                
                # 触发策略信号检查（使用延迟导入避免循环依赖）
                try:
                    from Simple_strategy import StrategyState
                    import sys
                    # 获取正在运行的脚本模块（而不是导入的 main 模块）
                    main_module = sys.modules.get('__main__')
                    
                    # 获取全局策略状态机实例
                    strategy_machine = getattr(main_module, 'strategy_machine', None) if main_module else None
                    
                    if strategy_machine is None:
                        logging.debug("[Depth_Marketdata] 策略状态机未初始化，跳过信号检查")
                        return
                    
                    current_state = strategy_machine.get_state()
                    
                    # 检查开仓信号
                    if current_state == StrategyState.OpenCondition:
                        if DataBuffer.open_signal(cfg.HYPER_SYMBOL):
                            strategy_machine.check_and_execute_open(
                                lambda: DataBuffer.open_signal(cfg.HYPER_SYMBOL)
                            )
                    
                    # 检查平仓信号
                    elif current_state == StrategyState.CloseCondition:
                        if DataBuffer.close_signal(cfg.HYPER_SYMBOL):
                            strategy_machine.check_and_execute_close(
                                lambda: DataBuffer.close_signal(cfg.HYPER_SYMBOL)
                            )
                except (ImportError, AttributeError) as e:
                    # 如果模块未找到或属性不存在，记录警告但不中断运行
                    logging.debug(f"[Depth_Marketdata] 策略信号检查跳过: {e}")
            else:
                logging.debug("[价差] 数据不足或过期，跳过计算")

        print(
            f"[{exchange.upper()}] 价格更新: "
            f"bid={book.bids[0].price if book.bids else 0:.6f}, "
            f"ask={book.asks[0].price if book.asks else 0:.6f}"
        )

    @classmethod
    def on_hyperliquid_l2book(cls, book: L2Book):
        """Hyperliquid订单簿回调。"""
        cls._handle_orderbook("Hyperliquid", book, calculate_signal=False)
    
    @classmethod
    def on_binance_l2book(cls, book: L2Book):
        """Binance订单簿回调。"""
        cls._handle_orderbook("Binance", book, calculate_signal=True)
    
    @classmethod
    def on_hyperliquid_raw(cls, raw_data):
        """接收Hyperliquid原始数据 → 解析 → 结构化处理"""
        book = HyperliquidWebSocket.parse_l2book(raw_data, depth=5)
        cls._handle_orderbook("Hyperliquid", book, calculate_signal=False)
    
    @classmethod
    def on_trades(cls, data):
        """处理交易数据"""
        for trade in data.get("data", []):
            side = "buy" if trade["side"] == "B" else "sell"
            logging.info(f"[TRADE] {trade['coin']} {side} {trade['sz']} @ {trade['price']} (t={trade['time']})")
