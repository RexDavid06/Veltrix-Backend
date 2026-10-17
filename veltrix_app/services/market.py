from __future__ import annotations

import os
import threading
import time
from decimal import Decimal
from typing import Any

from django.db import models
from django.utils import timezone

# In-memory TTL cache for the market snapshot. The snapshot can be relatively
# expensive (it may fan out to a quote provider), and it is consulted by both
# the market endpoint and the demo order/watchlist endpoints, so a short-lived
# cache avoids hammering the provider on every request.
_SNAPSHOT_TTL_SECONDS = 60.0
_snapshot_cache: list[Any] = []  # [timestamp, quotes]
_snapshot_lock = threading.Lock()


class ServiceMeta(type):
    pass


DEFAULT_SYMBOLS = [
    ("AAPL", "Apple Inc."),
    ("MSFT", "Microsoft"),
    ("NVDA", "NVIDIA"),
    ("BTC-USD", "Bitcoin"),
    ("ETH-USD", "Ethereum"),
    ("EURUSD=X", "EUR/USD"),
    ("GBPUSD=X", "GBP/USD"),
    ("XAUUSD=X", "Gold"),
    ("^IXIC", "NASDAQ Composite"),
]


def _fallback_quotes() -> list[dict[str, Any]]:
    base_prices = {
        "AAPL": 214.42,
        "MSFT": 424.61,
        "NVDA": 127.15,
        "BTC-USD": 66314.90,
        "ETH-USD": 3419.12,
        "EURUSD=X": 1.0844,
        "GBPUSD=X": 1.2716,
        "XAUUSD=X": 2348.77,
        "^IXIC": 18214.60,
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


def _fetch_quotes_uncached() -> list[dict[str, Any]]:
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


def fetch_market_snapshot() -> list[dict[str, Any]]:
    """Return the market snapshot, served from a short-lived in-memory cache."""
    now = time.monotonic()
    with _snapshot_lock:
        cached_at, quotes = (None, None)
        if _snapshot_cache:
            cached_at, quotes = _snapshot_cache[0], _snapshot_cache[1]
        if quotes is not None and cached_at is not None and (now - cached_at) < _SNAPSHOT_TTL_SECONDS:
            return quotes

    fresh = _fetch_quotes_uncached()
    with _snapshot_lock:
        _snapshot_cache[:] = [now, fresh]
    return fresh


# Common client-facing symbol aliases mapped to the canonical symbols used by
# the market snapshot. Keeps order price lookups working for the labels shown in
# the UI (e.g. "EUR/USD", "NAS100") without inventing new prices.
SYMBOL_ALIASES = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "XAU/USD": "XAUUSD=X",
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "EUR-USD": "EURUSD=X",
    "GBP-USD": "GBPUSD=X",
    "XAU-USD": "XAUUSD=X",
    "NAS100": "^IXIC",
    "NASDAQ": "^IXIC",
    "NASDAQ100": "^IXIC",
    "US100": "^IXIC",
}


def normalize_symbol(symbol: str | None) -> str:
    """Map a UI symbol to its canonical market symbol (best effort)."""
    s = (symbol or "").strip().upper()
    return SYMBOL_ALIASES.get(s, s)


# Reverse of SYMBOL_ALIASES for the primary UI label of each canonical symbol.
# Used so the watchlist can report the same symbol string the market table
# displays, keeping the star/highlight state consistent for forex and indices.
UI_SYMBOLS = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "XAUUSD=X": "XAU/USD",
    "BTC-USD": "BTC/USD",
    "ETH-USD": "ETH/USD",
    "^IXIC": "NAS100",
}


def display_symbol(symbol: str | None) -> str:
    """Map a canonical symbol back to the label shown in the UI."""
    s = (symbol or "").strip().upper()
    return UI_SYMBOLS.get(s, s)


def guess_asset_class(symbol: str) -> str:
    s = (symbol or "").upper()
    if s.startswith("^"):
        return "Index"
    if s.startswith("XAU") or s.startswith("XAG"):
        return "Commodity"
    if s.endswith("=X"):
        return "Forex"
    if s.endswith("-USD") or s.endswith("USDT"):
        return "Crypto"
    return "Equity"


def quote_for_symbol(symbol: str) -> dict[str, Any] | None:
    """Return the current snapshot quote for a (possibly aliased) symbol."""
    canonical = normalize_symbol(symbol)
    for quote in fetch_market_snapshot():
        if quote.get("symbol") == canonical:
            return quote
    return None
