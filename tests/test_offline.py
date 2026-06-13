#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline logic tests -- no network. Synthetic fill fixtures model each archetype
the mission already analysed, proving the filters reject them for the right
reason and accept a clean directional forecaster.

Run:  python -m tests.test_offline      (or  python run_scan.py --selftest)
"""
from __future__ import annotations

import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, ".")
from scanner import metrics as M  # noqa: E402

DAY = 86400
NOW = 1_750_000_000  # fixed clock so fixtures are deterministic


def fill(side: str, size: float, price: float, ts: float, title: str, asset: str) -> Dict[str, Any]:
    return {"side": side, "size": size, "price": price, "usdcSize": size * price,
            "timestamp": ts, "title": title, "outcome": "Yes", "asset": asset,
            "conditionId": "0x" + asset, "type": "TRADE"}


def market_maker_fills() -> List[Dict[str, Any]]:
    """WeatherHK/ColdMath archetype: huge two-sided volume, ~equal buy/sell,
    constant rebuy of the same outcome -> near-zero efficiency."""
    out = []
    start = NOW - 200 * DAY
    for i in range(120):
        t = start + i * DAY
        title = f"Highest temperature in NYC on day {i % 40}"
        asset = f"nyc{i % 40:02d}"
        # buy then sell at ~same price minutes apart, repeatedly (churn)
        out.append(fill("BUY", 1000, 0.50, t, title, asset))
        out.append(fill("SELL", 1000, 0.505, t + 120, title, asset))
        out.append(fill("BUY", 1000, 0.50, t + 240, title, asset))  # rebuy
        out.append(fill("SELL", 1000, 0.50, t + 360, title, asset))
    return out


def longshot_fills() -> List[Dict[str, Any]]:
    """Empusa archetype: ~2-month account, few markets, ~all profit from one
    13x precipitation longshot -> high concentration, too few markets, too new."""
    out = []
    start = NOW - 55 * DAY
    # one big winner
    out.append(fill("BUY", 200, 0.07, start, "Rainfall in London in March", "lonrain"))
    out.append(fill("SELL", 200, 0.95, start + 20 * DAY, "Rainfall in London in March", "lonrain"))
    # a handful of small scattered markets, mostly break-even
    for i in range(8):
        t = start + i * DAY
        title = f"Highest temperature in Paris day {i}"
        out.append(fill("BUY", 30, 0.40, t, title, f"par{i}"))
        out.append(fill("SELL", 30, 0.42, t + 7 * DAY, title, f"par{i}"))
    return out


def clean_forecaster_fills() -> List[Dict[str, Any]]:
    """The profile we WANT: directional, distributed across 50 markets, holds to
    near-resolution (exits ~0.95), 1+ year history, buys anchored at 14:00 GMT+3.
    Timestamps are built from explicit Bucharest-local datetimes so the awake-
    window share is deterministic regardless of the fixed clock above."""
    from datetime import datetime
    out = []
    base = datetime(2024, 1, 5, 14, 0, tzinfo=M.TZ)  # 14:00 GMT+3
    for i in range(50):
        buy_ts = base.timestamp() + i * 5 * DAY     # 14:00 local, every 5 days
        title = f"Highest temperature in Seoul market {i}"
        asset = f"seoul{i:02d}"
        out.append(fill("BUY", 100, 0.45, buy_ts, title, asset))
        # win ~75%: held to near-resolution (exit 0.95); losers cut at 0.30
        win = (i % 4 != 0)
        out.append(fill("SELL", 100, 0.95 if win else 0.30, buy_ts + 2 * DAY, title, asset))
    return out


def _check(name: str, cond: bool) -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return cond


def run_all() -> int:
    print("Offline filter-logic self-test (synthetic fixtures, no network)\n")
    ok = True

    # --- market maker: must fail efficiency (#2) ---
    s = M.scorecard("0xMM", market_maker_fills(), weather_only=True, now=NOW)
    ab = M.antibot_flags(s)
    f = M.hard_filters(s, antibot=ab)
    print("Market-maker archetype (WeatherHK/ColdMath):")
    ok &= _check("efficiency < 15% flagged", any("#2" in x for x in f))
    ok &= _check("anti-bot flags two-sided/churn", len(ab) > 0)
    print(f"    eff={s['eff']:.1f}%  rebuy={s['rebuy_ratio']:.2f}  fails={len(f)}\n")

    # --- longshot: must fail concentration (#5) AND age (#6) AND markets (#7) ---
    s = M.scorecard("0xLS", longshot_fills(), weather_only=True, now=NOW)
    f = M.hard_filters(s)
    print("Longshot archetype (Empusa):")
    ok &= _check("concentration > 55% flagged", any("#5" in x for x in f))
    ok &= _check("account age < 90d flagged", any("#6" in x for x in f))
    ok &= _check("distinct markets < 40 flagged", any("#7" in x for x in f))
    print(f"    conc={s['conc']:.0f}%  days={s['days']:.0f}  markets={s['resolved']}  fails={len(f)}\n")

    # --- clean forecaster: should pass the core directional/distribution/history gates ---
    s = M.scorecard("0xCLEAN", clean_forecaster_fills(), weather_only=True, now=NOW)
    f = M.hard_filters(s)
    print("Clean directional forecaster (target profile):")
    ok &= _check("efficiency >= 15%", s["eff"] >= 15)
    ok &= _check("concentration <= 55%", s["conc"] <= 55)
    ok &= _check("hold >= 6h", s["hold_h"] >= 6)
    ok &= _check("age >= 90d", s["days"] >= 90)
    ok &= _check("distinct markets >= 40", s["resolved"] >= 40)
    ok &= _check("buys land in GMT+3 awake window", s["awake_share"] > 50)
    print(f"    eff={s['eff']:.1f}%  conc={s['conc']:.0f}%  hold={s['hold_h']:.1f}h  "
          f"days={s['days']:.0f}  markets={s['resolved']}  winrate={s['winrate']:.0f}%  "
          f"score={M.rank_score(s)}\n")

    # --- liquidity absorption math ---
    book = {"asks": [{"price": 0.46, "size": 200}, {"price": 0.47, "size": 300},
                     {"price": 0.50, "size": 1000}]}
    absb = M.absorb_within(book, "BUY", 0.45, 0.02)  # only 0.46 & 0.47 within 2c
    pts, passed = M.liquidity_score(absb, typical_fill=45)
    print("Liquidity absorption:")
    ok &= _check("absorbs only levels within 2c", abs(absb - (0.46*200 + 0.47*300)) < 1e-6)
    ok &= _check("passes when book >= typical fill", passed)
    print(f"    absorbable=${absb:,.0f}  pts={pts}\n")

    print("RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
