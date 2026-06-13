#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Per-wallet scorecard, the 11 hard filters, anti-bot/wash heuristics, CLOB
liquidity absorption, and the 0-100 ranking score.

Pure functions where possible so the logic is unit-testable offline with
synthetic fixtures (see tests/test_offline.py). Network is only touched by the
``enrich_*`` helpers that take a client module.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bucharest")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=3))

# --------------------------------------------------------------------------- #
# thresholds (tunable) -- the 11 hard filters
# --------------------------------------------------------------------------- #
PNL_MIN, PNL_MAX = 1000.0, 20000.0      # #1 copyable-scale band
EFF_MIN = 15.0                           # #2 net/volume, kills market makers
REBUY_RATIO_MAX = 0.30                   # #3 sell-then-rebuy churn
HOLD_MIN_H = 6.0                         # #4 median hold, kills scalpers
CONC_MAX = 55.0                          # #5 top-3 markets share of winnings
TRACK_MIN_DAYS = 90.0                    # #6 account age
RESOLVED_MIN = 40                        # #7 distinct resolved markets
WINRATE_MIN = 55.0                       # #8 capital-weighted win rate
BOND_PNL_MAX = 40.0                      # #10 share of PnL parked at >=95c
AWAKE_LO, AWAKE_HI = 8, 24               # GMT+3 awake window for lag-copy score

# liquidity (#9): book must absorb the wallet's typical fill within this many cents
LIQ_CENTS = 0.02

# weather/climate market detection (fallback when Gamma tags are unavailable)
_WEATHER_RE = re.compile(
    r"\b(weather|temperature|high temp|low temp|hottest|coldest|degrees?|"
    r"°[cf]?|rain(fall)?|precipitation|precip|snow(fall)?|hurricane|cyclone|"
    r"typhoon|earthquake|magnitude|climate|heat ?wave|nws|noaa)\b",
    re.IGNORECASE,
)
# city-temp markets often read "Highest temperature in <City> on <date>"
_CITY_TEMP_RE = re.compile(r"\btemp(erature)?\b.*\bin\b", re.IGNORECASE)


def is_weather_market(title: str) -> bool:
    if not title:
        return False
    return bool(_WEATHER_RE.search(title) or _CITY_TEMP_RE.search(title))


def usd(a: Dict[str, Any]) -> float:
    v = a.get("usdcSize")
    if v is not None:
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    return float(a.get("size", 0) or 0) * float(a.get("price", 0) or 0)


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


# --------------------------------------------------------------------------- #
# core scorecard from /activity fills
# --------------------------------------------------------------------------- #
def scorecard(addr: str, trades: List[Dict[str, Any]], *,
              weather_only: bool = True,
              positions: Optional[List[Dict[str, Any]]] = None,
              resolution: Optional[Dict[str, bool]] = None,
              now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Compute every metric the filters need.

    trades      : /activity TRADE fills.
    positions   : /positions rows -> authoritative realizedPnl per conditionId.
    resolution  : {conditionId: won_bool} from Gamma for capital-weighted winrate.
    """
    now = now or time.time()
    if weather_only:
        trades = [a for a in trades if is_weather_market(a.get("title", ""))]
    if not trades:
        return None

    buy = sum(usd(a) for a in trades if a.get("side") == "BUY")
    sell = sum(usd(a) for a in trades if a.get("side") == "SELL")
    vol = buy + sell
    sizes = [usd(a) for a in trades]
    fill_med, fill_max = _median(sizes), max(sizes)

    # ---- realized PnL: prefer /positions, fall back to sell-buy proxy ----
    realized_by_cond: Dict[str, float] = {}
    if positions:
        for p in positions:
            if weather_only and not is_weather_market(p.get("title", "")):
                continue
            cond = p.get("conditionId") or p.get("condition_id")
            if not cond:
                continue
            r = p.get("realizedPnl", p.get("cashPnl"))
            if r is not None:
                try:
                    realized_by_cond[cond] = realized_by_cond.get(cond, 0.0) + float(r)
                except (TypeError, ValueError):
                    pass
    net = sum(realized_by_cond.values()) if realized_by_cond else (sell - buy)
    eff = (net / vol * 100.0) if vol else 0.0

    # ---- per-outcome walk: hold time + rebuy-after-sell ----
    per_asset: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in trades:
        per_asset[a.get("asset")].append(a)
    holds: List[float] = []
    rebuys = 0
    roundtrip_sub_min = 0   # anti-bot signal: sell within 60s of a buy
    for lst in per_asset.values():
        lst.sort(key=lambda x: float(x["timestamp"]))
        pos, open_ts, sold = 0.0, None, False
        for a in lst:
            sz = float(a.get("size", 0) or 0)
            ts = float(a["timestamp"])
            if a.get("side") == "BUY":
                if sold and pos <= 1e-9:
                    rebuys += 1
                if pos <= 1e-9:
                    open_ts = ts
                pos += sz
            else:
                if open_ts is not None:
                    held = ts - open_ts
                    holds.append(held)
                    if held < 60:
                        roundtrip_sub_min += 1
                pos -= sz
                if pos <= 1e-9:
                    sold, open_ts = True, None
    hold_med = _median(holds)

    # ---- per-market PnL (proxy) -> concentration + distinct markets ----
    pnl_by_market: Dict[str, float] = defaultdict(float)
    cond_by_market: Dict[str, str] = {}
    capital_by_market: Dict[str, float] = defaultdict(float)
    for a in trades:
        title = a.get("title", "?")
        pnl_by_market[title] += usd(a) * (1 if a.get("side") == "SELL" else -1)
        cond_by_market[title] = a.get("conditionId") or a.get("condition_id") or title
        if a.get("side") == "BUY":
            capital_by_market[title] += usd(a)

    # if /positions gave realized PnL, override per-market with the truth
    if realized_by_cond:
        cond_to_title = {v: k for k, v in cond_by_market.items()}
        for cond, r in realized_by_cond.items():
            pnl_by_market[cond_to_title.get(cond, cond)] = r

    wins = [v for v in pnl_by_market.values() if v > 0]
    pos_total = sum(wins) or 1.0
    conc = sum(sorted(wins, reverse=True)[:3]) / pos_total * 100.0 if wins else 100.0
    resolved = len(pnl_by_market)

    # ---- win rate, capital-weighted, over resolved markets ----
    if resolution:
        won_cap = lost_cap = 0.0
        for title, cond in cond_by_market.items():
            if cond not in resolution:
                continue
            cap = capital_by_market.get(title, 0.0) or 1.0
            if resolution[cond]:
                won_cap += cap
            else:
                lost_cap += cap
        denom = won_cap + lost_cap
        winrate = (won_cap / denom * 100.0) if denom else 0.0
    else:
        # proxy: share of markets with positive PnL, weighted by deployed capital
        won_cap = sum(capital_by_market.get(t, 0.0) for t, v in pnl_by_market.items() if v > 0)
        all_cap = sum(capital_by_market.values()) or 1.0
        winrate = won_cap / all_cap * 100.0

    # ---- bond-style yield farming: share of PnL from >=95c entries (#10) ----
    bond_pnl = 0.0
    for a in trades:
        if a.get("side") == "BUY" and float(a.get("price", 0) or 0) >= 0.95:
            # treat capital parked on near-certain outcomes as "bond" capital
            bond_pnl += usd(a)
    bond_share = (bond_pnl / buy * 100.0) if buy else 0.0

    # ---- buy-hour histogram in GMT+3 + awake share ----
    buy_h: Dict[int, float] = defaultdict(float)
    for a in trades:
        if a.get("side") == "BUY":
            hr = datetime.fromtimestamp(float(a["timestamp"]), TZ).hour
            buy_h[hr] += usd(a)
    top_buy_hours = sorted(buy_h, key=buy_h.get, reverse=True)[:3]
    total_buy_val = sum(buy_h.values()) or 1.0
    awake_share = sum(v for h, v in buy_h.items() if AWAKE_LO <= h < AWAKE_HI) / total_buy_val * 100.0

    days = (now - min(float(a["timestamp"]) for a in trades)) / 86400.0

    return dict(
        addr=addr, n=len(trades), vol=vol, buy=buy, sell=sell, net=net, eff=eff,
        fill_med=fill_med, fill_max=fill_max, hold_h=hold_med / 3600.0,
        rebuy_ratio=rebuys / max(resolved, 1), conc=conc, resolved=resolved,
        winrate=winrate, days=days, bond_share=bond_share,
        roundtrip_sub_min=roundtrip_sub_min,
        top_buy_hours=sorted(top_buy_hours), awake_share=awake_share,
        buy_hist={h: round(v, 2) for h, v in sorted(buy_h.items())},
        top_markets=sorted(pnl_by_market.items(), key=lambda x: -x[1])[:5],
        markets_conds=cond_by_market, capital_by_market=dict(capital_by_market),
    )


# --------------------------------------------------------------------------- #
# anti-bot / market-maker / wash heuristics (#11)
# --------------------------------------------------------------------------- #
def antibot_flags(s: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    # symmetric two-sided quoting => buy ~= sell volume and near-zero efficiency
    if s["vol"] > 0 and abs(s["buy"] - s["sell"]) / s["vol"] < 0.05 and s["eff"] < 8:
        flags.append("two-sided quoting (buy~=sell, eff<8%) = MM")
    # heavy sub-minute round trips => HFT/bot
    if s["roundtrip_sub_min"] >= max(5, 0.2 * s["n"]):
        flags.append("many sub-minute round-trips = bot/HFT")
    # churn
    if s["rebuy_ratio"] >= 0.6:
        flags.append("very high rebuy churn = MM")
    return flags


def linked_wallet_flags(addr: str, trades: List[Dict[str, Any]],
                        universe: Dict[str, str]) -> List[str]:
    """Circular-flow check: if a counterparty in the same universe appears on the
    opposite side of the same market within seconds, repeatedly -> possible wash.
    Counterparty data is only present if /trades exposes maker/taker; otherwise
    this returns [] and the check is a no-op (documented assumption)."""
    flags: List[str] = []
    pairs: Dict[str, int] = defaultdict(int)
    for a in trades:
        cp = (a.get("maker") or a.get("counterparty") or "").lower()
        if cp and cp in universe and cp != addr.lower():
            pairs[cp] += 1
    for cp, cnt in pairs.items():
        if cnt >= 10:
            flags.append(f"circular flow with {cp[:10]}... ({cnt} fills)")
    return flags


# --------------------------------------------------------------------------- #
# CLOB liquidity absorption (#9)
# --------------------------------------------------------------------------- #
def absorb_within(book: Dict[str, Any], side: str, ref_price: float,
                  cents: float = LIQ_CENTS) -> float:
    """USD notional absorbable within `cents` of ref_price.
    A copier BUYs -> consumes asks above ref; SELLs -> consumes bids below ref."""
    levels = book.get("asks" if side == "BUY" else "bids", []) or []
    total = 0.0
    for lvl in levels:
        price = float(lvl.get("price", 0) or 0)
        size = float(lvl.get("size", 0) or 0)
        if side == "BUY" and price <= ref_price + cents:
            total += price * size
        elif side == "SELL" and price >= ref_price - cents:
            total += price * size
    return total


def liquidity_score(absorbable_usd: float, typical_fill: float) -> Tuple[float, bool]:
    """Returns (0..10 points, passes_filter_9). Passes if the book absorbs at
    least the wallet's typical fill within LIQ_CENTS."""
    if typical_fill <= 0:
        return 0.0, False
    ratio = absorbable_usd / typical_fill
    passes = ratio >= 1.0
    pts = 10.0 * min(1.0, absorbable_usd / 500.0)  # copier scale ~$500
    return round(pts, 1), passes


# --------------------------------------------------------------------------- #
# the 11 hard filters
# --------------------------------------------------------------------------- #
def hard_filters(s: Dict[str, Any], *, liquidity_pass: Optional[bool] = None,
                 antibot: Optional[List[str]] = None) -> List[str]:
    fails: List[str] = []
    if not (PNL_MIN <= s["net"] <= PNL_MAX):
        fails.append(f"#1 PnL ${s['net']:.0f} outside ${PNL_MIN:.0f}-${PNL_MAX:.0f}")
    if s["eff"] < EFF_MIN:
        fails.append(f"#2 efficiency {s['eff']:.1f}% < {EFF_MIN}% (market maker)")
    if s["rebuy_ratio"] >= REBUY_RATIO_MAX:
        fails.append(f"#3 rebuy ratio {s['rebuy_ratio']:.2f} >= {REBUY_RATIO_MAX} (churn)")
    if s["hold_h"] < HOLD_MIN_H:
        fails.append(f"#4 median hold {s['hold_h']:.1f}h < {HOLD_MIN_H}h (scalper)")
    if s["conc"] > CONC_MAX:
        fails.append(f"#5 concentration {s['conc']:.0f}% > {CONC_MAX}% (longshot-hitter)")
    if s["days"] < TRACK_MIN_DAYS:
        fails.append(f"#6 account age {s['days']:.0f}d < {TRACK_MIN_DAYS:.0f}d (too green)")
    if s["resolved"] < RESOLVED_MIN:
        fails.append(f"#7 distinct markets {s['resolved']} < {RESOLVED_MIN} (small sample)")
    if s["winrate"] < WINRATE_MIN:
        fails.append(f"#8 win rate {s['winrate']:.0f}% < {WINRATE_MIN}% (luck)")
    if liquidity_pass is False:
        fails.append("#9 book does not absorb typical fill within 2c (thin)")
    if s["bond_share"] > BOND_PNL_MAX:
        fails.append(f"#10 {s['bond_share']:.0f}% capital parked >=95c (bond-style)")
    if antibot:
        fails.append("#11 " + "; ".join(antibot))
    return fails


# --------------------------------------------------------------------------- #
# 0-100 ranking score (only meaningful for survivors)
# --------------------------------------------------------------------------- #
def rank_score(s: Dict[str, Any], *, liq_pts: Optional[float] = None) -> float:
    # 25 -- directional quality: 15-45% band is the sweet spot; >70% penalised
    if 15 <= s["eff"] <= 45:
        eff_pts = 25.0
    elif s["eff"] <= 70:
        eff_pts = 12.0
    else:
        eff_pts = 4.0
    # 20 -- distribution (1 - concentration)
    dist_pts = 20.0 * (1 - min(s["conc"], 100) / 100.0)
    # 15 -- track record: distinct markets x age
    hist_pts = 15.0 * min(1.0, s["resolved"] / 120.0) * min(1.0, s["days"] / 180.0)
    # 15 -- win rate (capital-weighted), scaled over 50-80%
    win_pts = 15.0 * min(1.0, max(0.0, (s["winrate"] - 50) / 30.0))
    # 15 -- lag-copy compatibility: buy activity overlapping GMT+3 awake window
    lag_pts = 15.0 * (s["awake_share"] / 100.0)
    # 10 -- liquidity/copyability (real CLOB depth if available, else fill proxy)
    if liq_pts is None:
        liq_pts = 10.0 * min(1.0, s["fill_max"] / 500.0)
    return round(eff_pts + dist_pts + hist_pts + win_pts + lag_pts + liq_pts, 1)
