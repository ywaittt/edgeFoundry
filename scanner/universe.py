#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the universe of weather/climate trader wallets.

Two complementary sources, then dedup:
  1. Leaderboard seed  -- every page of category=weather x {monthly,weekly,all}
                          x {profit,volume}.
  2. Order-flow expand  -- wallets that traded the most active weather markets in
                          the last ~30 days (catches profitable traders who are
                          not on the leaderboard).
"""
from __future__ import annotations

import time
from typing import Dict, List, Set

from . import pmclient as pm
from .metrics import is_weather_market

WINDOWS = ("monthly", "weekly", "all")
ORDERS = ("profit", "volume")


def _addr_of(row: Dict) -> str:
    for k in ("proxyWallet", "wallet", "address", "user", "account"):
        v = row.get(k)
        if v:
            return str(v).lower()
    return ""


def seed_from_leaderboard() -> Dict[str, str]:
    """Returns {address: display_name}. Empty if the leaderboard endpoint is
    unavailable (the order-flow source then carries the universe)."""
    found: Dict[str, str] = {}
    for window in WINDOWS:
        for order in ORDERS:
            try:
                rows = pm.leaderboard(window=window, order=order, category="weather", limit=200)
            except pm.PolymarketError:
                rows = []
            for row in rows:
                addr = _addr_of(row)
                if not addr:
                    continue
                name = row.get("name") or row.get("pseudonym") or row.get("displayName") or ""
                found.setdefault(addr, name)
    return found


def discover_from_order_flow(*, days: int = 30, max_markets: int = 80,
                             trades_per_market: int = 1000) -> Set[str]:
    """Pull recent, high-volume weather markets from Gamma and harvest every
    wallet that traded them."""
    wallets: Set[str] = set()
    tag_ids = pm.weather_tag_ids()
    markets: List[Dict] = []

    if tag_ids:
        for tid in tag_ids:
            markets.extend(pm.gamma_markets(tag_id=tid, closed=False, limit=500))
            markets.extend(pm.gamma_markets(tag_id=tid, closed=True, limit=500))
    else:
        # No tag id resolved -> scan recent markets and keep weather ones by title.
        page = pm.gamma_markets(limit=500)
        markets.extend(m for m in page if is_weather_market(m.get("question") or m.get("title", "")))

    # Keep weather markets, rank by traded volume, take the most active.
    weather = [m for m in markets if is_weather_market(m.get("question") or m.get("title", ""))]
    weather.sort(key=lambda m: float(m.get("volume") or m.get("volumeNum") or 0), reverse=True)

    cutoff = time.time() - days * 86400
    for m in weather[:max_markets]:
        cond = m.get("conditionId") or m.get("condition_id")
        if not cond:
            continue
        for fill in pm.market_trades(cond, cap=trades_per_market):
            ts = fill.get("timestamp")
            if ts and float(ts) < cutoff:
                continue
            addr = _addr_of(fill)
            if addr:
                wallets.add(addr)
    return wallets


def build_universe(*, use_leaderboard: bool = True, use_order_flow: bool = True,
                   days: int = 30) -> Dict[str, str]:
    """Returns {address: name} deduped across both sources."""
    universe: Dict[str, str] = {}
    if use_leaderboard:
        universe.update(seed_from_leaderboard())
    if use_order_flow:
        for addr in discover_from_order_flow(days=days):
            universe.setdefault(addr, "")
    return universe
