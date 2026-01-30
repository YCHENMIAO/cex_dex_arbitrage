"""
不同交易所下单的管理
交易执行模块 (优化版)
"""
from concurrent.futures import ThreadPoolExecutor
from binance.client import Client
from binance.exceptions import BinanceAPIException
import logging
from typing import Dict, Optional, Any
import config as cfg

# Hyperliquid 依赖
try:
    from hyperliquid.exchange import Exchange as HyperExchange
    from hyperliquid.info import Info
    from hyperliquid.utils import constants
    import eth_account
    HAVE_HYPER_SDK = True
except ImportError:
    HAVE_HYPER_SDK = False

class TradeExecutor:
    def __init__(self):
        self.binance_client = None
        self.hyper_exchange = None
        self.hyper_info = None  # Hyperliquid Info 客户端，用于查询账户信息
        self.executor = ThreadPoolExecutor(max_workers=4) # 4个线程够用了
        self.is_ready = False

    def init_clients(self, testnet=True):
        """
        程序启动时调用一次，建立长连接
        """
        logging.info("正在初始化交易接口连接...")
        
        # 1. 初始化 Binance REST Client
        try:
            api_key = cfg.BINANCE_TEST_APIKEY if testnet else cfg.BINANCE_MAIN_APIKEY
            secret = cfg.BINANCE_TEST_SECRETKEY if testnet else cfg.BINANCE_MAIN_SECRETKEY
            
            self.binance_client = Client(api_key, secret, testnet=testnet)
            # 这里的 ping 是为了预热连接
            self.binance_client.ping()
            logging.info("[TradeExecutor] Binance REST Client 就绪")
        except Exception as e:
            logging.error(f"[TradeExecutor] Binance 初始化失败: {e}")

        # 2. 初始化 Hyperliquid Exchange
        if HAVE_HYPER_SDK:
            try:
                base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
                wallet_key = cfg.HYPERTEST_WALLETKEY if testnet else cfg.HYPER_MAIN_WALLETKEY
                wallet_address = cfg.HYPERTEST_WALLET if testnet else cfg.HYPER_MAIN_WALLET
                
                # 创建 Account 对象 (本地钱包)
                account = eth_account.Account.from_key(wallet_key)
                
                # 初始化 Exchange (这一步通常会拉取一次 info，比较耗时，所以必须预加载)
                self.hyper_exchange = HyperExchange(account, base_url)
                
                # 初始化 Info 客户端（用于查询账户信息，不需要签名）
                self.hyper_info = Info(base_url, skip_ws=True)
                
                # 保存钱包地址供后续使用
                self.hyper_wallet_address = wallet_address
                
                logging.info("[TradeExecutor] Hyperliquid Exchange 就绪")
            except Exception as e:
                logging.error(f"[TradeExecutor] Hyperliquid 初始化失败: {e}")
        
        self.is_ready = True

    def _execute_binance(self, symbol, side, quantity, price=None, usdt_amount=None):
        """Binance 具体执行逻辑"""
        try:
            # 这里的 symbol 需要大写，例如 "BTCUSDT"
            symbol = symbol.upper()
            side = side.upper()
            
            if price: # 限价单
                return self.binance_client.create_order(
                    symbol=symbol,
                    side=side,
                    type='LIMIT',
                    timeInForce='GTC',
                    quantity=str(quantity),
                    price=str(price)
                )
            else: # 市价单
                if usdt_amount:
                    return self.binance_client.create_order(
                        symbol=symbol,
                        side=side,
                        type='MARKET',
                        quoteOrderQty=str(round(usdt_amount, 2))
                    )
                else:
                    return self.binance_client.create_order(
                        symbol=symbol,
                        side=side,
                        type='MARKET',
                        quantity=str(quantity)
                    )
        except BinanceAPIException as e:
            logging.error(f"[Binance下单失败] {e}")
            return {"error": str(e)}

    def _execute_hyper(self, symbol, side, quantity, price=None):
        """Hyperliquid 具体执行逻辑"""
        try:
            # Hyperliquid SDK 的 symbol 不需要 "USDT" 后缀，例如 "BTC"
            # 注意：side 在 Hyper SDK 里是布尔值 is_buy
            is_buy = (side.lower() == 'buy')
            
            if price:
                # 限价单
                return self.hyper_exchange.order(
                    name=symbol,
                    is_buy=is_buy,
                    sz=quantity,
                    limit_px=price,
                    order_type={"limit": {"tif": "Gtc"}}
                )
            else:
                # 市价单 (注意：Hyperliquid SDK 的 market_open 自带滑点设置)
                return self.hyper_exchange.market_open(
                    name=symbol,
                    is_buy=is_buy,
                    sz=quantity,
                    slippage=0.02 # 允许 2% 滑点，防止市价单失败
                )
        except Exception as e:
            logging.error(f"[Hyperliquid下单失败] {e}")
            return {"error": str(e)}

    def place_order(self, exchange, symbol, side, quantity, price=None, usdt_amount=None, async_exec=False):
        """
        统一对外接口
        :param async_exec: 是否异步执行（放入线程池，不阻塞当前线程）
        """
        if not self.is_ready:
            logging.error("交易执行器未初始化！")
            return None

        task = None
        if exchange.lower() == 'binance':
            if async_exec:
                task = self.executor.submit(self._execute_binance, symbol, side, quantity, price, usdt_amount)
            else:
                return self._execute_binance(symbol, side, quantity, price, usdt_amount)
        
        elif exchange.lower() == 'hyperliquid':
            if async_exec:
                task = self.executor.submit(self._execute_hyper, symbol, side, quantity, price)
            else:
                return self._execute_hyper(symbol, side, quantity, price)
        
        return task # 如果是异步，返回 Future 对象

    def _cancel_binance(self, symbol, order_id=None, client_order_id=None):
        """Binance 撤单具体执行逻辑"""
        try:
            # 这里的 symbol 需要大写，例如 "BTCUSDT"
            symbol = symbol.upper()
            
            # Binance 支持通过 orderId 或 origClientOrderId 撤单
            if client_order_id:
                return self.binance_client.cancel_order(
                    symbol=symbol,
                    origClientOrderId=client_order_id
                )
            elif order_id:
                return self.binance_client.cancel_order(
                    symbol=symbol,
                    orderId=int(order_id)  # Binance 的 orderId 是整数
                )
            else:
                return {"error": "order_id 或 client_order_id 必须提供其中一个"}
        except BinanceAPIException as e:
            logging.error(f"[Binance撤单失败] {e}")
            return {"error": str(e)}
        except Exception as e:
            logging.error(f"[Binance撤单异常] {e}")
            return {"error": str(e)}

    def _cancel_hyper(self, order_ids):
        """
        Hyperliquid 撤单具体执行逻辑
        
        参数:
            order_ids: 订单ID列表（可以是单个ID的列表）
        """
        try:
            # 确保 order_ids 是列表
            if not isinstance(order_ids, list):
                order_ids = [order_ids]
            
            # Hyperliquid SDK 的 cancel 方法接受 orderIds 列表
            return self.hyper_exchange.cancel(order_ids)
        except Exception as e:
            logging.error(f"[Hyperliquid撤单失败] {e}")
            return {"error": str(e)}

    def cancel_order(self, exchange, symbol=None, order_id=None, client_order_id=None, order_ids=None, async_exec=False):
        """
        统一撤单接口
        
        参数:
            exchange: 交易所名称 ("binance" 或 "hyperliquid")
            symbol: 交易对符号（Binance 必填，例如 "BTCUSDT"；Hyperliquid 不需要）
            order_id: 订单ID（Binance 使用，可选）
            client_order_id: 客户端订单ID（Binance 使用，可选）
            order_ids: 订单ID列表（Hyperliquid 使用，可以是单个ID或列表）
            async_exec: 是否异步执行（放入线程池，不阻塞当前线程）
        
        返回:
            同步执行：返回撤单结果字典
            异步执行：返回 Future 对象
        
        注意:
            - Binance: 必须提供 symbol，以及 order_id 或 client_order_id 其中之一
            - Hyperliquid: 必须提供 order_ids（可以是单个ID或列表），不需要 symbol
        """
        if not self.is_ready:
            logging.error("交易执行器未初始化！")
            return None

        task = None
        if exchange.lower() == 'binance':
            if not symbol:
                logging.error("[cancel_order] Binance 撤单需要提供 symbol 参数")
                return {"error": "Binance 撤单需要提供 symbol 参数"}
            
            if not order_id and not client_order_id:
                logging.error("[cancel_order] Binance 撤单需要提供 order_id 或 client_order_id 参数")
                return {"error": "Binance 撤单需要提供 order_id 或 client_order_id 参数"}
            
            if async_exec:
                task = self.executor.submit(self._cancel_binance, symbol, order_id, client_order_id)
            else:
                return self._cancel_binance(symbol, order_id, client_order_id)
        
        elif exchange.lower() == 'hyperliquid':
            if not order_ids:
                logging.error("[cancel_order] Hyperliquid 撤单需要提供 order_ids 参数")
                return {"error": "Hyperliquid 撤单需要提供 order_ids 参数"}
            
            if async_exec:
                task = self.executor.submit(self._cancel_hyper, order_ids)
            else:
                return self._cancel_hyper(order_ids)
        else:
            logging.error(f"[cancel_order] 不支持的交易所: {exchange}")
            return {"error": f"不支持的交易所: {exchange}"}
        
        return task  # 如果是异步，返回 Future 对象

    @staticmethod
    def Rsp_orderInsert(response: dict, exchange: str, symbol: str) -> dict:
        """
        处理下单请求的 HTTP 返回值
        :return: 标准化的结果字典 {'success': bool, 'msg': str, 'data': dict}
        """
        exchange = exchange.lower()
        symbol_str = symbol or "N/A"
        
        # 1. 基础空值检查
        if not response:
            logging.error(f"[Rsp_Insert] {exchange} {symbol_str} 未收到响应")
            return {"success": False, "msg": "No response", "data": None}

        # 2. 检查通用错误标记 (TradeExecutor 中捕获异常会返回 'error')
        if "error" in response:
            logging.error(f"[Rsp_Insert] {exchange} {symbol_str} 请求报错: {response['error']}")
            return {"success": False, "msg": response['error'], "data": response}

        # 3. 针对不同交易所的解析逻辑
        is_success = False
        order_id = None
        msg = "OK"

        try:
            if exchange == "binance":
                # Binance 成功下单通常包含 'orderId'
                if "orderId" in response:
                    is_success = True
                    order_id = response['orderId']
                    logging.info(f"[Rsp_Insert] Binance 下单请求成功! ID: {order_id}")
                else:
                    msg = f"未知响应结构: {response}"

            elif exchange == "hyperliquid":
                # Hyperliquid SDK 成功通常返回 {'status': 'ok', 'response': {...}}
                status = response.get("status")
                if status == "ok":
                    # 进一步检查内部状态 (Hyper可能返回status=ok但内部逻辑报错)
                    inner_resp = response.get("response", {})
                    if isinstance(inner_resp, dict) and inner_resp.get("type") == "order":
                        statuses = inner_resp.get("data", {}).get("statuses", [])
                        # 如果 statuses 里有 error，说明逻辑拒绝
                        if statuses and "error" in statuses[0]:
                            msg = f"Hyper 逻辑拒绝: {statuses[0]['error']}"
                        else:
                            is_success = True
                            logging.info(f"[Rsp_Insert] Hyper 下单请求成功! Response: {status}")
                    else:
                        is_success = True # 宽容处理，只要status=ok就算发送成功
                else:
                    msg = f"Hyper状态异常: {status}"

        except Exception as e:
            msg = f"解析响应异常: {str(e)}"
            logging.error(f"[Rsp_Insert] 解析异常: {e}")

        # 4. 返回最终判定
        if not is_success:
            logging.warning(f"[Rsp_Insert] {exchange} 下单指令可能失败: {msg}")
            
        return {"success": is_success, "msg": msg, "data": response}

    @staticmethod
    def Rsp_orderCancel(response: dict, exchange: str, symbol: str) -> dict:
        """
        处理撤单请求的 HTTP 返回值
        """
        exchange = exchange.lower()
        symbol_str = symbol or "N/A"
        
        if not response:
            logging.error(f"[Rsp_Cancel] {exchange} {symbol_str} 未收到响应")
            return {"success": False, "msg": "No response", "data": None}

        if "error" in response:
            logging.error(f"[Rsp_Cancel] {exchange} {symbol_str} 撤单报错: {response['error']}")
            return {"success": False, "msg": response['error'], "data": response}

        is_success = False
        msg = "OK"

        try:
            if exchange == "binance":
                # Binance 撤单成功也会返回 orderId 和 status
                if "orderId" in response:
                    is_success = True
                    logging.info(f"[Rsp_Cancel] Binance 撤单请求已发送. ID: {response['orderId']}")
                else:
                    msg = str(response)

            elif exchange == "hyperliquid":
                # Hyperliquid 撤单成功通常返回 {'status': 'ok', ...}
                if response.get("status") == "ok":
                    is_success = True
                    logging.info(f"[Rsp_Cancel] Hyper 撤单请求已发送.")
                else:
                    msg = response.get("response", "Unknown error")

        except Exception as e:
            msg = str(e)

        return {"success": is_success, "msg": msg, "data": response}

    @classmethod
    def Req_orderInsert(
        cls,
        executor_instance: 'TradeExecutor',
        exchange: str,
        api_key: str = None,
        secret_key: Optional[str] = None,
        wallet_private_key: Optional[str] = None,
        symbol: str = None,
        side: Optional[str] = None,
        order_type: Optional[str] = None,
        quantity: Optional[float] = None,
        price: Optional[float] = None,
        order_id: Optional[str] = None,
        action: str = "place",
    ) -> Dict[str, Any]:
        """
        统一下单函数（使用 TradeExecutor 实例）
        注意：api_key, secret_key, wallet_private_key 等参数已不再需要（由 TradeExecutor 管理），
        但为了保持向后兼容，仍保留这些参数。
        
        参数:
            executor_instance: TradeExecutor 实例
            其他参数：与原 Req_orderInsert 保持一致
        """
        if not executor_instance.is_ready:
            return {"success": False, "error": "交易执行器未初始化！请先调用 trade_executor.init_clients()"}
        
        logging.info(f"[Req_orderInsert] 开始下单: {exchange} {side} {symbol} {quantity}")
        
        exchange = exchange.lower()
        
        try:
            # 直接调用 TradeExecutor 的下单方法
            raw_response = executor_instance.place_order(
                exchange=exchange,
                symbol=symbol,
                side=side or "buy",
                quantity=quantity or 0.001,
                price=price,
                async_exec=False  # 默认同步执行
            )
            
            # 使用 Rsp_orderInsert 处理响应
            rsp_result = cls.Rsp_orderInsert(raw_response, exchange, symbol or "")
            
            # 转换为兼容的返回格式（保持向后兼容）
            if rsp_result["success"]:
                return {"success": True, "data": rsp_result["data"], "msg": rsp_result.get("msg", "OK")}
            else:
                return {"success": False, "error": rsp_result.get("msg", "未知错误"), "data": rsp_result.get("data")}
                
        except Exception as e:
            logging.error(f"[Req_orderInsert] 下单异常: {e}")
            return {"success": False, "error": str(e)}

    @classmethod
    def Req_orderCancel(
        cls,
        executor_instance: 'TradeExecutor',
        exchange: str,
        api_key: str = None,
        secret_key: Optional[str] = None,
        wallet_private_key: Optional[str] = None,
        symbol: Optional[str] = None,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        order_ids: Optional[list] = None,
        action: str = "cancel",
    ) -> Dict[str, Any]:
        """
        统一撤单函数（使用 TradeExecutor 实例）
        注意：api_key, secret_key, wallet_private_key 等参数已不再需要（由 TradeExecutor 管理），
        但为了保持向后兼容，仍保留这些参数。
        
        参数:
            executor_instance: TradeExecutor 实例
            exchange: 交易所名称 ("binance" 或 "hyperliquid")
            symbol: 交易对符号（Binance 必填，例如 "BTCUSDT"；Hyperliquid 不需要）
            order_id: 订单ID（Binance 使用，可选）
            client_order_id: 客户端订单ID（Binance 使用，可选）
            order_ids: 订单ID列表（Hyperliquid 使用，可以是单个ID或列表）
            其他参数：为保持向后兼容保留，但实际不使用
        
        返回:
            {"success": True/False, "data": ..., "error": ...}
        """
        if not executor_instance.is_ready:
            return {"success": False, "error": "交易执行器未初始化！请先调用 trade_executor.init_clients()"}
        
        exchange = exchange.lower()
        logging.info(f"[Req_orderCancel] 开始撤单: {exchange} symbol={symbol} order_id={order_id} client_order_id={client_order_id} order_ids={order_ids}")
        
        try:
            # 直接调用 TradeExecutor 的撤单方法
            raw_response = executor_instance.cancel_order(
                exchange=exchange,
                symbol=symbol,
                order_id=order_id,
                client_order_id=client_order_id,
                order_ids=order_ids,
                async_exec=False  # 默认同步执行
            )
            
            # 使用 Rsp_orderCancel 处理响应
            rsp_result = cls.Rsp_orderCancel(raw_response, exchange, symbol or "")
            
            # 转换为兼容的返回格式（保持向后兼容）
            if rsp_result["success"]:
                return {"success": True, "data": rsp_result["data"], "msg": rsp_result.get("msg", "OK")}
            else:
                return {"success": False, "error": rsp_result.get("msg", "未知错误"), "data": rsp_result.get("data")}
                
        except Exception as e:
            logging.error(f"[Req_orderCancel] 撤单异常: {e}")
            return {"success": False, "error": str(e)}


class InitialStateChecker:
    """
    初始状态检查器
    用于查询账户余额和持仓状态，确定策略的初始状态
    """
    def __init__(self, trade_executor: TradeExecutor):
        """
        :param trade_executor: TradeExecutor 实例，用于访问客户端
        """
        self.trade_executor = trade_executor
    
    def get_balances(self):
        """
        查询账户余额
        返回格式:
        {
            "binance": {
                "usdt": float,  # USDT 余额
                "available": float,  # 可用余额
                "locked": float  # 锁定余额
            },
            "hyperliquid": {
                "usdc": float,  # USDC 余额
                "available": float,  # 可用余额
                "locked": float  # 锁定余额（保证金）
            }
        }
        """
        balances = {
            "binance": {"usdt": 0.0, "available": 0.0, "locked": 0.0},
            "hyperliquid": {"usdc": 0.0, "available": 0.0, "locked": 0.0}
        }
        
        if not self.trade_executor.is_ready:
            logging.error("[get_balances] 交易执行器未初始化！")
            return balances
        
        # 1. 查询 Binance USDT 余额（合约账户）
        if self.trade_executor.binance_client:
            try:
                # 方法1: 查询合约账户余额（推荐，因为使用的是合约交易）
                try:
                    futures_account = self.trade_executor.binance_client.futures_account()
                    if futures_account and "assets" in futures_account:
                        for asset in futures_account["assets"]:
                            if asset.get("asset") == "USDT":
                                available_balance = float(asset.get("availableBalance", 0))
                                wallet_balance = float(asset.get("walletBalance", 0))
                                locked_balance = wallet_balance - available_balance
                                
                                balances["binance"]["usdt"] = available_balance
                                balances["binance"]["available"] = available_balance
                                balances["binance"]["locked"] = max(0, locked_balance)
                                logging.info(f"[Binance] USDT 余额: {balances['binance']['usdt']:.2f} (可用: {balances['binance']['available']:.2f}, 锁定: {balances['binance']['locked']:.2f})")
                                break
                except Exception as e:
                    logging.warning(f"[Binance] 查询合约账户余额失败 (方法1): {e}")
                    # 方法2: 尝试查询现货账户余额（备用）
                    try:
                        asset_balance = self.trade_executor.binance_client.get_asset_balance(asset='USDT')
                        if asset_balance:
                            balances["binance"]["usdt"] = float(asset_balance.get("free", 0))
                            balances["binance"]["available"] = float(asset_balance.get("free", 0))
                            balances["binance"]["locked"] = float(asset_balance.get("locked", 0))
                            logging.info(f"[Binance] USDT 余额（现货）: {balances['binance']['usdt']:.2f} (可用: {balances['binance']['available']:.2f}, 锁定: {balances['binance']['locked']:.2f})")
                    except Exception as e2:
                        logging.error(f"[Binance] 查询现货账户余额失败 (方法2): {e2}")
            except Exception as e:
                logging.error(f"[Binance] 查询余额异常: {e}")
        
        # 2. 查询 Hyperliquid USDC 余额
        if HAVE_HYPER_SDK and self.trade_executor.hyper_info:
            try:
                # 使用初始化时保存的钱包地址
                wallet_address = getattr(self.trade_executor, 'hyper_wallet_address', None)
                if not wallet_address:
                    # 备用方案：从配置获取
                    wallet_address = cfg.HYPERTEST_WALLET
                
                if wallet_address:
                    # 查询用户状态（包含余额信息）
                    user_state = self.trade_executor.hyper_info.user_state(wallet_address)
                    
                    if user_state and "marginSummary" in user_state:
                        margin_summary = user_state["marginSummary"]
                        # Hyperliquid 使用 USDC 作为保证金
                        # 查询可用余额（减去未平仓头寸的保证金）
                        available_balance_raw = margin_summary.get("availableMargin", 0)
                        # 锁定余额（已使用的保证金）
                        locked_balance_raw = margin_summary.get("totalMarginUsed", 0)
                        # 账户总值
                        account_value_raw = margin_summary.get("accountValue", 0)
                        
                        # 确保转换为 float（处理字符串类型）
                        try:
                            available_balance = float(available_balance_raw) if available_balance_raw else 0.0
                            locked_balance = float(locked_balance_raw) if locked_balance_raw else 0.0
                            account_value = float(account_value_raw) if account_value_raw else 0.0
                        except (ValueError, TypeError):
                            available_balance = 0.0
                            locked_balance = 0.0
                            account_value = 0.0
                            logging.warning(f"[Hyperliquid] 余额数据格式异常: availableMargin={available_balance_raw}, totalMarginUsed={locked_balance_raw}, accountValue={account_value_raw}")
                        
                        balances["hyperliquid"]["usdc"] = available_balance
                        balances["hyperliquid"]["available"] = available_balance
                        balances["hyperliquid"]["locked"] = locked_balance
                        
                        logging.info(f"[Hyperliquid] USDC 余额: {balances['hyperliquid']['usdc']:.2f} (可用: {balances['hyperliquid']['available']:.2f}, 锁定: {balances['hyperliquid']['locked']:.2f}, 账户总值: {account_value:.2f})")
                    else:
                        logging.warning("[Hyperliquid] 无法从 user_state 中获取余额信息")
                else:
                    logging.warning("[Hyperliquid] 无法获取钱包地址")
            except Exception as e:
                logging.error(f"[Hyperliquid] 查询余额异常: {e}")
                import traceback
                logging.debug(traceback.format_exc())
        
        return balances
    
    def Req_Investment_position(self, symbol_binance: str = None, symbol_hyper: str = None, strategy_machine=None):
        """
        查询持仓并确定初始状态
        
        :param symbol_binance: Binance 交易对符号，如 "BTCUSDT"，默认从配置读取
        :param symbol_hyper: Hyperliquid 交易对符号，如 "BTC"，默认从配置读取
        :param strategy_machine: 策略状态机实例（必需），用于更新状态机状态
        :return: tuple (bool, str) - (是否成功, 初始状态)
        
        逻辑：
        - 如果两边都无持仓：状态设为 OpenCondition，返回 True
        - 如果两边持有对冲仓位（Binance 空 + Hyper 多）：状态设为 CloseCondition，返回 True
        - 如果只有单边持仓：打印警告，返回 False
        """
        # 从 Simple_strategy 导入状态常量
        from Simple_strategy import StrategyState
        
        if strategy_machine is None:
            logging.error("[Req_Investment_position] strategy_machine 参数不能为 None")
            return False, None
        
        symbol_binance = symbol_binance or cfg.BINANCE_SYMBOL
        symbol_hyper = symbol_hyper or cfg.HYPER_SYMBOL
        
        binance_position = None
        hyper_position = None
        
        # 1. 查询 Binance 持仓
        if self.trade_executor.binance_client:
            try:
                # 查询合约持仓信息
                positions = self.trade_executor.binance_client.futures_position_information(symbol=symbol_binance)
                if positions and len(positions) > 0:
                    pos = positions[0]
                    position_amt = float(pos.get("positionAmt", 0))
                    if abs(position_amt) > 1e-8:  # 有持仓（考虑浮点误差）
                        binance_position = {
                            "amount": position_amt,
                            "side": "long" if position_amt > 0 else "short",
                            "entry_price": float(pos.get("entryPrice", 0)),
                            "unrealized_pnl": float(pos.get("unRealizedProfit", 0))
                        }
                        logging.info(f"[Binance] 持仓: {binance_position['side']} {abs(binance_position['amount'])} @ {binance_position['entry_price']}")
                    else:
                        logging.info(f"[Binance] 无持仓")
            except Exception as e:
                logging.error(f"[Binance] 查询持仓失败: {e}")
        
        # 2. 查询 Hyperliquid 持仓
        if HAVE_HYPER_SDK and self.trade_executor.hyper_info:
            try:
                wallet_address = getattr(self.trade_executor, 'hyper_wallet_address', None)
                if not wallet_address:
                    wallet_address = cfg.HYPERTEST_WALLET
                
                if wallet_address:
                    user_state = self.trade_executor.hyper_info.user_state(wallet_address)
                    if user_state and "assetPositions" in user_state:
                        positions = user_state["assetPositions"]
                        # 查找指定币种的持仓
                        for pos in positions:
                            if pos.get("position", {}).get("coin") == symbol_hyper:
                                position_data = pos.get("position", {})
                                size = float(position_data.get("szi", 0))  # 持仓数量
                                if abs(size) > 1e-8:  # 有持仓
                                    hyper_position = {
                                        "amount": size,
                                        "side": "long" if size > 0 else "short",
                                        "entry_price": float(position_data.get("entryPx", 0)),
                                        "unrealized_pnl": float(pos.get("unrealizedPnl", 0))
                                    }
                                    logging.info(f"[Hyperliquid] 持仓: {hyper_position['side']} {abs(hyper_position['amount'])} @ {hyper_position['entry_price']}")
                                    break
                        
                        if not hyper_position:
                            logging.info(f"[Hyperliquid] 无持仓")
            except Exception as e:
                logging.error(f"[Hyperliquid] 查询持仓失败: {e}")
                import traceback
                logging.debug(traceback.format_exc())
        
        # 3. 判断持仓状态并设置策略状态
        has_binance = binance_position is not None
        has_hyper = hyper_position is not None
        
        # 情况1: 两边都无持仓
        if not has_binance and not has_hyper:
            logging.info("[持仓检查] 两边都无持仓，状态设为 OpenCondition")
            initial_state = StrategyState.OpenCondition
            strategy_machine.update_state(initial_state)
            return True, initial_state
        
        # 情况2: 两边都有持仓
        if has_binance and has_hyper:
            # 检查是否为对冲仓位：Binance 空 + Hyper 多
            binance_side = binance_position["side"]
            hyper_side = hyper_position["side"]
            
            if binance_side == "short" and hyper_side == "long":
                initial_state = StrategyState.CloseCondition
                logging.info("[持仓检查] 检测到对冲仓位（Binance 空 + Hyper 多），状态设为 CloseCondition")
                strategy_machine.update_state(initial_state)
                return True, initial_state
            else:
                # 其他组合视为单边持仓
                print(f"警告，单边持仓: Binance={binance_side}, Hyper={hyper_side}")
                logging.warning(f"[持仓检查] 非对冲仓位组合: Binance={binance_side}, Hyper={hyper_side}")
                return False, None
        
        # 情况3: 只有单边持仓
        print("警告，单边持仓")
        if has_binance:
            logging.warning(f"[持仓检查] 只有 Binance 持仓: {binance_position['side']} {abs(binance_position['amount'])}")
        if has_hyper:
            logging.warning(f"[持仓检查] 只有 Hyperliquid 持仓: {hyper_position['side']} {abs(hyper_position['amount'])}")
        return False, None