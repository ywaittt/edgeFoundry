#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Render the scan result as a readable Markdown report:
  1) Winner -- Copy Playbook
  2) Reserves (3-5)
  3) Rejection journal
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bucharest")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=3))

PROFILE = "https://polymarket.com/profile/{addr}"

# Per-niche copy lag tolerance. Daily city-temp markets resolve at the local
# day's end -> the actionable window is the city's afternoon, converted to GMT+3.
NICHE_LAG = {
    "monthly precipitation": "hours-to-days (slow drift; very lag-forgiving)",
    "earthquake": "days (long horizon; lag-forgiving)",
    "daily temperature": "minutes-to-a-few-hours (copy before the city's afternoon, GMT+3)",
    "other weather": "hours (verify per market)",
}


def _bar(value: float, peak: float, width: int = 28) -> str:
    if peak <= 0:
        return ""
    return "#" * max(0, int(round(value / peak * width)))


def _sizing_map(fill_med: float, bankroll: float = 500.0,
                his_bankroll_guess: Optional[float] = None) -> List[str]:
    """Proportional sizing: scale his median fill down to a $500 bankroll.
    If his bankroll is unknown, anchor on a conservative 2% unit of $500 and show
    the ratio implied by his median fill against a 5%-of-bankroll assumption."""
    lines = []
    his_unit = fill_med
    # assume his median fill is ~5% of his working bankroll (documented heuristic)
    implied_bankroll = his_bankroll_guess or (his_unit / 0.05 if his_unit else 0)
    ratio = (bankroll / implied_bankroll) if implied_bankroll else 0
    your_fill = his_unit * ratio
    lines.append(f"- His median fill: **${his_unit:,.0f}**")
    if implied_bankroll:
        lines.append(f"- Implied bankroll (median≈5%): ~${implied_bankroll:,.0f} "
                     f"→ proportional ratio ≈ **{ratio:.3f}x**")
        lines.append(f"- Your matched fill on $500: **${your_fill:,.2f}** "
                     f"(cap at 2-5% = $10-$25 per market)")
    else:
        lines.append("- Insufficient fill data to derive a ratio.")
    return lines


def scorecard_block(s: Dict[str, Any]) -> str:
    L = [
        f"- **Realized weather PnL:** ${s['net']:,.0f}",
        f"- **Efficiency (net/vol):** {s['eff']:.1f}%  (vol ${s['vol']:,.0f})",
        f"- **Median hold:** {s['hold_h']:.1f} h",
        f"- **Rebuy ratio:** {s['rebuy_ratio']:.2f}",
        f"- **Concentration (top-3):** {s['conc']:.0f}%",
        f"- **Account age:** {s['days']:.0f} days",
        f"- **Distinct resolved markets:** {s['resolved']}",
        f"- **Win rate (capital-weighted):** {s['winrate']:.0f}%",
        f"- **Fill median / max:** ${s['fill_med']:,.0f} / ${s['fill_max']:,.0f}",
        f"- **Bond-style share:** {s['bond_share']:.0f}%",
    ]
    L.append("- **Top 5 markets by PnL:**")
    for title, pnl in s["top_markets"]:
        L.append(f"    - ${pnl:,.0f} — {title}")
    return "\n".join(L)


def buy_hist_block(s: Dict[str, Any]) -> str:
    hist = s.get("buy_hist", {})
    if not hist:
        return "_no buy timing data_"
    peak = max(hist.values()) or 1
    rows = [f"  {h:02d}:00 GMT+3 | {_bar(v, peak)} ${v:,.0f}" for h, v in sorted(hist.items())]
    top = ", ".join(f"{h:02d}:00" for h in s["top_buy_hours"])
    return "```\n" + "\n".join(rows) + "\n```\n" + f"Peak buy hours (GMT+3): **{top}**  |  awake-window share: **{s['awake_share']:.0f}%**"


def winner_playbook(name: str, s: Dict[str, Any], score: float,
                    cross_check: Optional[str] = None) -> str:
    addr = s["addr"]
    out = [
        "## 1) WINNER — Copy Playbook",
        "",
        f"**{name or '(pseudonym n/a)'}** — `{addr}`",
        f"Profile: {PROFILE.format(addr=addr)}",
        f"**Rank score: {score}/100**",
        "",
        "### Scorecard",
        scorecard_block(s),
        "",
        "### Entry (BUY) windows in GMT+3",
        buy_hist_block(s),
        "",
        "### Copy lag tolerance by niche",
    ]
    for niche, lag in NICHE_LAG.items():
        out.append(f"- **{niche}:** {lag}")
    out += [
        "",
        "### Sizing map for a $500 bankroll",
        *_sizing_map(s["fill_med"]),
        "",
        "### Copy gate",
        "- Enter **only** if the current price is within **2-3¢** of his fill price.",
        "- Skip if the CLOB book cannot absorb your size within 2¢ (see liquidity).",
    ]
    if cross_check:
        out += ["", "### Edge cross-check", cross_check]
    return "\n".join(out)


def reserves_table(reserves: List[Dict[str, Any]]) -> str:
    out = ["## 2) Reserves", "",
           "| Wallet | Score | PnL | Eff | Hold(h) | Conc | Markets | WinRate |",
           "|---|---|---|---|---|---|---|---|"]
    for r in reserves:
        s = r["s"]
        out.append(f"| `{s['addr'][:12]}…` | {r['score']} | ${s['net']:,.0f} | "
                   f"{s['eff']:.0f}% | {s['hold_h']:.1f} | {s['conc']:.0f}% | "
                   f"{s['resolved']} | {s['winrate']:.0f}% |")
    return "\n".join(out)


def rejection_journal(rejected: List[Dict[str, Any]], *, limit: int = 40) -> str:
    out = ["## 3) Rejection journal", "",
           "| Wallet | First failing filter(s) |", "|---|---|"]
    for r in rejected[:limit]:
        s = r["s"]
        label = r.get("name") or s["addr"][:12] + "…"
        reasons = "; ".join(r["fails"][:3])
        out.append(f"| {label} | {reasons} |")
    return "\n".join(out)


def full_report(winner, reserves, rejected, *, meta: Dict[str, Any]) -> str:
    head = [
        "# Polymarket Weather Copy-Trader Scan",
        "",
        f"_Generated {datetime.now(TZ):%Y-%m-%d %H:%M} GMT+3_",
        f"_Universe: {meta.get('universe_size', '?')} wallets "
        f"({meta.get('leaderboard', 0)} leaderboard + {meta.get('order_flow', 0)} order-flow)_",
        f"_Scored: {meta.get('scored', '?')} | Passed all 11 filters: {meta.get('passed', '?')}_",
        "",
    ]
    parts = ["\n".join(head)]
    if winner:
        parts.append(winner_playbook(winner.get("name", ""), winner["s"],
                                     winner["score"], winner.get("cross_check")))
    else:
        parts.append("## 1) WINNER\n\n**No wallet passed all 11 hard filters.** "
                     "Closest candidate and the exact filter(s) it fails are listed "
                     "as the first reserve below — not substituted as a compromise winner.")
    if reserves:
        parts.append(reserves_table(reserves))
    if rejected:
        parts.append(rejection_journal(rejected))
    return "\n\n".join(parts)
