"""
Simple Strategy 模块 (修正版 - Leg1 始终为 Hyperliquid)
逻辑：
    不管是开仓还是平仓，永远先在 Hyperliquid (流动性差) 挂单。
    成交后，去 Binance (流动性好) 吃单对冲。
"""
import logging
import threading
import time
import config as cfg
from DataBuffer import DataBuffer, price_board

class StrategyState:
    # --- 待机状态 ---
    OpenCondition      = "OpenCondition"      # 空仓，扫描开仓机会
    CloseCondition     = "CloseCondition"     # 持仓，扫描平仓机会
    
    # --- 开仓流程 (Leg1=Hyper Buy, Leg2=Binance Sell) ---
    OpenLeg1Waiting    = "OpenLeg1Waiting"    # Hyper 挂单中 (买)
    OpenLeg1Canceling  = "OpenLeg1Canceling"  # Hyper 撤单中
    OpenLeg2Waiting    = "OpenLeg2Waiting"    # Binance 对冲中 (卖)
    OpenLeg2Chasing    = "OpenLeg2Chasing"    # Binance 追单中
    
    # --- 平仓流程 (Leg1=Hyper Sell, Leg2=Binance Buy) ---
    CloseLeg1Waiting   = "CloseLeg1Waiting"   # Hyper 挂单中 (卖)  <-- 修正: Leg1 是 Hyper
    CloseLeg1Canceling = "CloseLeg1Canceling" # Hyper 撤单中
    CloseLeg2Waiting   = "CloseLeg2Waiting"   # Binance 平仓中 (买)  <-- 修正: Leg2 是 Binance
    CloseLeg2Chasing   = "CloseLeg2Chasing"   # Binance 追单中

class StrategyStateMachine:
    def __init__(self, trade_executor):
        self.executor = trade_executor
        self.lock = threading.Lock()
        
        # --- 状态管理 ---
        self.current_state = StrategyState.OpenCondition
        
        # --- 交易配置 ---
        self.base_quantity = 0.001       # 目标开仓数量
        self.order_timeout_sec = 5.0     # 超时时间
        self.max_chase_retries = 3       # 最大限价追单次数（前3次限价，第4次市价）
        
        # --- 精度配置（根据实际交易对调整）---
        # Binance BTCUSDT: 价格2位小数，数量3位小数
        self.binance_price_precision = 2  # 价格精度（小数位数）
        self.binance_qty_precision = 3    # 数量精度（小数位数）
        # Hyperliquid BTC: 价格通常2位小数，数量3位小数（根据实际调整）
        self.hyper_price_precision = 2
        self.hyper_qty_precision = 3
        
        # --- 运行时数据 ---
        self.leg1_filled_qty = 0.0       # Leg1 已成交数量 (累计量，作为 Leg2 的目标)
        self.leg2_filled_qty = 0.0       # Leg2 已成交数量（累计量）
        self.current_position = 0.0      # 总净持仓 (Hyper方向: 正为多, 负为空)
        
        # --- 订单追踪 ---
        self.active_order_id = None      # 当前活跃订单 ID（统一为字符串）
        self.active_order_time = 0       # 当前活跃订单发送时间
        self.chase_retry_count = 0       # 追单计数器
        self.last_cum_filled_qty = {}    # 记录每个订单ID的上一次累计成交量（用于计算增量）

    # ==================== 辅助函数 ====================
    @staticmethod
    def _round_precision(value, decimals):
        """精度处理：四舍五入到指定小数位数"""
        if value is None:
            return None
        return round(float(value), decimals)
    
    def _round_price(self, price, exchange):
        """价格精度处理"""
        if price is None:
            return None
        precision = self.binance_price_precision if exchange.lower() == "binance" else self.hyper_price_precision
        return self._round_precision(price, precision)
    
    def _round_qty(self, qty, exchange):
        """数量精度处理"""
        if qty is None:
            return None
        precision = self.binance_qty_precision if exchange.lower() == "binance" else self.hyper_qty_precision
        return self._round_precision(qty, precision)
    
    def _extract_order_id(self, response, exchange):
        """
        从下单响应中提取订单ID，统一转换为字符串
        
        返回: (order_id: str, success: bool)
        """
        if not response:
            return None, False
        
        if "error" in response:
            logging.error(f"[下单响应] {exchange} 错误: {response['error']}")
            return None, False
        
        exchange_lower = exchange.lower()
        
        # Binance: 直接有 orderId 字段
        if exchange_lower == "binance":
            if "orderId" in response:
                order_id = str(response["orderId"])  # 统一转为字符串
                return order_id, True
            else:
                logging.error(f"[下单响应] Binance 响应中未找到 orderId: {response}")
                return None, False
        
        # Hyperliquid: 深层嵌套结构
        elif exchange_lower == "hyperliquid":
            # 尝试多种可能的格式
            if "orderId" in response:
                return str(response["orderId"]), True
            
            # 格式1: {'status': 'ok', 'response': {'data': {'statuses': [{'resting': {'oid': ...}}]}}}
            if "response" in response:
                resp_data = response.get("response", {})
                if isinstance(resp_data, dict):
                    data = resp_data.get("data", {})
                    if isinstance(data, dict):
                        statuses = data.get("statuses", [])
                        if statuses and isinstance(statuses, list) and len(statuses) > 0:
                            first_status = statuses[0]
                            if "resting" in first_status:
                                oid = first_status["resting"].get("oid")
                                if oid:
                                    return str(oid), True
                            # 也可能直接有 oid
                            if "oid" in first_status:
                                return str(first_status["oid"]), True
            
            # 格式2: 直接返回的列表格式（某些SDK可能这样）
            if isinstance(response, list) and len(response) > 0:
                first_item = response[0]
                if isinstance(first_item, dict):
                    if "orderId" in first_item:
                        return str(first_item["orderId"]), True
                    resp = first_item.get("response", {})
                    if isinstance(resp, dict):
                        data = resp.get("data", {})
                        if isinstance(data, dict):
                            statuses = data.get("statuses", [])
                            if statuses and len(statuses) > 0 and "resting" in statuses[0]:
                                oid = statuses[0]["resting"].get("oid")
                                if oid:
                                    return str(oid), True
            
            logging.error(f"[下单响应] Hyperliquid 响应格式无法解析: {response}")
            return None, False
        
        else:
            logging.error(f"[下单响应] 不支持的交易所: {exchange}")
            return None, False

    # ==================== 状态变更 ====================
    def update_state(self, new_state):
        self.current_state = new_state
        logging.info(f"[状态变更] >>> {new_state}")

    def get_state(self):
        with self.lock:
            return self.current_state

    # ==================== 核心：主循环超时检查 (on_tick) ====================
    def on_tick_check(self):
        """每秒执行：处理超时和追单间隔"""
        with self.lock:
            now = time.time()
            state = self.current_state
            
            if self.active_order_id is None:
                return

            # --- 1. Leg 1 (Hyperliquid) 挂单超时检查 ---
            # 无论是开仓买入，还是平仓卖出，Leg1 都是 Hyperliquid
            if state in [StrategyState.OpenLeg1Waiting, StrategyState.CloseLeg1Waiting]:
                if now - self.active_order_time > self.order_timeout_sec:
                    logging.warning(f"[超时] Hyper Leg1 订单 {self.active_order_id} 超时，执行撤单")
                    
                    next_state = StrategyState.OpenLeg1Canceling if state == StrategyState.OpenLeg1Waiting else StrategyState.CloseLeg1Canceling
                    self.update_state(next_state)
                    
                    # 始终撤 Hyperliquid 的单
                    threading.Thread(target=self._send_cancel, args=("Hyperliquid", self.active_order_id)).start()

            # --- 2. Leg 2 (Binance) 及追单超时检查 ---
            elif state in [StrategyState.OpenLeg2Waiting, StrategyState.OpenLeg2Chasing, 
                           StrategyState.CloseLeg2Waiting, StrategyState.CloseLeg2Chasing]:
                
                if now - self.active_order_time > self.order_timeout_sec:
                    logging.warning(f"[超时] Binance Leg2/追单 {self.active_order_id} 超时，取消并触发重试")
                    # 始终撤 Binance 的单
                    threading.Thread(target=self._send_cancel, args=("Binance", self.active_order_id)).start()

    # ==================== 核心：订单回调处理 ====================
    def on_order_update_logic(self, exchange, event_type, order_id, filled_qty=0.0):
        """
        处理 WS 回报。
        filled_qty: main.py传入的是累计成交量（cum_filled_qty），需要计算增量
        event_type: "ALL_traded", "PARTIAL_filled_canceled", "ALL_canceled"
        """
        with self.lock:
            # 订单ID验证：统一转为字符串比较（确保类型一致）
            if order_id is None or self.active_order_id is None:
                return
            
            order_id_str = str(order_id)
            active_order_id_str = str(self.active_order_id)
            
            if order_id_str != active_order_id_str:
                logging.debug(f"[回调] 订单ID不匹配，忽略: 收到={order_id_str}, 当前={active_order_id_str}")
                return
            
            # 计算增量成交量（使用字符串ID作为key）
            last_cum = self.last_cum_filled_qty.get(order_id_str, 0.0)
            incremental_qty = filled_qty - last_cum
            if incremental_qty < 0:  # 防止累计量异常
                incremental_qty = 0.0
            self.last_cum_filled_qty[order_id_str] = filled_qty

            logging.info(f"[回调] State:{self.current_state}, Event:{event_type}, CumQty:{filled_qty}, IncQty:{incremental_qty}")

            # ------------------------------------------------------------
            # 一、开仓流程 (Hyper Buy -> Binance Sell)
            # ------------------------------------------------------------
            
            # [Leg 1] Hyper 挂单中
            if self.current_state == StrategyState.OpenLeg1Waiting:
                if event_type == "ALL_traded":
                    # 完全成交
                    self.leg1_filled_qty = filled_qty  # 使用累计量
                    self.current_position += incremental_qty  # 持仓增加 (Hyper Long)
                    self._start_leg2_open(initial=True, qty=self.leg1_filled_qty)
                    self.active_order_id = None
                    
                elif event_type == "PARTIAL_filled_canceled":
                    # 部分成交且撤单：立即对冲已成交部分
                    self.leg1_filled_qty = filled_qty  # 使用累计量
                    self.current_position += incremental_qty
                    self.update_state(StrategyState.OpenLeg1Canceling)
                    threading.Thread(target=self._send_cancel, args=("Hyperliquid", order_id)).start()
                    self._start_leg2_open(initial=True, qty=filled_qty)
                    self.active_order_id = None

            # [Leg 1] Hyper 撤单中
            elif self.current_state == StrategyState.OpenLeg1Canceling:
                if event_type == "ALL_canceled":
                    # 撤单成功，无成交
                    if self.leg1_filled_qty == 0:
                        self.update_state(StrategyState.OpenCondition)
                    self.active_order_id = None
                elif event_type == "ALL_traded":
                    # 撤单失败，全成了（2秒窗口内收到FILLED）
                    self.leg1_filled_qty = filled_qty
                    self.current_position += incremental_qty
                    self._start_leg2_open(initial=False, qty=filled_qty)
                    self.active_order_id = None

            # [Leg 2] Binance 对冲/追单中 (Sell)
            elif self.current_state in [StrategyState.OpenLeg2Waiting, StrategyState.OpenLeg2Chasing]:
                target = self.leg1_filled_qty
                
                if event_type == "ALL_traded":
                    # 完全成交
                    self.leg2_filled_qty = filled_qty
                    if abs(target - self.leg2_filled_qty) <= 1e-6:
                        logging.info("开仓对冲完成！")
                        self.active_order_id = None
                        self.leg1_filled_qty = 0.0
                        self.leg2_filled_qty = 0.0
                        self.chase_retry_count = 0
                        self.update_state(StrategyState.CloseCondition)
                elif event_type == "PARTIAL_filled_canceled":
                    # 部分成交且撤单：继续追单
                    self.leg2_filled_qty = filled_qty
                    remaining = target - self.leg2_filled_qty
                    self.update_state(StrategyState.OpenLeg2Chasing)
                    if remaining > 1e-6:
                        self._execute_leg2_chase_step(exchange="Binance", side="SELL", qty=remaining)
                elif event_type == "ALL_canceled":
                    # 被撤销或被拒，继续追
                    self.update_state(StrategyState.OpenLeg2Chasing)
                    remaining = target - self.leg2_filled_qty
                    if remaining > 1e-6:
                        self._execute_leg2_chase_step(exchange="Binance", side="SELL", qty=remaining)

            # ------------------------------------------------------------
            # 二、平仓流程 (Hyper Sell -> Binance Buy) [修正后]
            # ------------------------------------------------------------

            # [Leg 1] Hyper 挂单中 (Sell)
            elif self.current_state == StrategyState.CloseLeg1Waiting:
                if event_type == "ALL_traded":
                    # 完全成交
                    self.leg1_filled_qty = filled_qty  # 使用累计量
                    self.current_position -= incremental_qty  # 持仓减少
                    self._start_leg2_close(initial=True, qty=self.leg1_filled_qty)
                    self.active_order_id = None
                elif event_type == "PARTIAL_filled_canceled":
                    # 部分成交且撤单：立即平掉已成交部分
                    self.leg1_filled_qty = filled_qty  # 使用累计量
                    self.current_position -= incremental_qty
                    self.update_state(StrategyState.CloseLeg1Canceling)
                    threading.Thread(target=self._send_cancel, args=("Hyperliquid", order_id)).start()
                    self._start_leg2_close(initial=True, qty=filled_qty)
                    self.active_order_id = None

            # [Leg 1] Hyper 撤单中
            elif self.current_state == StrategyState.CloseLeg1Canceling:
                if event_type == "ALL_canceled":
                    # 撤单成功
                    if self.current_position <= 1e-5:
                        # 已经平完了
                        self.update_state(StrategyState.OpenCondition)
                    else:
                        # 没平完，回到 CloseCondition 继续等待
                        self.update_state(StrategyState.CloseCondition)
                    self.active_order_id = None
                elif event_type == "ALL_traded":
                    # 撤单失败，全成了（2秒窗口内收到FILLED）
                    self.leg1_filled_qty = filled_qty
                    self.current_position -= incremental_qty
                    self._start_leg2_close(initial=False, qty=filled_qty)
                    self.active_order_id = None

            # [Leg 2] Binance 平仓/追单中 (Buy)
            elif self.current_state in [StrategyState.CloseLeg2Waiting, StrategyState.CloseLeg2Chasing]:
                target = self.leg1_filled_qty
                
                if event_type == "ALL_traded":
                    # 完全成交
                    self.leg2_filled_qty = filled_qty
                    if abs(target - self.leg2_filled_qty) <= 1e-6:
                        logging.info("平仓对冲完成！")
                        self.active_order_id = None
                        self.leg1_filled_qty = 0.0
                        self.leg2_filled_qty = 0.0
                        self.chase_retry_count = 0
                        # 检查总仓位
                        if self.current_position <= 1e-5:
                            self.update_state(StrategyState.OpenCondition)
                        else:
                            # 极少情况：分批平仓中
                            self.update_state(StrategyState.CloseCondition)
                elif event_type == "PARTIAL_filled_canceled":
                    # 部分成交且撤单：继续追单
                    self.leg2_filled_qty = filled_qty
                    remaining = target - self.leg2_filled_qty
                    self.update_state(StrategyState.CloseLeg2Chasing)
                    if remaining > 1e-6:
                        self._execute_leg2_chase_step(exchange="Binance", side="BUY", qty=remaining)
                elif event_type == "ALL_canceled":
                    # 被撤销或被拒，继续追
                    self.update_state(StrategyState.CloseLeg2Chasing)
                    remaining = target - self.leg2_filled_qty
                    if remaining > 1e-6:
                        self._execute_leg2_chase_step(exchange="Binance", side="BUY", qty=remaining)


    # ==================== 动作执行：追单逻辑 ====================

    def _start_leg2_open(self, initial, qty):
        """开始 Leg 2 开仓 (Binance Sell)"""
        if initial:
            self.chase_retry_count = 0
            self.leg2_filled_qty = 0 
            self.update_state(StrategyState.OpenLeg2Waiting)
        else:
            self.update_state(StrategyState.OpenLeg2Chasing)
        
        self._execute_leg2_chase_step("Binance", "SELL", qty)

    def _start_leg2_close(self, initial, qty):
        """开始 Leg 2 平仓 (Binance Buy)"""
        if initial:
            self.chase_retry_count = 0
            self.leg2_filled_qty = 0 # 重置 Leg2 进度
            self.update_state(StrategyState.CloseLeg2Waiting)
        else:
            self.update_state(StrategyState.CloseLeg2Chasing)
            
        self._execute_leg2_chase_step("Binance", "BUY", qty)

    def _execute_leg2_chase_step(self, exchange, side, qty):
        """通用追单 (针对 Binance) - 前3次限价单，第4次市价单"""
        price = None
        
        # 每次追单前重新获取当前订单簿价格
        ticker_side = "bid" if side == "SELL" else "ask" 
        market_price = price_board.get_price(exchange, ticker_side)
        
        # 精度处理：数量和价格
        qty_rounded = self._round_qty(qty, exchange)
        if qty_rounded is None or qty_rounded <= 0:
            logging.error(f"[追单] 数量无效: {qty}")
            return
        
        if market_price is None:
            logging.error(f"[追单] 无法获取 {exchange} {ticker_side} 价格，使用市价单")
            price = None  # 市价单
        elif self.chase_retry_count < self.max_chase_retries:
            # 前3次：限价单，每次价格调整 0.1%
            adj = (self.chase_retry_count + 1) * 0.001
            if side == "SELL": 
                price_raw = market_price * (1 - adj)  # 卖盘降低价格
            else:              
                price_raw = market_price * (1 + adj)  # 买盘提高价格
            price = self._round_price(price_raw, exchange)  # 精度处理
            logging.info(f"[追单] {exchange} {side} Limit 第{self.chase_retry_count+1}次 | 价格: {price} (基准: {market_price:.4f}), 数量: {qty_rounded}")
        else:
            # 第4次及以后：市价单
            price = None
            logging.info(f"[追单] {exchange} {side} Market (第{self.chase_retry_count+1}次), 数量: {qty_rounded}")

        # 同步下单（async_exec=False），因为需要立即获取订单ID
        raw_res = self.executor.place_order(
            exchange=exchange,
            symbol=cfg.BINANCE_SYMBOL,
            side=side,
            quantity=qty_rounded,  # 使用精度处理后的数量
            price=price,  # price=None 表示市价单，已做精度处理
            async_exec=False  # ✅ 同步执行，确保立即获取响应
        )
        
        # 使用统一的方法提取订单ID
        order_id, success = self._extract_order_id(raw_res, exchange)
        if success and order_id:
            self.active_order_id = order_id  # 已经是字符串
            self.active_order_time = time.time()
            self.chase_retry_count += 1
            self.last_cum_filled_qty[order_id] = 0.0  # 初始化累计量
            logging.info(f"[追单] 下单成功，订单ID: {order_id}")
        else:
            error_msg = raw_res.get("error", "未知错误") if raw_res else "无响应"
            logging.error(f"[追单] 下单失败或无法提取订单ID: {error_msg}")

    def _send_cancel(self, exchange, order_id):
        """撤单（根据交易所类型传递正确参数）"""
        if exchange.lower() == "binance":
            self.executor.cancel_order(
                exchange=exchange,
                symbol=cfg.BINANCE_SYMBOL,
                order_id=order_id,
                async_exec=False
            )
        elif exchange.lower() == "hyperliquid":
            self.executor.cancel_order(
                exchange=exchange,
                order_ids=[order_id],
                async_exec=False
            )

    # ==================== 外部接口 ====================
    def check_and_execute_open(self, signal_func):
        if self.get_state() != StrategyState.OpenCondition: 
            return
        if signal_func():
            with self.lock:
                if self.current_state != StrategyState.OpenCondition: 
                    return
                logging.info(">>> 触发开仓信号 (Hyper Long) <<<")
                
                # Leg 1: Hyper Buy (Maker) - 限价单
                price_raw = price_board.get_price("Hyperliquid", "bid")
                if price_raw is None:
                    logging.error("[开仓] 无法获取 Hyperliquid bid 价格，取消开仓")
                    return
                
                # 精度处理
                price = self._round_price(price_raw, "Hyperliquid")
                qty = self._round_qty(self.base_quantity, "Hyperliquid")
                
                if qty is None or qty <= 0:
                    logging.error(f"[开仓] 数量无效: {self.base_quantity}")
                    return
                
                # 同步下单，获取订单ID（async_exec=False）
                raw_res = self.executor.place_order(
                    exchange="Hyperliquid",
                    symbol=cfg.HYPER_SYMBOL,
                    side="BUY",
                    quantity=qty,  # 精度处理后的数量
                    price=price,   # 精度处理后的价格
                    async_exec=False  # ✅ 同步执行
                )
                
                # 使用统一的方法提取订单ID
                order_id, success = self._extract_order_id(raw_res, "Hyperliquid")
                if success and order_id:
                    self.active_order_id = order_id  # 已经是字符串
                    self.active_order_time = time.time()
                    self.leg1_filled_qty = 0.0
                    self.last_cum_filled_qty[order_id] = 0.0
                    self.update_state(StrategyState.OpenLeg1Waiting)
                    logging.info(f"[开仓] Leg1 下单成功，订单ID: {order_id}, 价格: {price}, 数量: {qty}")
                else:
                    error_msg = raw_res.get("error", "未知错误") if raw_res else "无响应"
                    logging.error(f"[开仓] 下单失败或无法提取订单ID: {error_msg}")

    def check_and_execute_close(self, signal_func):
        if self.get_state() != StrategyState.CloseCondition: 
            return
        if signal_func():
            with self.lock:
                if self.current_state != StrategyState.CloseCondition: 
                    return
                if self.current_position <= 1e-5: 
                    return

                logging.info(">>> 触发平仓信号 (Hyper Short) <<<")
                
                # Leg 1: Hyper Sell (Maker) - 限价单
                price_raw = price_board.get_price("Hyperliquid", "ask")
                if price_raw is None:
                    logging.error("[平仓] 无法获取 Hyperliquid ask 价格，取消平仓")
                    return
                
                # 精度处理
                price = self._round_price(price_raw, "Hyperliquid")
                qty = self._round_qty(self.current_position, "Hyperliquid")  # 平掉所有
                
                if qty is None or qty <= 0:
                    logging.error(f"[平仓] 数量无效: {self.current_position}")
                    return
                
                # 同步下单，获取订单ID（async_exec=False）
                raw_res = self.executor.place_order(
                    exchange="Hyperliquid",
                    symbol=cfg.HYPER_SYMBOL,
                    side="SELL",
                    quantity=qty,  # 精度处理后的数量
                    price=price,   # 精度处理后的价格
                    async_exec=False  # ✅ 同步执行
                )
                
                # 使用统一的方法提取订单ID
                order_id, success = self._extract_order_id(raw_res, "Hyperliquid")
                if success and order_id:
                    self.active_order_id = order_id  # 已经是字符串
                    self.active_order_time = time.time()
                    self.leg1_filled_qty = 0.0  # 重置，成交回调时累加
                    self.last_cum_filled_qty[order_id] = 0.0
                    self.update_state(StrategyState.CloseLeg1Waiting)
                    logging.info(f"[平仓] Leg1 下单成功，订单ID: {order_id}, 价格: {price}, 数量: {qty}")
                else:
                    error_msg = raw_res.get("error", "未知错误") if raw_res else "无响应"
                    logging.error(f"[平仓] 下单失败或无法提取订单ID: {error_msg}")