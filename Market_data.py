"""
行情数据处理模块：处理深度行情数据回调
"""
import logging
from DataBuffer import L2Book, DataBuffer, price_board
from websocket_cex_dex import HyperliquidWebSocket
import config as cfg


class Depth_Marketdata:
    """深度行情数据处理类"""
    
    @classmethod
    def on_hyperliquid_l2book(cls, book: L2Book):
        """Hyperliquid订单簿回调 - 静默更新，不计算价差"""
        # 更新价格板（内部已处理）
        DataBuffer.push_neworder_book("Hyperliquid", book)
        
        # 可选：调试日志（生产环境可以关闭）
        print(f"[HYPELIQUID] 价格更新: bid={book.bids[0].price if book.bids else 0:.6f}, ask={book.asks[0].price if book.asks else 0:.6f}")
    
    @classmethod
    def on_binance_l2book(cls, book: L2Book):
        """Binance订单簿回调 - 更新数据并立即计算价差"""
        # 更新价格板（内部已处理）
        DataBuffer.push_neworder_book("Binance", book)
        
        # 立即计算价差
        if book.bids and book.asks:
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
        
        # 可选：调试日志
        print(f"[BINANCE] 价格更新: bid={book.bids[0].price if book.bids else 0:.6f}, ask={book.asks[0].price if book.asks else 0:.6f}")
    
    @classmethod
    def on_hyperliquid_raw(cls, raw_data):
        """接收Hyperliquid原始数据 → 解析 → 结构化处理"""
        book = HyperliquidWebSocket.parse_l2book(raw_data, depth=5)
        cls.on_hyperliquid_l2book(book)
    
    @classmethod
    def on_trades(cls, data):
        """处理交易数据"""
        for trade in data.get("data", []):
            side = "buy" if trade["side"] == "B" else "sell"
            logging.info(f"[TRADE] {trade['coin']} {side} {trade['sz']} @ {trade['price']} (t={trade['time']})")
