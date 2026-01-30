"""
WebSocket 客户端模块
包含 BaseWebSocket 基类和交易所特定的 WebSocket 实现
"""
import json
import threading
import time
import logging
from typing import Dict, List, Callable, Optional, Any
import websocket
import config as cfg
from DataBuffer import Level, L2Book
from binance.client import Client


# ==================== WebSocket 基类 ====================
class BaseWebSocket:
    """
    通用 WebSocket 基类
    特点：自动重连、心跳维护、线程安全
    """
    def __init__(self, url: str, exchange_name: str):
        self.url = url
        self.exchange_name = exchange_name
        self.ws: Optional[websocket.WebSocketApp] = None
        self.wst: Optional[threading.Thread] = None  # WebSocket 运行线程
        
        # 状态控制
        self._running = False
        self._connected = threading.Event()
        self._lock = threading.Lock()
        
        # 订阅与回调
        self.subscriptions: List[Dict] = []
        self.callbacks: Dict[str, List[Callable]] = {}
        
        # 向后兼容：保留 ws_ready 属性
        self.ws_ready = self._connected

    def start(self):
        """启动连接（非阻塞）"""
        if self._running:
            return
        self._running = True
        self.wst = threading.Thread(target=self._run_forever_loop, daemon=True)
        self.wst.start()
        logging.info(f"[{self.exchange_name}] WebSocket 线程已启动")

    def stop(self):
        """优雅停止"""
        self._running = False
        if self.ws:
            self.ws.close()
        if self.wst:
            self.wst.join(timeout=2)
        logging.info(f"[{self.exchange_name}] WebSocket 已停止")

    def _run_forever_loop(self):
        """核心循环：负责连接和断线重连"""
        while self._running:
            try:
                logging.info(f"[{self.exchange_name}] 正在连接: {self.url}")
                self.ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                # 阻塞调用，直到断开
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logging.error(f"[{self.exchange_name}] 运行异常: {e}")
            
            # 断开后的处理
            self._connected.clear()
            if self._running:
                logging.warning(f"[{self.exchange_name}] 连接断开，3秒后重连...")
                time.sleep(3)

    def _on_open(self, ws):
        logging.info(f"[{self.exchange_name}] 连接成功")
        self._connected.set()
        self.on_connected()  # 子类钩子

    def _on_close(self, ws, close_status_code, close_msg):
        # 注意：不要在这里调用 start()，让 _run_forever_loop 处理循环
        logging.warning(f"[{self.exchange_name}] 连接关闭: {close_msg}")
        self._connected.clear()

    def _on_error(self, ws, error):
        logging.error(f"[{self.exchange_name}] 错误: {error}")

    def _on_message(self, ws, message):
        # 子类需重写此方法
        pass

    def on_connected(self):
        """连接成功后的钩子，用于发送订阅"""
        # 重新发送所有订阅
        with self._lock:
            for sub in self.subscriptions:
                self.send_json(sub)

    def send_json(self, data: dict):
        """线程安全的发送方法"""
        if self.ws and self._connected.is_set():
            try:
                self.ws.send(json.dumps(data))
            except Exception as e:
                logging.error(f"[{self.exchange_name}] 发送失败: {e}")


# ==================== Hyperliquid WebSocket ====================
class HyperliquidWebSocket(BaseWebSocket):
    """
    Hyperliquid WebSocket 客户端
    支持在同一个连接中同时订阅 l2Book 和 user 流
    """
    def __init__(self, url: str, wallet_address: str = None, user_callback: Callable = None):
        """
        :param url: WebSocket URL
        :param wallet_address: 钱包地址，如果提供则自动订阅 user 流
        :param user_callback: 用户流回调函数，如果提供则自动注册
        """
        super().__init__(url, "Hyperliquid")
        self.wallet_address = wallet_address
        self.user_callback = user_callback
        self._ping_thread: Optional[threading.Thread] = None
        
        # 如果提供了用户回调，提前注册
        if user_callback:
            if "user" not in self.callbacks:
                self.callbacks["user"] = []
            self.callbacks["user"].append(user_callback)

    def start(self):
        super().start()
        # 启动 Ping 线程
        if not self._ping_thread or not self._ping_thread.is_alive():
            self._ping_thread = threading.Thread(target=self._run_ping_thread, daemon=True)
            self._ping_thread.start()

    def stop(self):
        super().stop()
        if self._ping_thread:
            self._ping_thread.join(timeout=1)

    def on_connected(self):
        # Hyperliquid 需要在连接后立刻发送订阅
        super().on_connected()
        
        # 如果配置了钱包地址，自动订阅 User events（在同一个连接中）
        if self.wallet_address:
            # 使用内部方法直接发送订阅消息，不重复注册回调
            msg = {"method": "subscribe", "subscription": {"type": "user", "user": self.wallet_address}}
            with self._lock:
                if msg not in self.subscriptions:
                    self.subscriptions.append(msg)
            self.send_json(msg)
            logging.info(f"[{self.exchange_name}] 已自动订阅用户流: {self.wallet_address}")

    def subscribe_l2(self, coin: str, callback: Callable):
        """订阅 L2 订单簿"""
        msg = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}
        if "l2Book" not in self.callbacks:
            self.callbacks["l2Book"] = []
        self.callbacks["l2Book"].append(callback)
        with self._lock:
            # 避免重复订阅
            if msg not in self.subscriptions:
                self.subscriptions.append(msg)
        self.send_json(msg)

    def subscribe_user(self, user_address: str, callback: Callable = None):
        """订阅用户事件"""
        msg = {"method": "subscribe", "subscription": {"type": "user", "user": user_address}}
        if callback:
            if "user" not in self.callbacks:
                self.callbacks["user"] = []
            self.callbacks["user"].append(callback)
        with self._lock:
            # 避免重复订阅
            if msg not in self.subscriptions:
                self.subscriptions.append(msg)
        self.send_json(msg)

    def subscribe(self, sub_type: str, coin: str, callback: Optional[Callable] = None):
        """向后兼容的订阅方法"""
        if sub_type == "l2Book":
            self.subscribe_l2(coin, callback)
        elif sub_type == "user":
            self.subscribe_user(coin, callback)
        else:
            msg = {"method": "subscribe", "subscription": {"type": sub_type, "coin": coin}}
            channel = sub_type
            if callback:
                if channel not in self.callbacks:
                    self.callbacks[channel] = []
                self.callbacks[channel].append(callback)
            with self._lock:
                if msg not in self.subscriptions:
                    self.subscriptions.append(msg)
            self.send_json(msg)

    def _on_message(self, ws, message):
        # 处理连接建立消息
        if message.strip() == "Websocket connection established.":
            logging.info(f"[{self.exchange_name}] WebSocket 连接已建立")
            return

        try:
            data = json.loads(message)
            channel = data.get("channel")
            
            # 心跳响应
            if channel == "pong":
                return

            # 数据分发
            if channel == "l2Book" and "l2Book" in self.callbacks:
                for cb in self.callbacks["l2Book"]:
                    try:
                        cb(data)
                    except Exception as e:
                        logging.error(f"[{self.exchange_name}] 回调执行出错: {e}")
            
            elif channel == "user" and "user" in self.callbacks:
                for cb in self.callbacks["user"]:
                    try:
                        cb(data)
                    except Exception as e:
                        logging.error(f"[{self.exchange_name}] 回调执行出错: {e}")
            elif channel and channel in self.callbacks:
                # 通用回调处理
                for cb in self.callbacks[channel]:
                    try:
                        cb(data)
                    except Exception as e:
                        logging.error(f"[{self.exchange_name}] 回调执行出错: {e}")

        except json.JSONDecodeError:
            logging.debug(f"[{self.exchange_name}] 非JSON消息: {message}")

    def _run_ping_thread(self):
        """Hyperliquid 建议应用层 Ping"""
        while self._running:
            time.sleep(50)
            if self._connected.is_set():
                self.send_json({"method": "ping"})

    # ---解析 L2 订单簿 ---
    @staticmethod
    def parse_l2book(raw_data: dict, depth: int = 10) -> L2Book:
        """
        解析 Hyperliquid 的 l2Book 消息
        :param raw_data: 原始 JSON dict（如 on_message 收到的 data）
        :param depth: 保留前 N 档（默认 10）
        :return: L2Book 对象
        """
        data = raw_data.get("data", {})
        coin = data.get("coin", "UNKNOWN")
        levels = data.get("levels", [[], []])  # [bids, asks]
        timestamp = data.get("time", 0)

        def parse_levels(side_levels):
            result = []
            for lvl in side_levels[:depth]:
                try:
                    price = float(lvl["px"])
                    size = float(lvl["sz"])
                    orders = int(lvl.get("n", 0))
                    result.append(Level(price, size, orders))
                except (ValueError, KeyError) as e:
                    logging.warning(f"解析 level 失败: {lvl} -> {e}")
            return result

        bids = parse_levels(levels[0])   # 买盘（从高到低）
        asks = parse_levels(levels[1])   # 卖盘（从低到高）

        return L2Book(coin, bids, asks, timestamp)


# ==================== Binance WebSocket ====================
class BinanceWebSocket(BaseWebSocket):
    def __init__(
        self,
        symbol: str,
        url: str = cfg.BINANCE_WS_URL,           # ← 默认现货
        depth: int = 10,                      # ← 新增：深度档数
        interval_ms: int = 100,                # ← 新增：更新频率（合约推荐 100ms，现货可以用 1000ms）
        stream_type: str = "market"            # ← "market" (行情) 或 "user" (用户订单)
    ):
        self.symbol = symbol.lower()
        base_url = url.rstrip("/")
        self.depth = depth
        self.interval_ms = interval_ms
        self.stream_type = stream_type
        
        # 构建流 URL
        if stream_type == "market":
            # 根据 URL 自动判断是现货还是合约，生成正确的 stream 名称
            if "data-stream.binance.vision" in base_url:
                # 现货专属后门（裸连最快）
                stream_name = f"{self.symbol}@depth{self.depth}"
            else:
                # 合约（包括 fstream / fstream3 / fstream.binancefuture.com 等）
                stream_name = f"{self.symbol}@depth{self.depth}@{self.interval_ms}ms"
            stream_url = f"{base_url}/ws/{stream_name}"
        else:
            # User stream URL 应该在创建时传入完整 URL（包含 listen_key）
            # 格式：wss://fstream.binancefuture.com/ws/{listen_key}
            stream_url = base_url
        
        super().__init__(stream_url, f"Binance-{stream_type}")
        self.base_url = base_url

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)
            
            # 处理 Ping/Pong (Binance 原生支持，但为了保险)
            if "ping" in data:
                self.send_json({"pong": data["ping"]})
                return
            
            # 区分 User Stream 和 Market Stream
            if self.stream_type == "user":
                # 用户流可能包含多种事件类型：executionReport, outboundAccountPosition 等
                event_type = data.get("e")
                if event_type == "executionReport":
                    # 订单执行报告
                    if "order" in self.callbacks:
                        for cb in self.callbacks["order"]:
                            try:
                                cb(data)
                            except Exception as e:
                                logging.error(f"[{self.exchange_name}] 回调执行出错: {e}")
                else:
                    # 其他用户流事件（账户更新等）
                    logging.debug(f"[{self.exchange_name}] 收到用户流事件: {event_type}")
            
            elif self.stream_type == "market":
                # Binance深度流格式 (@depth20@100ms 返回完整深度数据)
                # 格式1: 直接深度更新 {"e":"depthUpdate","s":"BTCUSDT",...}
                # 格式2: 多流格式 {"stream":"btcusdt@depth20@100ms","data":{...}}
                stream_data = data
                if "stream" in data and "data" in data:
                    stream_data = data["data"]
                
                if stream_data.get("e") == "depthUpdate" or ("bids" in stream_data or "asks" in stream_data):
                    # 解析订单簿（@depth20流每次返回完整快照）
                    book = self.parse_l2book(stream_data, depth=5)
                    
                    # 触发回调
                    if "depth" in self.callbacks:
                        for cb in self.callbacks["depth"]:
                            try:
                                cb(book)
                            except Exception as e:
                                logging.error(f"[{self.exchange_name}] 回调执行出错: {e}")
                    
        except json.JSONDecodeError as e:
            logging.debug(f"[{self.exchange_name}] JSON解析失败: {message[:100]}")
        except Exception as e:
            logging.error(f"[{self.exchange_name}] 消息处理出错: {e}")

    def on_connected(self):
        """Binance 深度流通过 URL 直接订阅，不需要发送订阅消息"""
        # Binance 深度流的订阅信息已经在 URL 中，连接后自动接收数据
        # 只需要确保回调已注册即可
        logging.info(f"[{self.exchange_name}] 连接成功，深度流已自动订阅")

    def subscribe_depth(self, symbol: str, callback: Callable):
        """订阅深度（仅注册回调，订阅信息在 URL 中）"""
        # Binance 深度流通过 URL 订阅，不需要发送 SUBSCRIBE 消息
        # 只需要注册回调即可
        if "depth" not in self.callbacks:
            self.callbacks["depth"] = []
        self.callbacks["depth"].append(callback)
        logging.info(f"[{self.exchange_name}] 深度回调已注册: {symbol}")

    def subscribe(self, callback: Optional[Callable] = None):
        """向后兼容的订阅方法：订阅订单簿深度流"""
        # Binance 深度流通过 URL 订阅，只需要注册回调
        if callback:
            if "depth" not in self.callbacks:
                self.callbacks["depth"] = []
            self.callbacks["depth"].append(callback)
            logging.info(f"[{self.exchange_name}] 深度回调已注册")

    @staticmethod
    def parse_l2book(raw_data: dict, depth: int = 10) -> L2Book:
        """
        解析 Binance 的深度更新消息
        :param raw_data: Binance深度更新数据 (@depth20流返回完整快照)
        :param depth: 保留前 N 档
        :return: L2Book 对象
        """
        symbol = raw_data.get("s", "UNKNOWN")
        timestamp = raw_data.get("E", 0)  # 事件时间
        
        # Binance格式: [price, quantity]
        # @depth20流返回: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        # 增量流返回: {"b": [[price, qty], ...], "a": [[price, qty], ...]}
        bids_raw = raw_data.get("bids", raw_data.get("b", []))
        asks_raw = raw_data.get("asks", raw_data.get("a", []))
        
        def parse_levels(side_levels):
            result = []
            for lvl in side_levels[:depth]:
                try:
                    price = float(lvl[0])
                    size = float(lvl[1])
                    if size > 0:  # 只添加有效深度
                        orders = 1  # Binance不提供订单数
                        result.append(Level(price, size, orders))
                except (ValueError, IndexError) as e:
                    logging.warning(f"解析 level 失败: {lvl} -> {e}")
            return result
        
        bids = parse_levels(bids_raw)
        asks = parse_levels(asks_raw)
        
        # 确保排序正确
        bids.sort(key=lambda x: x.price, reverse=True)  # 买盘从高到低
        asks.sort(key=lambda x: x.price)  # 卖盘从低到高
        
        return L2Book(symbol, bids, asks, timestamp)


# ===================== Binance 用户流 ListenKey 管理 =====================
class BinanceListenKeyManager:
    """管理 Binance ListenKey 的申请和续期"""
    def __init__(self, client: Client, testnet: bool = True):
        self.client = client
        self.testnet = testnet
        self.listen_key: Optional[str] = None
        self._running = False
        self._keep_alive_thread: Optional[threading.Thread] = None

    def get_listen_key(self) -> str:
        """申请 ListenKey"""
        response = None
        try:
            if self.testnet:
                # 测试网使用 futures_testnet
                response = self.client.futures_stream_get_listen_key()
            else:
                # 主网使用 futures
                response = self.client.futures_stream_get_listen_key()
            
            # 处理不同的返回格式
            if isinstance(response, dict):
                # 标准格式：{"listenKey": "xxx"}
                self.listen_key = response.get('listenKey') or response.get('listenkey')
            elif isinstance(response, str):
                # 直接返回字符串格式
                self.listen_key = response
            else:
                # 尝试转换为字符串
                self.listen_key = str(response)
            
            if not self.listen_key:
                raise ValueError("ListenKey 为空")
            
            logging.info(f"[Binance] ListenKey 申请成功: {self.listen_key[:20]}...")
            return self.listen_key
        except Exception as e:
            logging.error(f"[Binance] ListenKey 申请失败: {e}")
            if response is not None:
                logging.error(f"[Binance] 响应类型: {type(response)}, 响应内容: {response}")
            raise

    def keep_alive(self):
        """延长 ListenKey 有效期"""
        if not self.listen_key:
            return
        
        try:
            if self.testnet:
                self.client.futures_stream_keepalive(listenKey=self.listen_key)
            else:
                self.client.futures_stream_keepalive(listenKey=self.listen_key)
            logging.info(f"[Binance] ListenKey 续期成功")
        except Exception as e:
            logging.error(f"[Binance] ListenKey 续期失败: {e}")
            # 续期失败，尝试重新申请
            try:
                self.get_listen_key()
            except Exception as e2:
                logging.error(f"[Binance] ListenKey 重新申请失败: {e2}")

    def _keep_alive_loop(self):
        """后台线程：每30分钟延长一次 ListenKey"""
        while self._running:
            time.sleep(1800)  # 30分钟 = 1800秒
            if self._running and self.listen_key:
                self.keep_alive()

    def start_keep_alive(self):
        """启动续期线程"""
        if self._keep_alive_thread and self._keep_alive_thread.is_alive():
            return
        
        self._running = True
        self._keep_alive_thread = threading.Thread(target=self._keep_alive_loop, daemon=True)
        self._keep_alive_thread.start()
        logging.info("[Binance] ListenKey 续期线程已启动")

    def stop(self):
        """停止续期线程并删除 ListenKey"""
        self._running = False
        
        if self.listen_key:
            try:
                if self.testnet:
                    self.client.futures_stream_close(listenKey=self.listen_key)
                else:
                    self.client.futures_stream_close(listenKey=self.listen_key)
                logging.info("[Binance] ListenKey 已删除")
            except Exception as e:
                logging.error(f"[Binance] ListenKey 删除失败: {e}")
        
        if self._keep_alive_thread:
            self._keep_alive_thread.join(timeout=2)


# ===================== Binance 用户流 =====================
class BinanceUserStream:
    """
    使用 BinanceWebSocket 和 ListenKey 管理器的用户流实现
    
    回调函数由外部传入，不在此类内部定义
    """
    def __init__(self, testnet=True, base_url: str = None, callback: Callable = None):
        """
        :param testnet: 是否使用测试网
        :param base_url: WebSocket 基础 URL
        :param callback: 用户流消息回调函数，接收订单执行报告等消息
        """
        self.testnet = testnet
        self.base_url = base_url or (cfg.BINANCE_WSCONTRACT_URL if not testnet else cfg.BINANCE_WSCONTRACT_URL)
        self.callback = callback
        
        # 创建 REST Client
        api_key = cfg.BINANCE_TEST_APIKEY if testnet else cfg.BINANCE_MAIN_APIKEY
        secret_key = cfg.BINANCE_TEST_SECRETKEY if testnet else cfg.BINANCE_MAIN_SECRETKEY
        self.client = Client(api_key=api_key, api_secret=secret_key, testnet=testnet)
        
        # ListenKey 管理器
        self.key_manager = BinanceListenKeyManager(self.client, testnet=testnet)
        
        # WebSocket 客户端
        self.ws_client: Optional[BinanceWebSocket] = None

    def start(self):
        """
        启动用户流
        
        流程：
        1. REST Client 已在 __init__ 中创建
        2. 申请 ListenKey
        3. 启动续期线程（每30分钟延长一次）
        4. 构建 WebSocket URL（包含 ListenKey）
        5. 创建 WebSocket 客户端
        6. 注册回调
        7. 启动 WebSocket
        """
        # 1. 申请 ListenKey（使用 REST Client）
        listen_key = self.key_manager.get_listen_key()
        
        # 2. 启动续期线程（每30分钟延长一次 ListenKey）
        self.key_manager.start_keep_alive()
        
        # 3. 构建 WebSocket URL（包含 listen_key）
        ws_url = f"{self.base_url.rstrip('/')}/ws/{listen_key}"
        logging.info(f"[Binance] 用户流 WebSocket URL: {ws_url[:50]}...")
        
        # 4. 创建 WebSocket 客户端
        self.ws_client = BinanceWebSocket(
            symbol="",  # 用户流不需要 symbol
            url=ws_url,  # 传入完整 URL（包含 listen_key）
            stream_type="user"
        )
        
        # 5. 注册回调（使用外部传入的回调函数）
        if self.callback:
            def on_order_message(data):
                self.callback(data)
            
            if "order" not in self.ws_client.callbacks:
                self.ws_client.callbacks["order"] = []
            self.ws_client.callbacks["order"].append(on_order_message)
        
        # 6. 启动 WebSocket
        self.ws_client.start()
        
        net_label = "测试网" if self.testnet else "主网"
        logging.info(f"[Binance] 用户数据流已启动（{net_label}）")

    def stop_User_stream(self):
        """停止用户流"""
        if self.ws_client:
            self.ws_client.stop()
            self.ws_client = None
        
        self.key_manager.stop()
        logging.info("[Binance] 用户数据流已停止")


# ==================== 交易所管理器 ====================
class ExchangeManager:
    """
    管理多个交易所的WebSocket连接
    
    优化说明：
    - BaseWebSocket.start() 内部已经创建了独立线程运行 WebSocket
    - 因此不需要 ExchangeManager 再创建额外的线程
    - 直接调用 start() 即可，它是非阻塞的
    """
    def __init__(self):
        self.exchanges: Dict[str, BaseWebSocket] = {}

    def add_exchange(self, name: str, ws_client: BaseWebSocket):
        """添加交易所"""
        self.exchanges[name] = ws_client

    def start_all(self):
        """启动所有交易所（非阻塞，每个交易所内部已有独立线程）"""
        for name, client in self.exchanges.items():
            logging.info(f"正在启动 [{name}]...")
            client.start()
            logging.info(f"[{name}] 启动命令已发送")

    def stop_all(self):
        """停止所有交易所"""
        for name, client in self.exchanges.items():
            logging.info(f"正在停止 [{name}]...")
            client.stop()

    def wait_all_ready(self, timeout: int = 10):
        """等待所有交易所连接就绪"""
        for name, client in self.exchanges.items():
            if hasattr(client, 'ws_ready'):
                if not client.ws_ready.wait(timeout=timeout):
                    logging.warning(f"[{name}] 连接就绪超时")
                else:
                    logging.info(f"[{name}] 连接就绪")  
