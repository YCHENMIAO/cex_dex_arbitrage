"""
集中配置本项目使用的全局常量，供 main.py 等模块导入使用。
"""

# Hyperliquid
MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
WS_URL = TESTNET_WS_URL  # 切换测试网：TESTNET_WS_URL

# hyper 钱包
HYPERTEST_WALLET = ""
HYPERTEST_WALLETKEY = ""

HYPERTEST_WALLET_MYWALLET=""
HYPER_MAIN_WALLET = ""
HYPER_MAIN_WALLETKEY = "你的主网KEY"

HYPE_PLACE_PREP_URL = "https://api.hyperliquid-testnet.xyz"

# Binance
# 现货市场
BINANCE_WS_URL = "wss://data-stream.binance.vision"  # 默认使用现货市场（更稳定）
# 合约市场
BINANCE_WSCONTRACT_URL = "wss://fstream.binancefuture.com"

BINANCE_TEST_APIKEY = ""
BINANCE_TEST_SECRETKEY = ""

BINANCE_MAIN_APIKEY = "主网 key"
BINANCE_MAIN_SECRETKEY = "主网 secret"
BINANCE_SPOT_URL = "https://testnet.binance.vision"

# 交易对
BINANCE_SYMBOL = "BTCUSDT"  # Binance 交易对符号
HYPER_SYMBOL = "BTC"        # 合约（示例），这里先那一个合约测试。多了不好管理。

# 手续费配置（费率，例如 0.0002 表示 0.02%）
BINANCE_MAKER_FEE = 0.0002  # Binance maker 手续费率（挂单）
BINANCE_TAKER_FEE = 0.0004  # Binance taker 手续费率（吃单）
HYPER_MAKER_FEE = 0.0002    # Hyperliquid maker 手续费率
HYPER_TAKER_FEE = 0.0004   # Hyperliquid taker 手续费率

# 套利信号阈值
MIN_SPREAD_THRESHOLD = 0.0  # 最小价差阈值（扣除手续费后），单位：价格单位
# 注意：如果价差扣除手续费后仍大于此阈值，才会触发交易信号