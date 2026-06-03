import os
from dotenv import load_dotenv

load_dotenv()

# MEXC API
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")

# URLs
FUTURES_BASE_URL = "https://contract.mexc.com"
SPOT_BASE_URL = "https://api.mexc.com"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Demo mode
IS_DEMO = os.getenv("IS_DEMO", "true").lower() == "true"

# Trading settings
DEFAULT_PAIR_SPOT = "BTCUSDT"
DEFAULT_PAIR_FUTURES = "BTC_USDT"
RISK_PERCENT = 1.5
LEVERAGE = 10
TIMEFRAME = "Min15"