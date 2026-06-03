import time
import hmac
import hashlib
import requests
import os
from dotenv import load_dotenv

load_dotenv()

SPOT_BASE_URL    = "https://api.mexc.com"
FUTURES_BASE_URL = "https://contract.mexc.com"
MEXC_API_KEY     = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY  = os.getenv("MEXC_SECRET_KEY")


class MEXCSpotClient:

    def _init_(self):
        self.base    = SPOT_BASE_URL
        self.api_key = MEXC_API_KEY
        self.secret  = MEXC_SECRET_KEY

    def get_ticker(self, symbol: str):
        r = requests.get(
            f"{self.base}/api/v3/ticker/price",
            params={"symbol": symbol}
        )
        return r.json()

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 100):
        interval_map = {
            "1m": "1m", "5m": "5m", "15m": "15m",
            "30m": "30m", "1h": "60m", "4h": "4h", "1d": "1d"
        }
        mexc_interval = interval_map.get(interval, interval)
        r = requests.get(
            f"{self.base}/api/v3/klines",
            params={"symbol": symbol, "interval": mexc_interval, "limit": limit}
        )
        data = r.json()
        if isinstance(data, dict):
            r2 = requests.get(
                f"{self.base}/api/v3/klines",
                params={"symbol": symbol, "interval": interval, "limit": limit}
            )
            data = r2.json()
        return data

    def get_orderbook(self, symbol: str, limit: int = 50):
        r = requests.get(
            f"{self.base}/api/v3/depth",
            params={"symbol": symbol, "limit": limit}
        )
        return r.json()

    def get_account(self):
        params = {"timestamp": int(time.time() * 1000)}
        query  = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig    = hmac.new(
            self.secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        params["signature"] = sig
        r = requests.get(
            f"{self.base}/api/v3/account",
            headers={"X-MEXC-APIKEY": self.api_key},
            params=params
        )
        return r.json()

    def place_order(self, symbol, side, order_type, quantity, price=None):
        params = {
            "symbol":    symbol,
            "side":      side,
            "type":      order_type,
            "quantity":  quantity,
            "timestamp": int(time.time() * 1000)
        }
        if order_type == "LIMIT" and price:
            params["price"]       = price
            params["timeInForce"] = "GTC"
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        params["signature"] = hmac.new(
            self.secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        r = requests.post(
            f"{self.base}/api/v3/order",
            headers={"X-MEXC-APIKEY": self.api_key},
            params=params
        )
        return r.json()


class MEXCFuturesClient:

    def _init_(self):
        self.base    = FUTURES_BASE_URL
        self.api_key = MEXC_API_KEY
        self.secret  = MEXC_SECRET_KEY

    def get_ticker(self, symbol: str):
        r = requests.get(
            f"{self.base}/api/v1/contract/ticker",
            params={"symbol": symbol}
        )
        return r.json()

    def get_klines(self, symbol: str, interval: str = "Min15", limit: int = 100):
        r = requests.get(
            f"{self.base}/api/v1/contract/kline/{symbol}",
            params={"interval": interval, "limit": limit}
        )
        return r.json()

    def get_account_assets(self):
        ts      = str(int(time.time() * 1000))
        sig_str = self.api_key + ts
        sig     = hmac.new(
            self.secret.encode(), sig_str.encode(), hashlib.sha256
        ).hexdigest()
        headers = {
            "ApiKey":       self.api_key,
            "Request-Time": ts,
            "Signature":    sig
        }
        r = requests.get(
            f"{self.base}/api/v1/private/account/assets",
            headers=headers
        )
        return r.json()