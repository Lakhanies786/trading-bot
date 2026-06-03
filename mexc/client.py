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

    def __init__(self):
        self.base    = SPOT_BASE_URL
        self.api_key = MEXC_API_KEY
        self.secret  = MEXC_SECRET_KEY

    def get_ticker(self, symbol):
        r = requests.get(
            f"{self.base}/api/v3/ticker/price",
            params={"symbol": symbol}
        )
        return r.json()

    def get_klines(self, symbol, interval="15m", limit=100):
        r = requests.get(
            f"{self.base}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit}
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
            "symbol":     symbol,
            "side":       side,
            "type":       order_type,
            "quantity":   quantity,
            "timestamp":  int(time.time() * 1000)
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

    def __init__(self):
        self.base    = FUTURES_BASE_URL
        self.api_key = MEXC_API_KEY
        self.secret  = MEXC_SECRET_KEY

    def get_ticker(self, symbol):
        r = requests.get(
            f"{self.base}/api/v1/contract/ticker",
            params={"symbol": symbol}
        )
        return r.json()

    def get_klines(self, symbol, interval="Min15", limit=100):
        r = requests.get(
            f"{self.base}/api/v1/contract/kline/{symbol}",
            params={"interval": interval, "limit": limit}
        )
        return r.json()

    def get_account_assets(self):
        ts       = str(int(time.time() * 1000))
        sign_str = self.api_key + ts
        sig      = hmac.new(
            self.secret.encode(), sign_str.encode(), hashlib.sha256
        ).hexdigest()
        headers  = {
            "ApiKey":       self.api_key,
            "Request-Time": ts,
            "Signature":    sig
        }
        r = requests.get(
            f"{self.base}/api/v1/private/account/assets",
            headers=headers
        )
        return r.json()

    def place_order(self, symbol, side, order_type, vol,
                    price=None, leverage=10, open_type=1):
        import json
        payload = {
            "symbol":   symbol,
            "side":     side,
            "openType": open_type,
            "type":     order_type,
            "vol":      vol,
            "leverage": leverage
        }
        if order_type == 1 and price:
            payload["price"] = price
        body = json.dumps(payload)
        ts   = str(int(time.time() * 1000))
        sig  = hmac.new(
            self.secret.encode(), (self.api_key + ts + body).encode(),
            hashlib.sha256
        ).hexdigest()
        headers = {
            "ApiKey":       self.api_key,
            "Request-Time": ts,
            "Signature":    sig,
            "Content-Type": "application/json"
        }
        r = requests.post(
            f"{self.base}/api/v1/private/order/submit",
            headers=headers, data=body
        )
        return r.json()
