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

    def __post_init__(self):
        if self.binance_symbols is None:
            raw = os.getenv("BINANCE_SYMBOLS", "BTC,ETH,SOL,BNB,DOGE,AVAX")
            self.binance_symbols = [s.strip() for s in raw.split(",") if s.strip()]
        if self.bybit_symbols is None:
            raw = os.getenv("BYBIT_SYMBOLS", "BTC,ETH,SOL")
            self.bybit_symbols = [s.strip() for s in raw.split(",") if s.strip()]


config = Config()
