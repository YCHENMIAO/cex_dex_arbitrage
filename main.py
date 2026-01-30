import json
import threading
import time
import logging
from typing import Dict, List, Callable, Optional, Any, NamedTuple, Tuple
from dataclasses import dataclass
from datetime import datetime
import pandas as pd
import requests
import hmac
import asyncio
import hashlib
from urllib.parse import urlencode
import config as cfg
from DataBuffer import Level, L2Book, Ticker, PriceBoard, price_board, DataBuffer
from Market_data import Depth_Marketdata
from websocket_cex_dex import (
    BaseWebSocket, 
    HyperliquidWebSocket, 
    BinanceWebSocket,
    BinanceListenKeyManager,
    BinanceUserStream,
    ExchangeManager
)
from trade_engine import TradeExecutor, InitialStateChecker
from Simple_strategy import StrategyStateMachine, StrategyState
from binance.client import Client
from binance import ThreadedWebsocketManager
try:
    from nacl.signing import SigningKey# 安装pynacl库，方便hyper下单
    HAVE_NACL = True
except Exception:
    HAVE_NACL = False

# Hyperliquid SDK 导入（根据实际安装的包名调整）
try:
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    from hyperliquid.utils.signing import sign_l1_action
    from hyperliquid.utils.signing import get_payload
    HAVE_HYPER_SDK = True
except ImportError:
    try:
        # 备用导入方式
        from hyperliquid.exchange import Exchange
        HAVE_HYPER_SDK = True
        HYPER_SDK_TYPE = "exchange"
    except ImportError:
        HAVE_HYPER_SDK = False
        HYPER_SDK_TYPE = None


# ==================== 配置区 ====================
# 全局常量移至 config.py，通过 cfg.<VAR> 引用
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# ================================================

# ==================== 策略状态机 ====================
# 注意：状态机相关功能已移至 Simple_strategy.py 中的 StrategyStateMachine 类




# 全局交易执行器实例
trade_executor = TradeExecutor()

# 全局策略状态机实例（将在初始化时创建）
strategy_machine = None


#=======================订单状态管理=================
# 注意：订单状态处理已集成到回调函数中，直接调用状态机的 on_order_update_logic()
# ==================== 下单/撤单 ====================
def _binance_sign(secret_key: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """对Binance参数进行签名并返回带签名的参数"""
    query = urlencode({k: v for k, v in params.items() if v is not None})
    signature = hmac.new(secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    signed = dict(params)
    signed["signature"] = signature
    return signed

"""关于下单函数的一些说明：

Binance 合约和现货
下单 POST /api/v3/order，
撤单 DELETE /api/v3/order。
请求参数含 timestamp 并使用 secret_key 对 query 串签名，签名放在查询参数 signature。
市价单默认用 quoteOrderQty 表示花费的 USDT（若你想用数量，下单时传 quantity 并移除 quoteOrderQty）。
限价单用 timeInForce=GTC 且需要 quantity 与 price。

Hyper合约下单,因为hyper要用到钱包
POST {api_url}/exchange，body 为 {"action": {...}, "nonce": timestamp, "signature": ...}
签名对 action 的紧凑 JSON 字符串进行 Ed25519 签名（使用 PyNaCl）。
需要传入钱包私钥十六进制串 wallet_private_key。
限价单 order_type={"limit":{"tif":"Gtc"}} 且填 limit_px；
市价单 order_type={"market":{"slippage":"0.05"}} 且 limit_px="0"。
撤单 action={"type":"cancel","orderIds":[...]} 同样签名发送。
"""





# ===================== Binance 用户流回调处理 =====================
def create_binance_user_callback(strategy_machine):
    """创建 Binance 用户流回调函数"""
    def on_binance_user_message(msg):
        """
        处理 Binance 用户流消息
        支持两种格式：
        1. 合约用户流: {"e": "ORDER_TRADE_UPDATE", "o": {...}}
        2. 现货用户流: {"e": "executionReport", "i": order_id, "X": status, ...}
        """
        # 格式1: 合约用户流 (ORDER_TRADE_UPDATE)
        if msg.get("e") == "ORDER_TRADE_UPDATE":
            o = msg.get("o", {})
            status_raw = o.get("X", "")  # 订单状态
            cum_filled_qty = float(o.get("z", 0))  # 累计成交数量 (z: cumQty)
            order_id = o.get("i")  # 订单ID
            client_order_id = o.get("c")  # 客户端订单ID
            
            if status_raw == "FILLED":
                strategy_machine.on_order_update_logic("Binance", "ALL_traded", filled_qty=cum_filled_qty, order_id=order_id)
            
            elif status_raw in ("CANCELED", "EXPIRED"):
                if cum_filled_qty > 0:
                    # 关键点：如果撤单时成交量 > 0，则是部分成交撤单
                    strategy_machine.on_order_update_logic("Binance", "PARTIAL_filled_canceled", filled_qty=cum_filled_qty, order_id=order_id)
                else:
                    strategy_machine.on_order_update_logic("Binance", "ALL_canceled", filled_qty=0.0, order_id=order_id)
            
            elif status_raw == "REJECTED":
                strategy_machine.on_order_update_logic("Binance", "ALL_canceled", filled_qty=0.0, order_id=order_id)
            
            else:
                logging.debug(f"[Binance 用户流] 订单 {client_order_id or order_id} 状态: {status_raw} (未最终状态)")
        
        # 格式2: 现货用户流 (executionReport) - 向后兼容
        elif msg.get("e") == "executionReport":
            order_id = msg.get("i")
            client_order_id = msg.get("c")
            status = msg.get("X")
            cum_filled_qty = float(msg.get("z", 0))  # 累计成交数量

            if status == "FILLED":
                strategy_machine.on_order_update_logic("Binance", "ALL_traded", filled_qty=cum_filled_qty, order_id=order_id)
            elif status in ("CANCELED", "EXPIRED"):
                if cum_filled_qty > 0:
                    strategy_machine.on_order_update_logic("Binance", "PARTIAL_filled_canceled", filled_qty=cum_filled_qty, order_id=order_id)
                else:
                    strategy_machine.on_order_update_logic("Binance", "ALL_canceled", filled_qty=0.0, order_id=order_id)
            elif status == "REJECTED":
                strategy_machine.on_order_update_logic("Binance", "ALL_canceled", filled_qty=0.0, order_id=order_id)
            else:
                logging.debug(f"[Binance 用户流] 订单 {client_order_id or order_id} 状态: {status} (未最终状态)")
    
    return on_binance_user_message

# ===================== Hyperliquid 用户流回调处理 =====================
def create_hyper_user_callback(strategy_machine):
    """创建 Hyperliquid 用户流回调函数"""
    def hyper_user_callback(message):
        """
        Hyperliquid 用户流回调解析
        message 格式: {"channel": "user", "data": {...}} 或 {"channel": "orderUpdates", "data": [...]}
        """
        # 格式1: orderUpdates 频道（批量订单更新）
        if message.get("channel") == "orderUpdates":
            updates = message.get("data", [])
            for item in updates:
                order = item.get("order", {})
                status_raw = order.get("status", "").lower()  # 'filled' 或 'canceled'
                cum_sz = float(order.get("cumSz", 0))  # 累计成交数量
                sz = float(order.get("sz", 0))  # 订单总数量
                oid = order.get("oid")  # 订单ID
                cloid = order.get("cloid")  # 客户端订单ID
                
                if status_raw == "filled":
                    # 注意：HL 可能会分批推送 filled，这里建议逻辑是直到全部成交才算 ALL_traded
                    # 或者简化处理：只要状态变为 filled 且 cumSz 等于总 Sz 
                    if cum_sz >= sz or abs(cum_sz - sz) < 1e-8:  # 考虑浮点误差
                        strategy_machine.on_order_update_logic("Hyperliquid", "ALL_traded", filled_qty=cum_sz, order_id=oid or cloid)
                    else:
                        logging.warning(f"[Hyperliquid] 订单 {cloid or oid} 部分成交: {cum_sz}/{sz}")
                
                elif status_raw in ("canceled", "cancelled"):
                    if cum_sz > 0:
                        strategy_machine.on_order_update_logic("Hyperliquid", "PARTIAL_filled_canceled", filled_qty=cum_sz, order_id=oid or cloid)
                    else:
                        strategy_machine.on_order_update_logic("Hyperliquid", "ALL_canceled", filled_qty=0.0, order_id=oid or cloid)
                
                elif status_raw in ("rejected", "expired"):
                    strategy_machine.on_order_update_logic("Hyperliquid", "ALL_canceled", filled_qty=0.0, order_id=oid or cloid)
        
        # 格式2: user 频道（单个订单更新）- 向后兼容
        elif message.get("channel") == "user":
            user_data = message.get("data", {})
            
            # Hyperliquid 用户流可能包含多种类型的事件：order, fill, cancel 等
            if user_data.get("type") == "order":
                order_data = user_data.get("data", {})
                order = order_data.get("order", {})
                oid = order.get("oid")
                cloid = order.get("cloid")
                status = order.get("status", "").lower()
                cum_sz = float(order.get("cumSz", 0))
                sz = float(order.get("sz", 0))

                if status == "filled":
                    if cum_sz >= sz or abs(cum_sz - sz) < 1e-8:
                        strategy_machine.on_order_update_logic("Hyperliquid", "ALL_traded", filled_qty=cum_sz, order_id=oid or cloid)
                    else:
                        logging.warning(f"[Hyperliquid] 订单 {cloid or oid} 部分成交: {cum_sz}/{sz}")
                elif status in ("cancelled", "canceled"):
                    if cum_sz > 0:
                        strategy_machine.on_order_update_logic("Hyperliquid", "PARTIAL_filled_canceled", filled_qty=cum_sz, order_id=oid or cloid)
                    else:
                        strategy_machine.on_order_update_logic("Hyperliquid", "ALL_canceled", filled_qty=0.0, order_id=oid or cloid)
                elif status in ("rejected", "expired"):
                    strategy_machine.on_order_update_logic("Hyperliquid", "ALL_canceled", filled_qty=0.0, order_id=oid or cloid)
            else:
                # 处理其他类型的用户事件（fill, cancel 等）
                logging.debug(f"[Hyperliquid 用户流] 收到事件: {user_data.get('type', 'unknown')}")
    
    return hyper_user_callback

# ===================== 启动入口 =====================
def start_all_user_streams(strategy_machine, testnet=True):
    """启动所有用户流（Binance 和 Hyperliquid）"""
    # 创建 Binance 用户流回调
    binance_user_callback = create_binance_user_callback(strategy_machine)
    
    # 创建并启动 Binance 用户流
    binance_stream = BinanceUserStream(
        testnet=testnet,
        callback=binance_user_callback
    )
    binance_stream.start()
    
    logging.info("两个交易所用户流已全部启动，订单状态实时驱动状态机！")
    return binance_stream

if __name__ == "__main__":
    # ==================== 第一步：初始化交易执行器 ====================
    print("正在初始化交易执行器...")
    trade_executor = TradeExecutor()
    trade_executor.init_clients(testnet=True)
    print("交易执行器初始化完成！")
    
    # ==================== 第二步：创建策略状态机 ====================
    print("正在创建策略状态机...")
    globals()['strategy_machine'] = StrategyStateMachine(trade_executor)
    strategy_machine = globals()['strategy_machine']
    print(f"策略状态机创建完成，初始状态: {strategy_machine.get_state()}")
    
    # ====================查询账户余额和持仓 ====================
    print("正在检查账户余额和持仓状态...")
    state_checker = InitialStateChecker(trade_executor)
    
    # 查询账户余额
    balances = state_checker.get_balances()
    print(f"Binance USDT 余额: {balances['binance']['usdt']:.2f}")
    print(f"Hyperliquid USDC 余额: {balances['hyperliquid']['usdc']:.2f}")
    
    # 查询持仓并设置初始状态（传入策略状态机实例）
    is_position_valid, initial_state = state_checker.Req_Investment_position(strategy_machine=strategy_machine)
    
    if not is_position_valid:
        print("错误：持仓状态异常，程序退出！")
        print("请检查账户持仓情况：")
        print("  - 如果只有单边持仓，请先平仓")
        print("  - 如果持仓组合不正确，请调整持仓")
        exit(1)
    
    # 状态机状态已在 Req_Investment_position 中更新
    if initial_state:
        print(f"账户余额和持仓检查通过！策略状态已设置为: {initial_state}")
    
    # ==================== 第三步：创建交易所管理器 ====================
    manager = ExchangeManager()
    
    # 创建 Hyperliquid 客户端（同时支持行情流和用户流）
    wallet = cfg.HYPERTEST_WALLET  # 或根据环境选择主网/测试网
    hyper_user_callback = create_hyper_user_callback(strategy_machine)
    hyperliquid_client = HyperliquidWebSocket(
        url=cfg.WS_URL,
        wallet_address=wallet,
        user_callback=hyper_user_callback
    )
    manager.add_exchange("Hyperliquid", hyperliquid_client)
    
    # 创建 Binance 客户端（第一个参数是交易对符号，第二个参数是URL）
    binance_client = BinanceWebSocket(cfg.BINANCE_SYMBOL, cfg.BINANCE_WSCONTRACT_URL)
    manager.add_exchange("Binance", binance_client)
    
    # 启动所有交易所（每个交易所内部已有独立线程）
    print("启动所有交易所连接...")
    manager.start_all()
    
    # 等待连接就绪
    print("等待连接建立...")
    manager.wait_all_ready(timeout=15)

    # 订阅行情流（用户流已在初始化时自动订阅）
    if hyperliquid_client.ws_ready.is_set():
        print("Hyperliquid连接成功！开始订阅行情流...")
        hyperliquid_client.subscribe("l2Book", cfg.HYPER_SYMBOL, Depth_Marketdata.on_hyperliquid_raw)
    else:
        print("Hyperliquid连接失败")
    
    if binance_client.ws_ready.is_set():
        print("Binance连接成功！开始订阅...")
        binance_client.subscribe(Depth_Marketdata.on_binance_l2book)
    else:
        print("Binance连接失败")

    #=========================启用用户数据流==================
    binance_user_stream = start_all_user_streams(strategy_machine, testnet=True)

    try:
        print("\n套利系统运行中，按 Ctrl+C 停止...")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭所有连接...")
        binance_user_stream.stop_User_stream()  # 断开 Binance 用户流连接
        manager.stop_all()  # 断开所有行情连接（包括 Hyperliquid 的行情流和用户流）

        time.sleep(2)
        print("已关闭")