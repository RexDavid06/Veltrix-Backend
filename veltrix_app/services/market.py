from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

from django.db import models
from django.utils import timezone


class ServiceMeta(type):
    pass


DEFAULT_SYMBOLS = [
    ("AAPL", "Apple Inc."),
    ("MSFT", "Microsoft"),
    ("NVDA", "NVIDIA"),
    ("BTC-USD", "Bitcoin"),
    ("ETH-USD", "Ethereum"),
    ("EURUSD=X", "EUR/USD"),
    ("XAUUSD=X", "Gold"),
]


def _fallback_quotes() -> list[dict[str, Any]]:
    base_prices = {
        "AAPL": 214.42,
        "MSFT": 424.61,
        "NVDA": 127.15,
        "BTC-USD": 66314.90,
        "ETH-USD": 3419.12,
        "EURUSD=X": 1.0844,
        "XAUUSD=X": 2348.77,
    }
    return [
        {
            "symbol": symbol,
            "name": name,
            "price": base_prices.get(symbol, 100.0),
            "change_percent": 0.62 if symbol != "BTC-USD" else 1.23,
        }
        for symbol, name in DEFAULT_SYMBOLS
    ]


def fetch_market_snapshot() -> list[dict[str, Any]]:
    use_live = os.environ.get("MARKET_USE_LIVE", "0") == "1"
    if not use_live:
        return _fallback_quotes()

    try:
        import yfinance as yf
    except ImportError:
        return _fallback_quotes()

    quotes: list[dict[str, Any]] = []
    for symbol, name in DEFAULT_SYMBOLS:
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            price = float(getattr(info, "last_price", 0) or ticker.history(period="1d").tail(1)["Close"].iloc[0])
            prev_close = float(getattr(info, "previous_close", price) or price)
            change = ((price - prev_close) / prev_close * 100) if prev_close else 0.0
            quotes.append({
                "symbol": symbol,
                "name": name,
                "price": price,
                "change_percent": change,
            })
        except Exception:
            continue

    if not quotes:
        return _fallback_quotes()
    return quotes
