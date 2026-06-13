#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Thin, cached, retrying client around the public Polymarket APIs.

Endpoints are centralised here. Where a path or field name is not 100% certain
it is flagged with a ``# VERIFY`` comment -- check it against
https://docs.polymarket.com/api-reference and adapt rather than guessing.

Hosts used (all must be reachable / on the egress allowlist):
  - data-api.polymarket.com   activity, trades, positions, value
  - gamma-api.polymarket.com  market metadata, tags, resolution, volume
  - clob.polymarket.com       order-book depth (real liquidity)
  - lb-api.polymarket.com     leaderboard seed (optional; VERIFY host)
"""
from __future__ import annotations

import json
import os
import time
import hashlib
import threading
from typing import Any, Dict, List, Optional

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
# VERIFY: the public leaderboard is rendered at polymarket.com/leaderboard; the
# JSON behind it has historically been served from this host. If it 404s, fall
# back to order-flow discovery (universe.discover_from_order_flow), which needs
# no leaderboard endpoint at all.
LB_API = "https://lb-api.polymarket.com"

CACHE_DIR = os.environ.get("PM_CACHE_DIR", os.path.join(os.path.dirname(__file__), "..", "cache"))
CACHE_TTL = int(os.environ.get("PM_CACHE_TTL", "86400"))  # seconds; raw fills are immutable
REQUEST_TIMEOUT = 20
MAX_RETRIES = 4
RATE_LIMIT_SLEEP = float(os.environ.get("PM_RATE_SLEEP", "0.2"))

_session_local = threading.local()
_rate_lock = threading.Lock()
_last_call = [0.0]


def _session() -> requests.Session:
    s = getattr(_session_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "edgeFoundry-weather-scan/1.0", "Accept": "application/json"})
        _session_local.s = s
    return s


def _cache_path(key: str) -> str:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(CACHE_DIR, h + ".json")


def _cache_get(key: str) -> Optional[Any]:
    p = _cache_path(key)
    try:
        if os.path.getmtime(p) + CACHE_TTL < time.time():
            return None
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _cache_put(key: str, value: Any) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(_cache_path(key), "w", encoding="utf-8") as fh:
            json.dump(value, fh)
    except OSError:
        pass


def _throttle() -> None:
    with _rate_lock:
        wait = RATE_LIMIT_SLEEP - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


class PolymarketError(RuntimeError):
    """Raised when an endpoint is unreachable after all retries."""


def get_json(url: str, params: Optional[Dict[str, Any]] = None, *, use_cache: bool = True) -> Any:
    """GET with disk cache + exponential-backoff retries. Raises on hard failure."""
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            r = _session().get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:  # rate limited -> back off and retry
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            data = r.json()
            if use_cache:
                _cache_put(key, data)
            return data
        except Exception as exc:  # noqa: BLE001 -- network layer, surface as PolymarketError
            last_exc = exc
            time.sleep(min(2 ** attempt, 16))
    raise PolymarketError(f"GET failed after {MAX_RETRIES} tries: {url} ({last_exc})")


def post_json(url: str, payload: Any, *, use_cache: bool = True) -> Any:
    key = url + "#" + json.dumps(payload, sort_keys=True)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    last_exc: Optional[Exception] = None
    for attempt in range(MAX_RETRIES):
        _throttle()
        try:
            r = _session().post(url, json=payload, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            data = r.json()
            if use_cache:
                _cache_put(key, data)
            return data
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(min(2 ** attempt, 16))
    raise PolymarketError(f"POST failed after {MAX_RETRIES} tries: {url} ({last_exc})")


# --------------------------------------------------------------------------- #
# data-api
# --------------------------------------------------------------------------- #
def activity(addr: str, *, cap: int = 5000, kind: str = "TRADE") -> List[Dict[str, Any]]:
    """Paginated fill history for a wallet. Fields per the mission spec:
    side, size, price, usdcSize, timestamp, title, outcome, asset, conditionId,
    transactionHash, type."""
    out: List[Dict[str, Any]] = []
    offset = 0
    while len(out) < cap:
        chunk = get_json(f"{DATA_API}/activity",
                         {"user": addr, "limit": 100, "offset": offset})
        if not chunk:
            break
        out.extend(a for a in chunk if a.get("type") == kind)
        if len(chunk) < 100:
            break
        offset += 100
    return out


def positions(addr: str) -> List[Dict[str, Any]]:
    """Current + realized positions. Includes realizedPnl/cashPnl/avgPrice/curPrice
    per conditionId -- the authoritative source for realized PnL and win/loss."""
    return get_json(f"{DATA_API}/positions", {"user": addr}) or []


def market_trades(condition_id: str, *, cap: int = 2000) -> List[Dict[str, Any]]:
    """All fills in a market -> used to discover wallets from order flow.
    Each fill exposes ``proxyWallet`` (the trader address)."""
    out: List[Dict[str, Any]] = []
    offset = 0
    while len(out) < cap:
        chunk = get_json(f"{DATA_API}/trades",
                         {"market": condition_id, "limit": 100, "offset": offset})
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        offset += 100
    return out


# --------------------------------------------------------------------------- #
# gamma-api  (market metadata, tags, resolution, volume)
# --------------------------------------------------------------------------- #
def gamma_markets(*, tag_id: Optional[int] = None, closed: Optional[bool] = None,
                  limit: int = 500, offset: int = 0,
                  condition_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    if tag_id is not None:
        params["tag_id"] = tag_id
    if closed is not None:
        params["closed"] = str(closed).lower()
    if condition_ids:
        params["condition_ids"] = ",".join(condition_ids)
    return get_json(f"{GAMMA_API}/markets", params) or []


def gamma_market_by_condition(condition_id: str) -> Optional[Dict[str, Any]]:
    res = gamma_markets(condition_ids=[condition_id], limit=1)
    return res[0] if res else None


def gamma_tags() -> List[Dict[str, Any]]:
    """Tag catalogue -> resolve the numeric id(s) for weather/climate."""
    return get_json(f"{GAMMA_API}/tags", {"limit": 1000}) or []


def weather_tag_ids() -> List[int]:
    """Best-effort resolution of weather/climate tag ids. Falls back to [] (then
    callers rely on the title regex in metrics.is_weather_market)."""
    ids: List[int] = []
    try:
        for t in gamma_tags():
            label = (t.get("label") or t.get("slug") or "").lower()
            if any(k in label for k in ("weather", "climate", "temperature")):
                tid = t.get("id")
                if tid is not None:
                    ids.append(int(tid))
    except PolymarketError:
        pass
    return ids


# --------------------------------------------------------------------------- #
# clob  (real order-book depth)
# --------------------------------------------------------------------------- #
def clob_book(token_id: str) -> Dict[str, Any]:
    """Order book for one outcome token: {'bids':[{price,size}...],'asks':[...]}.
    Used to measure how much size can be filled within N cents (real liquidity)."""
    return get_json(f"{CLOB_API}/book", {"token_id": token_id})


# --------------------------------------------------------------------------- #
# leaderboard seed (optional)
# --------------------------------------------------------------------------- #
def leaderboard(window: str = "all", order: str = "profit",
                category: str = "weather", limit: int = 100) -> List[Dict[str, Any]]:
    """Seed wallets from the public weather leaderboard.

    VERIFY the exact host/params against the network tab on
    polymarket.com/leaderboard/weather/<window>/<order>. The shape returned is
    expected to contain {proxyWallet|wallet, name|pseudonym, amount|profit}.
    Returns [] on failure so the scan can fall back to order-flow discovery.
    """
    for base in (LB_API, DATA_API):
        try:
            data = get_json(f"{base}/leaderboard",
                            {"window": window, "order": order,
                             "category": category, "limit": limit})
            if data:
                return data if isinstance(data, list) else data.get("data", [])
        except PolymarketError:
            continue
    return []
