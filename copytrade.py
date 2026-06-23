#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Copy-trade PnL simulator for Polymarket.

Given a target trader's fills, compute what *your* PnL would be if you mirrored
him with:
  - a sizing fraction   (e.g. 0.65 = 65% of his share size), and
  - adverse slippage    (e.g. 0.03 = you pay 3c worse on every fill:
                         he buys 0.27 -> you buy 0.30; he sells 0.15 -> you sell 0.12),
  - an optional start time filter (only copy fills at/after this instant).

His fills come from one of:
  1. data-api /activity?user=<addr>           (auto, when the host is reachable)
  2. a CSV you paste off the Polymarket UI     (--csv)
  3. an inline demo of your example prices      (--demo)

Nothing is invented: with no real fills and no reachable API, the tool refuses
to print a PnL and tells you exactly how to supply the fills.

USAGE
  python copytrade.py --address 0x... --start 2026-06-21T09:00:00+03:00 \
                      --copy-fraction 0.65 --slippage 0.03
  python copytrade.py --csv japeth_jun21.csv --copy-fraction 0.65 --slippage 0.03
  python copytrade.py --demo --copy-fraction 0.65 --slippage 0.03

CSV columns (header required): timestamp,side,asset,price,size,title
  timestamp : unix seconds OR ISO8601 (2026-06-21T09:05:00+03:00)
  side      : BUY | SELL
  asset     : outcome token id or any stable per-outcome key
  price     : 0..1 probability, or cents like 27 (auto-normalised)
  size      : number of shares
  title     : market name (optional)
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Bucharest")
except Exception:  # pragma: no cover
    TZ = timezone(timedelta(hours=3))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def norm_price(p: Any) -> float:
    """Accept 0..1 probability or cents (>1.5 -> /100). Clamp to [0,1]."""
    v = float(p)
    if v > 1.5:
        v /= 100.0
    return max(0.0, min(1.0, v))


def to_epoch(ts: Any) -> float:
    """Unix seconds from int/float/ISO8601. Naive ISO is treated as GMT+3."""
    if isinstance(ts, (int, float)):
        return float(ts)
    s = str(ts).strip()
    if s.isdigit():
        return float(s)
    s = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.timestamp()


# --------------------------------------------------------------------------- #
# core simulation  (pure, unit-tested offline)
# --------------------------------------------------------------------------- #
def simulate(fills: List[Dict[str, Any]], *, copy_fraction: float, slippage: float,
             start_ts: Optional[float] = None, end_ts: Optional[float] = None,
             resolution: Optional[Dict[str, float]] = None,
             mark: str = "last") -> Dict[str, Any]:
    """Return his vs. your copy PnL.

    Accounting is cash-flow based per outcome token:
      BUY  -> cash -= price*size ; shares += size
      SELL -> cash += price*size ; shares -= size
    End value of any open shares = resolution payout (0..1) if known, else the
    last traded price (mark='last') or 0 (mark='zero', realised-only view).

    Your fills use:  size  = his_size * copy_fraction
                     price = his_price + slippage  on BUY   (you pay more)
                             his_price - slippage  on SELL  (you receive less)
    """
    fl = [f for f in fills
          if (start_ts is None or to_epoch(f["timestamp"]) >= start_ts)
          and (end_ts is None or to_epoch(f["timestamp"]) <= end_ts)]
    fl.sort(key=lambda f: to_epoch(f["timestamp"]))

    book: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"his_sh": 0.0, "my_sh": 0.0, "his_cash": 0.0, "my_cash": 0.0,
                 "his_buy_notional": 0.0, "my_buy_notional": 0.0,
                 "last": None, "title": None})
    legs: List[Dict[str, Any]] = []

    for f in fl:
        a = str(f["asset"])
        side = str(f["side"]).upper()
        p = norm_price(f["price"])
        sz = float(f["size"])
        my_sz = sz * copy_fraction
        if side == "BUY":
            my_p = min(1.0, p + slippage)
            cash_sgn, sh_sgn = -1.0, +1.0
        elif side == "SELL":
            my_p = max(0.0, p - slippage)
            cash_sgn, sh_sgn = +1.0, -1.0
        else:
            continue

        b = book[a]
        b["title"] = f.get("title") or a
        b["his_cash"] += cash_sgn * p * sz
        b["my_cash"] += cash_sgn * my_p * my_sz
        b["his_sh"] += sh_sgn * sz
        b["my_sh"] += sh_sgn * my_sz
        if side == "BUY":
            b["his_buy_notional"] += p * sz
            b["my_buy_notional"] += my_p * my_sz
        b["last"] = p
        legs.append({"ts": to_epoch(f["timestamp"]), "title": b["title"], "side": side,
                     "his_price": p, "my_price": my_p, "his_size": sz, "my_size": my_sz})

    rows = []
    his_pnl = my_pnl = his_cost = my_cost = 0.0
    for a, b in book.items():
        if resolution and a in resolution:
            exit_p = resolution[a]
        elif mark == "zero":
            exit_p = 0.0
        else:
            exit_p = b["last"] or 0.0
        his = b["his_cash"] + b["his_sh"] * exit_p
        my = b["my_cash"] + b["my_sh"] * exit_p
        his_pnl += his
        my_pnl += my
        his_cost += b["his_buy_notional"]
        my_cost += b["my_buy_notional"]
        rows.append({"title": b["title"], "his_pnl": his, "my_pnl": my,
                     "his_open_sh": b["his_sh"], "my_open_sh": b["my_sh"],
                     "exit_price": exit_p,
                     "his_cost": b["his_buy_notional"], "my_cost": b["my_buy_notional"]})

    rows.sort(key=lambda r: r["my_pnl"])
    return {
        "n_fills": len(fl), "n_markets": len(book),
        "his_pnl": his_pnl, "my_pnl": my_pnl,
        "his_cost": his_cost, "my_cost": my_cost,
        "his_roi": (his_pnl / his_cost * 100.0) if his_cost else 0.0,
        "my_roi": (my_pnl / my_cost * 100.0) if my_cost else 0.0,
        "slippage_drag": his_pnl * copy_fraction - my_pnl,  # copy PnL lost purely to slippage
        "rows": rows, "legs": legs,
    }


# --------------------------------------------------------------------------- #
# input sources
# --------------------------------------------------------------------------- #
def load_csv(path: str) -> List[Dict[str, Any]]:
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not r.get("side"):
                continue
            out.append({"timestamp": r["timestamp"], "side": r["side"],
                        "asset": r.get("asset") or r.get("title") or "mkt",
                        "price": r["price"], "size": r["size"],
                        "title": r.get("title", "")})
    return out


def fetch_fills(address: str) -> List[Dict[str, Any]]:
    """Pull /activity for the wallet via the cached pmclient. Raises if the host
    is unreachable (the CLI turns that into a clear, actionable message)."""
    from scanner import pmclient as pm
    raw = pm.activity(address)
    return [{"timestamp": a["timestamp"], "side": a.get("side"),
             "asset": a.get("asset"), "price": a.get("price"),
             "size": a.get("size"), "title": a.get("title", "")} for a in raw]


def demo_fills() -> List[Dict[str, Any]]:
    """Illustrative only -- encodes the example prices from the goal so you can
    see the mechanics. These are NOT japeththegoat's real trades.

    'straight line for a couple of hours' = he accumulated a flat-priced contract
    around 0.27, then it moved to 0.15 where he exited. Size is a placeholder 100
    shares so dollar figures are concrete; scale linearly to his real size."""
    base = to_epoch("2026-06-21T09:00:00+03:00")
    return [
        {"timestamp": base + 0,    "side": "BUY",  "asset": "X", "price": 0.27, "size": 100, "title": "Demo market (illustrative)"},
        {"timestamp": base + 7200, "side": "SELL", "asset": "X", "price": 0.15, "size": 100, "title": "Demo market (illustrative)"},
    ]


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def render(res: Dict[str, Any], *, copy_fraction: float, slippage: float,
           label: str) -> str:
    L = [
        f"# Copy-trade PnL — {label}",
        f"sizing = {copy_fraction:.0%} of his size | adverse slippage = {slippage*100:.0f}c per fill",
        f"fills copied = {res['n_fills']} across {res['n_markets']} market(s)",
        "",
        "| Market | His PnL | Your PnL | His cost | Your cost | Open(his/you) | Exit |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in res["rows"]:
        L.append(f"| {r['title'][:40]} | ${r['his_pnl']:,.2f} | ${r['my_pnl']:,.2f} | "
                 f"${r['his_cost']:,.2f} | ${r['my_cost']:,.2f} | "
                 f"{r['his_open_sh']:.0f}/{r['my_open_sh']:.0f} | {r['exit_price']:.2f} |")
    L += [
        "",
        f"**His total PnL:** ${res['his_pnl']:,.2f}  (ROI {res['his_roi']:.1f}% on ${res['his_cost']:,.2f} deployed)",
        f"**Your copy PnL:** ${res['my_pnl']:,.2f}  (ROI {res['my_roi']:.1f}% on ${res['my_cost']:,.2f} deployed)",
        f"**Slippage drag** (vs. naive {copy_fraction:.0%}×his): ${res['slippage_drag']:,.2f} lost to the {slippage*100:.0f}c.",
    ]
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser(description="Polymarket copy-trade PnL simulator")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--address", help="target wallet 0x... (auto-fetch via data-api)")
    src.add_argument("--csv", help="his fills as CSV (timestamp,side,asset,price,size,title)")
    src.add_argument("--demo", action="store_true", help="run the illustrative example prices")
    p.add_argument("--start", help="copy fills at/after this time (ISO8601; naive=GMT+3)")
    p.add_argument("--end", help="copy fills at/before this time")
    p.add_argument("--copy-fraction", type=float, default=0.65)
    p.add_argument("--slippage", type=float, default=0.03, help="adverse slippage, probability units (0.03 = 3c)")
    p.add_argument("--mark", choices=["last", "zero"], default="last",
                   help="value open shares at last price (default) or zero (realised-only)")
    args = p.parse_args()

    start_ts = to_epoch(args.start) if args.start else None
    end_ts = to_epoch(args.end) if args.end else None

    if args.demo:
        fills, label = demo_fills(), "ILLUSTRATIVE (example prices, not real fills)"
    elif args.csv:
        fills, label = load_csv(args.csv), f"CSV {args.csv}"
    elif args.address:
        try:
            fills = fetch_fills(args.address)
        except Exception as exc:  # noqa: BLE001
            sys.exit(
                "Could not fetch fills from data-api.polymarket.com:\n"
                f"  {exc}\n\n"
                "This environment blocks outbound network (403), so the live pull\n"
                "can't run here. Two ways to get the real number:\n"
                "  1. Allowlist data-api.polymarket.com, then re-run --address.\n"
                "  2. Copy his fills off polymarket.com/profile/<addr> (Activity tab)\n"
                "     into a CSV and run:  python copytrade.py --csv his.csv "
                f"--start {args.start or '2026-06-21T09:00:00+03:00'} "
                f"--copy-fraction {args.copy_fraction} --slippage {args.slippage}"
            )
        label = f"wallet {args.address}"
    else:
        p.error("supply one of --address / --csv / --demo")
        return 2

    if not fills:
        sys.exit("No fills in range. Nothing to copy — supply fills or widen --start/--end.")

    res = simulate(fills, copy_fraction=args.copy_fraction, slippage=args.slippage,
                   start_ts=start_ts, end_ts=end_ts, mark=args.mark)
    print(render(res, copy_fraction=args.copy_fraction, slippage=args.slippage, label=label))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
