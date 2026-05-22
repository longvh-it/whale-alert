import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    # Telegram
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    admin_chat_id: str = os.getenv("ADMIN_CHAT_ID", "")

    # Thresholds (USD)
    min_trade_size: float = float(os.getenv("MIN_TRADE_SIZE_USD", 100_000))
    min_position_size: float = float(os.getenv("MIN_POSITION_SIZE_USD", 500_000))
    min_liquidation_size: float = float(os.getenv("MIN_LIQUIDATION_SIZE_USD", 200_000))
    min_pnl_alert: float = float(os.getenv("MIN_PNL_ALERT_USD", 50_000))

    # System
    db_path: str = os.getenv("DB_PATH", "whale_bot.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    alert_cooldown: int = int(os.getenv("ALERT_COOLDOWN_SECONDS", 300))

    # Hyperliquid
    hl_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    hl_api_url: str = "https://api.hyperliquid.xyz/info"

    # Signal bot
    signal_channel_id: str = os.getenv("SIGNAL_CHANNEL_ID", "")
    auto_signal_enabled: bool = os.getenv("AUTO_SIGNAL_ENABLED", "false").lower() == "true"
    auto_signal_sl_pct: float = float(os.getenv("AUTO_SIGNAL_SL_PCT", "3"))    # SL % từ entry
    auto_signal_rr: float = float(os.getenv("AUTO_SIGNAL_RR", "2"))             # R:R ratio (TP3)
    auto_signal_min_usd: float = float(os.getenv("AUTO_SIGNAL_MIN_USD", "500000"))
    auto_signal_min_usd_alt: float = float(os.getenv("AUTO_SIGNAL_MIN_USD_ALT", "200000"))
    auto_signal_major_coins: list = None  # populated below

    # Trend Detector (4h klines)
    trend_poll_interval: int = int(os.getenv("TREND_POLL_INTERVAL", "900"))   # 15 phút
    trend_min_score: int = int(os.getenv("TREND_MIN_SCORE", "3"))              # indicator tối thiểu (3/3 TF)
    trend_atr_sl_mult: float = float(os.getenv("TREND_ATR_SL_MULT", "1.5"))   # SL = entry ± ATR * mult
    trend_atr_tp_mult: float = float(os.getenv("TREND_ATR_TP_MULT", "4.5"))   # TP3 = entry ± ATR * mult; TP1=1.5×ATR=1:1 R:R

    # Binance Futures WS
    binance_enabled: bool = os.getenv("BINANCE_ENABLED", "true").lower() == "true"
    binance_symbols: list = None  # populated below

    # Bybit Futures WS
    bybit_enabled: bool = os.getenv("BYBIT_ENABLED", "true").lower() == "true"
    bybit_symbols: list = None    # populated below

    # Coinglass REST (optional API key)
    coinglass_api_key: str = os.getenv("COINGLASS_API_KEY", "")
    coinglass_poll_interval: int = int(os.getenv("COINGLASS_POLL_INTERVAL", "300"))

    # OI Spike Detector
    oi_spike_threshold: float = float(os.getenv("OI_SPIKE_THRESHOLD", "5.0"))
    oi_poll_interval: int = int(os.getenv("OI_POLL_INTERVAL", "300"))

    # Funding Rate Detector
    funding_extreme_high: float = float(os.getenv("FUNDING_EXTREME_HIGH", "0.10"))
    funding_extreme_low: float = float(os.getenv("FUNDING_EXTREME_LOW", "-0.05"))
    funding_poll_interval: int = int(os.getenv("FUNDING_POLL_INTERVAL", "3600"))

    # Confluence Scorer
    confluence_enabled: bool = os.getenv("CONFLUENCE_ENABLED", "true").lower() == "true"
    confluence_window: int = int(os.getenv("CONFLUENCE_WINDOW", "300"))
    confluence_min_sources: int = int(os.getenv("CONFLUENCE_MIN_SOURCES", "3"))
    confluence_min_score_weighted: int = int(os.getenv("CONFLUENCE_MIN_SCORE_WEIGHTED", "5"))

    # Multi-timeframe Trend Detector
    trend_poll_interval_1h: int = int(os.getenv("TREND_POLL_INTERVAL_1H", "300"))
    trend_poll_interval_4h: int = int(os.getenv("TREND_POLL_INTERVAL_4H", "900"))
    trend_poll_interval_1d: int = int(os.getenv("TREND_POLL_INTERVAL_1D", "3600"))

    # TP1 Reversal — move SL to entry
    tp1_reversal_move_sl_enabled: bool = os.getenv("TP1_REVERSAL_MOVE_SL_ENABLED", "true").lower() == "true"
    tp1_reversal_min_score: int = int(os.getenv("TP1_REVERSAL_MIN_SCORE", "2"))
    tp1_reversal_warn_score: int = int(os.getenv("TP1_REVERSAL_WARN_SCORE", "1"))

    # Auto-cut on trend reversal
    reversal_cut_enabled: bool = os.getenv("REVERSAL_CUT_ENABLED", "true").lower() == "true"
    reversal_min_score: int = int(os.getenv("REVERSAL_MIN_SCORE", "4"))
    reversal_grace_minutes: int = int(os.getenv("REVERSAL_GRACE_MINUTES", "60"))
    reversal_alt_min_score: int = int(os.getenv("REVERSAL_ALT_MIN_SCORE", "5"))
    reversal_check_interval: int = int(os.getenv("REVERSAL_CHECK_INTERVAL", "15"))

    # Auto-cancel stale PENDING signals
    signal_pending_timeout_hours: float = float(os.getenv("SIGNAL_PENDING_TIMEOUT_HOURS", "4.0"))

    # Daily loss limit
    daily_sl_limit: int = int(os.getenv("DAILY_SL_LIMIT", "3"))
    daily_loss_limit_enabled: bool = os.getenv("DAILY_LOSS_LIMIT_ENABLED", "true").lower() == "true"

    # Signal quality score
    signal_min_quality_score: int = int(os.getenv("SIGNAL_MIN_QUALITY_SCORE", "60"))

    # DOM (Order Book) Analysis
    dom_enabled: bool = os.getenv("DOM_ENABLED", "true").lower() == "true"
    dom_coins: list = None  # populated below
    dom_wall_min_usd: float = float(os.getenv("DOM_WALL_MIN_USD", "1000000"))
    dom_wall_distance_max_pct: float = float(os.getenv("DOM_WALL_DISTANCE_MAX_PCT", "1.5"))
    dom_bid_ask_bullish: float = float(os.getenv("DOM_BID_ASK_BULLISH", "1.5"))
    dom_bid_ask_bearish: float = float(os.getenv("DOM_BID_ASK_BEARISH", "0.67"))
    dom_absorption_pct_threshold: float = float(os.getenv("DOM_ABSORPTION_PCT_THRESHOLD", "25"))
    dom_book_depth_levels: int = int(os.getenv("DOM_BOOK_DEPTH_LEVELS", "20"))

    # Ecosystem Call
    ecosystem_enabled: bool = os.getenv("ECOSYSTEM_ENABLED", "true").lower() == "true"
    ecosystem_volume_spike_min: float = float(os.getenv("ECOSYSTEM_VOLUME_SPIKE_MIN", "2.5"))
    ecosystem_call_min_trend_score: int = int(os.getenv("ECOSYSTEM_CALL_MIN_TREND_SCORE", "2"))
    ecosystem_signal_quality_penalty: int = int(os.getenv("ECOSYSTEM_SIGNAL_QUALITY_PENALTY", "15"))
    ecosystem_map: dict = None  # populated below

    # Trend Scanner (proactive daily scan — volume spike + multi-TF trend)
    scan_enabled: bool = os.getenv("SCAN_ENABLED", "false").lower() == "true"
    scan_daily_max: int = int(os.getenv("SCAN_DAILY_MAX", "2"))
    scan_volume_min: float = float(os.getenv("SCAN_VOLUME_MIN", "2.0"))
    scan_min_trend_score: int = int(os.getenv("SCAN_MIN_TREND_SCORE", "2"))
    scan_coin_cooldown_hours: int = int(os.getenv("SCAN_COIN_COOLDOWN_HOURS", "24"))

    # Periodic Trend Scanner (time-based, không cần whale trigger)
    trend_scan_enabled: bool = os.getenv("TREND_SCAN_ENABLED", "false").lower() == "true"
    trend_scan_interval: int = int(os.getenv("TREND_SCAN_INTERVAL", "1800"))          # giây, mặc định 30 phút
    trend_scan_daily_max: int = int(os.getenv("TREND_SCAN_DAILY_MAX", "2"))
    trend_scan_min_quality: int = int(os.getenv("TREND_SCAN_MIN_QUALITY", "35"))      # thấp hơn whale (50) vì đã cần 3/3 TF
    trend_scan_coin_cooldown_hours: int = int(os.getenv("TREND_SCAN_COIN_COOLDOWN_HOURS", "4"))
    trend_scan_coins: list = None  # populated below

    # OKX Futures WS
    okx_enabled: bool = os.getenv("OKX_ENABLED", "false").lower() == "true"
    okx_symbols: list = None  # populated below

    # TradingView Webhook
    tv_webhook_enabled: bool = os.getenv("TV_WEBHOOK_ENABLED", "false").lower() == "true"
    tv_webhook_port: int = int(os.getenv("TV_WEBHOOK_PORT", "8080"))
    tv_webhook_secret: str = os.getenv("TV_WEBHOOK_SECRET", "")

    def __post_init__(self):
        if self.binance_symbols is None:
            raw = os.getenv("BINANCE_SYMBOLS", "BTC,ETH,SOL,BNB,DOGE,AVAX")
            self.binance_symbols = [s.strip() for s in raw.split(",") if s.strip()]
        if self.bybit_symbols is None:
            raw = os.getenv("BYBIT_SYMBOLS", "BTC,ETH,SOL")
            self.bybit_symbols = [s.strip() for s in raw.split(",") if s.strip()]
        if self.auto_signal_major_coins is None:
            raw = os.getenv("AUTO_SIGNAL_MAJOR_COINS", "BTC,ETH")
            self.auto_signal_major_coins = [s.strip().upper() for s in raw.split(",") if s.strip()]
        if self.dom_coins is None:
            raw = os.getenv("DOM_COINS", "BTC,ETH,SOL,ARB,DOGE,AVAX")
            self.dom_coins = [s.strip().upper() for s in raw.split(",") if s.strip()]
        if self.trend_scan_coins is None:
            raw = os.getenv("TREND_SCAN_COINS", "BTC,ETH,SOL,BNB,XRP,DOGE,AVAX,ADA")
            self.trend_scan_coins = [s.strip().upper() for s in raw.split(",") if s.strip()]
        if self.okx_symbols is None:
            raw = os.getenv("OKX_SYMBOLS", "BTC,ETH,SOL,BNB,DOGE,XRP,AVAX")
            self.okx_symbols = [s.strip() for s in raw.split(",") if s.strip()]
        if self.ecosystem_map is None:
            self.ecosystem_map = {
                "SOL":    ["JUP", "RAY", "BONK", "JTO", "PYTH", "WIF", "DRIFT"],
                "ETH":    ["ARB", "OP", "MATIC", "LDO", "RPL"],
                "ARB":    ["GMX", "PENDLE", "RDNT"],
                "OP":     ["VELO", "SNX"],
                "FET":    ["AGIX", "TAO", "RENDER", "OCEAN"],
                "TAO":    ["FET", "AGIX", "RENDER"],
                "RENDER": ["FET", "AGIX", "TAO"],
                "BNB":    ["CAKE", "TWT", "XVS"],
                "AAVE":   ["UNI", "CRV", "MKR", "COMP"],
                "UNI":    ["AAVE", "CRV", "SUSHI"],
                "AXS":    ["SAND", "MANA", "GALA", "IMX"],
                "IMX":    ["AXS", "GODS"],
                "ATOM":   ["OSMO", "INJ", "TIA", "DYDX"],
                "INJ":    ["ATOM", "OSMO", "TIA"],
                "MKR":    ["AAVE", "COMP"],
            }


config = Config()
