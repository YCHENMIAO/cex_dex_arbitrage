# CEX-DEX 跨交易所套利系统

一个基于 Python 的实时跨交易所套利交易系统，支持 Binance 和 Hyperliquid 之间的价差套利。

## 📋 项目简介

本项目实现了 Binance（中心化交易所）和 Hyperliquid（去中心化交易所）之间的实时套利交易系统。系统通过 WebSocket 实时监控两个交易所的价格差异，当价差扣除手续费后仍有利可图时，自动执行对冲交易。

### 核心功能

- 🔄 **实时价格监控**：通过 WebSocket 实时获取两个交易所的订单簿数据
- 📊 **价差计算**：自动计算考虑手续费后的净价差
- 🤖 **自动交易**：基于状态机自动执行开仓和平仓操作
- 📈 **订单管理**：支持订单状态跟踪、超时撤单、部分成交处理
- 🔐 **多交易所支持**：统一接口支持 Binance 和 Hyperliquid

🏗️ 项目结构

```
cex_dex_arbitrage/
├── main.py                 # 主程序入口，初始化系统并启动所有服务
├── config.py               # 配置文件，包含 API 密钥、交易对等配置
├── DataBuffer.py           # 数据缓冲模块，处理订单簿数据和价差计算
├── Market_data.py          # 行情数据处理模块，处理深度行情回调
├── trade_engine.py         # 交易执行引擎，统一管理下单和撤单
├── Simple_strategy.py      # 策略状态机，管理交易状态转换
├── websocket_cex_dex.py    # WebSocket 客户端模块，管理交易所连接
└── CODE_REVIEW.md          # 代码审查报告
```

🔧 主要函数说明

### main.py

#### `_binance_sign(secret_key: str, params: Dict[str, Any]) -> Dict[str, Any]`
对 Binance API 参数进行签名。

**参数：**
- `secret_key`: Binance 密钥
- `params`: 需要签名的参数字典

**返回：** 包含签名的参数字典

#### `create_binance_user_callback(strategy_machine)`
创建 Binance 用户流回调函数，处理订单状态更新。

**参数：**
- `strategy_machine`: 策略状态机实例

**返回：** 回调函数，用于处理 Binance 用户流消息

#### `create_hyper_user_callback(strategy_machine)`
创建 Hyperliquid 用户流回调函数，处理订单状态更新。

**参数：**
- `strategy_machine`: 策略状态机实例

**返回：** 回调函数，用于处理 Hyperliquid 用户流消息

#### `start_all_user_streams(strategy_machine, testnet=True)`
启动所有交易所的用户流（Binance 和 Hyperliquid）。

**参数：**
- `strategy_machine`: 策略状态机实例
- `testnet`: 是否使用测试网

**返回：** Binance 用户流实例

---

### DataBuffer.py

#### `Level` (NamedTuple)
订单簿档位数据结构。

**字段：**
- `price`: 价格
- `size`: 数量
- `orders`: 订单数量

#### `L2Book`
订单簿数据结构。

**方法：**
- `mid_price()`: 计算中间价
- `spread()`: 计算买卖价差
- `depth(side, levels)`: 计算累计深度（前 N 档总挂单量）

#### `PriceBoard`
轻量级价格板，用于实时价差计算。线程安全，使用锁保证数据一致性。

**方法：**
- `update(exchange, bid, ask)`: 更新交易所价格（线程安全）
- `get_price(exchange, side)`: 获取指定交易所和方向的价格（"bid" 或 "ask"）
- `get_spread()`: 获取实时价差（不考虑手续费），返回 `(spread_buy_bin, spread_buy_hyp)`
- `get_spread_with_fees()`: 获取考虑手续费后的净价差，返回 `(net_spread_buy_bin, net_spread_buy_hyp)`

#### `DataBuffer`
数据缓冲处理类。

**类方法：**
- `open_signal(coin) -> bool`: 检查开仓信号，当考虑手续费后的价差大于阈值时返回 True
- `close_signal(coin) -> bool`: 检查平仓信号，当考虑手续费后的价差大于阈值时返回 True
- `push_neworder_book(exchange, book)`: 更新订单簿数据到价格板

---

### Market_data.py

#### `Depth_Marketdata`
深度行情数据处理类。

**类方法：**
- `on_hyperliquid_l2book(book)`: Hyperliquid 订单簿回调，静默更新价格板
- `on_binance_l2book(book)`: Binance 订单簿回调，更新数据并立即计算价差，触发策略信号检查
- `on_hyperliquid_raw(raw_data)`: 接收 Hyperliquid 原始数据并解析为订单簿
- `on_trades(data)`: 处理交易数据

---

### trade_engine.py

#### `TradeExecutor`
交易执行器，统一管理不同交易所的下单和撤单操作。

**方法：**

##### `init_clients(testnet=True)`
初始化交易接口连接，建立长连接。

**参数：**
- `testnet`: 是否使用测试网

##### `place_order(exchange, symbol, side, quantity, price=None, usdt_amount=None, async_exec=False)`
统一下单接口。

**参数：**
- `exchange`: 交易所名称（"binance" 或 "hyperliquid"）
- `symbol`: 交易对符号
- `side`: 买卖方向（"BUY" 或 "SELL"）
- `quantity`: 数量
- `price`: 限价单价格（可选，None 表示市价单）
- `usdt_amount`: 市价单 USDT 金额（可选）
- `async_exec`: 是否异步执行

**返回：** 同步执行返回响应字典，异步执行返回 Future 对象

##### `cancel_order(exchange, symbol=None, order_id=None, client_order_id=None, order_ids=None, async_exec=False)`
统一撤单接口。

**参数：**
- `exchange`: 交易所名称
- `symbol`: 交易对符号（Binance 必填）
- `order_id`: 订单ID（Binance 使用）
- `client_order_id`: 客户端订单ID（Binance 使用）
- `order_ids`: 订单ID列表（Hyperliquid 使用）
- `async_exec`: 是否异步执行

**返回：** 同步执行返回响应字典，异步执行返回 Future 对象

##### `Rsp_orderInsert(response, exchange, symbol) -> dict`
处理下单请求的 HTTP 返回值，标准化响应格式。

**返回：** `{"success": bool, "msg": str, "data": dict}`

##### `Rsp_orderCancel(response, exchange, symbol) -> dict`
处理撤单请求的 HTTP 返回值。

**返回：** `{"success": bool, "msg": str, "data": dict}`

##### `Req_orderInsert(executor_instance, exchange, symbol, side, order_type, quantity, price, ...) -> dict`
统一下单函数（使用 TradeExecutor 实例）。

**返回：** `{"success": bool, "data": dict, "msg": str}`

##### `Req_orderCancel(executor_instance, exchange, symbol, order_id, ...) -> dict`
统一撤单函数（使用 TradeExecutor 实例）。

**返回：** `{"success": bool, "data": dict, "error": str}`

#### `InitialStateChecker`
初始状态检查器，用于查询账户余额和持仓状态。

**方法：**

##### `get_balances() -> dict`
查询账户余额。

**返回：**
```python
{
    "binance": {
        "usdt": float,      # USDT 余额
        "available": float, # 可用余额
        "locked": float     # 锁定余额
    },
    "hyperliquid": {
        "usdc": float,      # USDC 余额
        "available": float, # 可用余额
        "locked": float     # 锁定余额（保证金）
    }
}
```

##### `Req_Investment_position(symbol_binance, symbol_hyper, strategy_machine) -> tuple`
查询持仓并确定初始状态。

**参数：**
- `symbol_binance`: Binance 交易对符号
- `symbol_hyper`: Hyperliquid 交易对符号
- `strategy_machine`: 策略状态机实例（必需）

**返回：** `(bool, str)` - (是否成功, 初始状态)

**逻辑：**
- 如果两边都无持仓：状态设为 `OpenCondition`
- 如果两边持有对冲仓位（Binance 空 + Hyper 多）：状态设为 `CloseCondition`，并将持仓数量同步到状态机
- 如果只有单边持仓：返回 False（需要手动调整持仓）

---

### Simple_strategy.py

#### `StrategyState`
策略状态常量类。

**状态：**
- `OpenCondition`: 开仓条件检查状态
- `OpenLeg1Waiting`: 开仓 Leg1 等待成交（Hyperliquid 挂单中）
- `OpenLeg1Canceling`: 开仓 Leg1 撤单中
- `OpenLeg2Waiting`: 开仓 Leg2 等待成交（Binance 对冲中）
- `OpenLeg2Chasing`: 开仓 Leg2 追单中（Binance 部分成交后继续追单）
- `CloseCondition`: 平仓条件检查状态
- `CloseLeg1Waiting`: 平仓 Leg1 等待成交（Hyperliquid 挂单中）
- `CloseLeg1Canceling`: 平仓 Leg1 撤单中
- `CloseLeg2Waiting`: 平仓 Leg2 等待成交（Binance 平仓中）
- `CloseLeg2Chasing`: 平仓 Leg2 追单中（Binance 部分成交后继续追单）

#### `StrategyStateMachine`
策略状态机，管理交易状态转换和订单执行。

**方法：**

##### `__init__(trade_executor)`
初始化策略状态机。

**参数：**
- `trade_executor`: TradeExecutor 实例

##### `get_state() -> str`
获取当前状态。

##### `update_state(new_state)`
更新状态。

##### `on_order_update_logic(exchange, event_type, order_id, filled_qty=0.0)`
处理订单更新回调，驱动状态机转换。

**参数：**
- `exchange`: 交易所名称（"Binance" 或 "Hyperliquid"）
- `event_type`: 订单事件类型（"ALL_traded", "PARTIAL_filled_canceled", "ALL_canceled"）
- `order_id`: 订单ID
- `filled_qty`: 累计成交数量（注意：这是累计量，不是增量）

##### `on_tick_check()`
超时检查，如果订单超时则发起撤单。每秒执行一次，检查当前活跃订单是否超时。

##### `check_and_execute_open(open_signal_func)`
检查开仓信号并执行。

**参数：**
- `open_signal_func`: 开仓信号函数，返回 bool

##### `check_and_execute_close(close_signal_func)`
检查平仓信号并执行。

**参数：**
- `close_signal_func`: 平仓信号函数，返回 bool

---

### websocket_cex_dex.py

#### `BaseWebSocket`
通用 WebSocket 基类，提供自动重连、心跳维护、线程安全等功能。

**方法：**
- `start()`: 启动连接（非阻塞）
- `stop()`: 优雅停止
- `send_json(data)`: 线程安全的发送方法
- `on_connected()`: 连接成功后的钩子，用于发送订阅

#### `HyperliquidWebSocket`
Hyperliquid WebSocket 客户端，支持在同一个连接中同时订阅 l2Book 和 user 流。

**方法：**
- `subscribe_l2(coin, callback)`: 订阅 L2 订单簿
- `subscribe_user(user_address, callback)`: 订阅用户事件
- `parse_l2book(raw_data, depth=10) -> L2Book`: 解析 Hyperliquid 的 l2Book 消息

#### `BinanceWebSocket`
Binance WebSocket 客户端。

**方法：**
- `subscribe_depth(symbol, callback)`: 订阅深度（仅注册回调）
- `parse_l2book(raw_data, depth=10) -> L2Book`: 解析 Binance 的深度更新消息

#### `BinanceListenKeyManager`
管理 Binance ListenKey 的申请和续期。

**方法：**
- `get_listen_key() -> str`: 申请 ListenKey
- `keep_alive()`: 延长 ListenKey 有效期
- `start_keep_alive()`: 启动续期线程（每30分钟延长一次）
- `stop()`: 停止续期线程并删除 ListenKey

#### `BinanceUserStream`
使用 BinanceWebSocket 和 ListenKey 管理器的用户流实现。

**方法：**
- `start()`: 启动用户流
- `stop_User_stream()`: 停止用户流

#### `ExchangeManager`
管理多个交易所的 WebSocket 连接。

**方法：**
- `add_exchange(name, ws_client)`: 添加交易所
- `start_all()`: 启动所有交易所（非阻塞）
- `stop_all()`: 停止所有交易所
- `wait_all_ready(timeout=10)`: 等待所有交易所连接就绪

---

## ⚙️ 配置说明

### config.py

主要配置项：

```python
# Hyperliquid WebSocket URL
MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"  # 主网
TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"  # 测试网
WS_URL = TESTNET_WS_URL  # 默认使用测试网（可根据需要切换）

# Hyperliquid 钱包地址和私钥
HYPERTEST_WALLET = "0x..."
HYPERTEST_WALLETKEY = "0x..."

# Binance API 密钥
BINANCE_TEST_APIKEY = "..."
BINANCE_TEST_SECRETKEY = "..."

# 交易对
BINANCE_SYMBOL = "BTCUSDT"  # Binance 交易对符号
HYPER_SYMBOL = "BTC"        # Hyperliquid 交易对符号

# 手续费配置（费率，例如 0.0002 表示 0.02%）
BINANCE_MAKER_FEE = 0.0002  # Binance maker 手续费率（挂单）
BINANCE_TAKER_FEE = 0.0004  # Binance taker 手续费率（吃单）
HYPER_MAKER_FEE = 0.0002    # Hyperliquid maker 手续费率
HYPER_TAKER_FEE = 0.0004    # Hyperliquid taker 手续费率

# 套利信号阈值
MIN_SPREAD_THRESHOLD = 0.0  # 最小价差阈值（扣除手续费后的净价差），单位：价格单位
# 注意：如果价差扣除手续费后仍大于此阈值，才会触发交易信号
```

⚠️ **安全提示**：请勿将包含真实 API 密钥的配置文件提交到版本控制系统。建议使用环境变量或加密配置文件。

## 🚀 使用说明

### 1. 安装依赖

```bash
pip install python-binance hyperliquid-python-sdk websocket-client pandas pynacl eth-account
```

**依赖说明：**
- `python-binance`: Binance API 客户端
- `hyperliquid-python-sdk`: Hyperliquid Python SDK
- `websocket-client`: WebSocket 客户端库
- `pandas`: 数据处理（部分功能使用）
- `pynacl`: 用于 Hyperliquid 签名
- `eth-account`: 以太坊账户管理（Hyperliquid 需要）

### 2. 配置 API 密钥

编辑 `config.py`，填入你的 API 密钥和钱包信息。

### 3. 运行程序

```bash
python main.py
```

### 4. 程序流程

1. **初始化交易执行器**：建立与 Binance 和 Hyperliquid 的连接
2. **创建策略状态机**：初始化交易状态管理
3. **检查账户状态**：查询余额和持仓，确定初始状态
4. **启动 WebSocket 连接**：连接两个交易所的行情流和用户流
5. **订阅行情数据**：开始接收实时订单簿数据
6. **自动交易**：当检测到套利机会时，自动执行交易

### 5. 停止程序

按 `Ctrl+C` 停止程序，系统会优雅关闭所有连接。

## 📊 交易策略

### 策略核心思想

系统采用**流动性优先策略**：永远先在 Hyperliquid（流动性较差）挂限价单（maker），成交后再去 Binance（流动性较好）吃单对冲（taker）。这样可以在流动性较差的交易所获得更优的价格，同时在流动性好的交易所快速完成对冲。

### 开仓逻辑

1. **方向**：Hyperliquid 买入（maker）+ Binance 卖出（taker）
2. **条件**：当 `Hyper.bid_price - Binance.ask_price` 扣除手续费后仍大于阈值时触发
   - 计算公式：`net_spread = Hyper.bid_price * (1 - hyper_maker_fee) - Binance.ask_price * (1 + binance_taker_fee)`
   - 只有当 `net_spread > MIN_SPREAD_THRESHOLD` 时才会触发
3. **执行流程**：
   - **Leg1**: Hyperliquid 买入（限价单，maker），在 bid 价格挂单
   - **Leg2**: 等待 Leg1 成交后，在 Binance 卖出（限价/市价单，taker）进行对冲
   - **追单机制**：如果 Leg2 部分成交或超时，会自动追单（前3次限价单，第4次转为市价单）

### 平仓逻辑

1. **方向**：Hyperliquid 卖出（maker）+ Binance 买入（taker）
2. **条件**：当 `Binance.bid_price - Hyper.ask_price` 扣除手续费后仍大于阈值时触发
   - 计算公式：`net_spread = Binance.bid_price * (1 - binance_maker_fee) - Hyper.ask_price * (1 + hyper_taker_fee)`
   - 只有当 `net_spread > MIN_SPREAD_THRESHOLD` 时才会触发
3. **执行流程**：
   - **Leg1**: Hyperliquid 卖出（限价单，maker），在 ask 价格挂单
   - **Leg2**: 等待 Leg1 成交后，在 Binance 买入（限价/市价单，taker）进行平仓
   - **追单机制**：如果 Leg2 部分成交或超时，会自动追单（前3次限价单，第4次转为市价单）

### 风险控制

- **订单超时**：每个订单设置超时时间（默认5秒），超时自动撤单
- **部分成交处理**：如果订单部分成交后被撤销，系统会自动追单完成剩余数量
- **精度处理**：自动处理价格和数量的精度（根据交易所要求）

## ⚠️ 注意事项

1. **风险提示**：
   - 本系统涉及真实资金交易，请充分测试后再使用
   - 建议先在测试网环境运行
   - 注意市场波动和流动性风险

2. **安全建议**：
   - 使用环境变量或加密配置文件管理 API 密钥
   - 定期检查账户余额和持仓
   - 设置合理的止损和风险控制

3. **性能优化**：
   - 系统使用多线程处理 WebSocket 连接
   - 订单执行支持异步模式，避免阻塞
   - 价格板使用线程锁保证数据一致性

4. **已知问题**：
   - 详见 `CODE_REVIEW.md` 中的代码审查报告
   - 建议优先修复严重问题（P0级别）

## 📝 日志说明

系统使用 Python `logging` 模块记录日志，默认级别为 `INFO`。主要日志类型：

- `[TradeExecutor]`: 交易执行相关日志
- `[状态变更]`: 策略状态机状态转换
- `[订单回报]`: 订单状态更新
- `[价差]`: 价差计算和套利信号
- `[Binance]` / `[Hyperliquid]`: 交易所连接和消息处理

## 🔗 相关文档

- [CODE_REVIEW.md](CODE_REVIEW.md): 代码审查报告，包含已知问题和修复建议

## 📄 许可证

本项目仅供学习和研究使用。使用本系统进行交易的风险由使用者自行承担。

