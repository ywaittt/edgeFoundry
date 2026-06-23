#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Offline tests for the copy-trade PnL math. Hand-computed expected values.
Run:  python -m tests.test_copytrade
"""
from __future__ import annotations

import sys
sys.path.insert(0, ".")
from copytrade import simulate, norm_price, to_epoch  # noqa: E402

F = 0.65
SLIP = 0.03


def approx(a, b, eps=1e-6):
    return abs(a - b) < eps


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return cond


def run_all() -> int:
    ok = True
    print("Copy-trade PnL self-test (hand-computed, no network)\n")

    # --- 1. the goal's example: buy 0.27 -> sell 0.15, 100 shares ---
    fills = [
        {"timestamp": 1000, "side": "BUY",  "asset": "X", "price": 0.27, "size": 100, "title": "m"},
        {"timestamp": 9000, "side": "SELL", "asset": "X", "price": 0.15, "size": 100, "title": "m"},
    ]
    r = simulate(fills, copy_fraction=F, slippage=SLIP)
    # his: -0.27*100 + 0.15*100 = -12.00
    # you: buy 0.30, sell 0.12, size 65 -> -0.30*65 + 0.12*65 = -11.70
    print("Example trade (buy 27c -> sell 15c, 100 sh):")
    ok &= _check("his PnL = -$12.00", approx(r["his_pnl"], -12.0))
    ok &= _check("your PnL = -$11.70", approx(r["my_pnl"], -11.70))
    ok &= _check("you lose 97.5% of a naive 65% scale (slippage offset by sizing)",
                 approx(r["my_pnl"], -11.70))
    print(f"    his=${r['his_pnl']:.2f}  you=${r['my_pnl']:.2f}  "
          f"drag=${r['slippage_drag']:.2f}\n")

    # --- 2. a WINNER shows slippage eating the edge ---
    win = [
        {"timestamp": 1, "side": "BUY",  "asset": "Y", "price": 0.27, "size": 100, "title": "w"},
        {"timestamp": 2, "side": "SELL", "asset": "Y", "price": 0.40, "size": 100, "title": "w"},
    ]
    r = simulate(win, copy_fraction=F, slippage=SLIP)
    # his: -27 + 40 = +13.00 ; you: buy .30 sell .37 sz65 -> -19.5 + 24.05 = +4.55
    print("Winner trade (buy 27c -> sell 40c, 100 sh):")
    ok &= _check("his PnL = +$13.00", approx(r["his_pnl"], 13.0))
    ok &= _check("your PnL = +$4.55 (slippage halves the edge)", approx(r["my_pnl"], 4.55))
    ok &= _check("your ROI < his ROI", r["my_roi"] < r["his_roi"])
    print(f"    his=${r['his_pnl']:.2f} ({r['his_roi']:.0f}%)  "
          f"you=${r['my_pnl']:.2f} ({r['my_roi']:.0f}%)\n")

    # --- 3. open position marked to resolution payout = 1.0 (he held a winner) ---
    held = [{"timestamp": 1, "side": "BUY", "asset": "Z", "price": 0.27, "size": 100, "title": "h"}]
    r = simulate(held, copy_fraction=F, slippage=SLIP, resolution={"Z": 1.0})
    # his: -27 + 100*1 = +73 ; you: buy .30 sz65 -> -19.5 + 65*1 = +45.5
    print("Open position resolved YES (buy 27c, settles $1):")
    ok &= _check("his PnL = +$73.00", approx(r["his_pnl"], 73.0))
    ok &= _check("your PnL = +$45.50", approx(r["my_pnl"], 45.5))
    print(f"    his=${r['his_pnl']:.2f}  you=${r['my_pnl']:.2f}\n")

    # --- 4. start-time filter excludes earlier fills ---
    r = simulate(fills, copy_fraction=F, slippage=SLIP, start_ts=5000)
    print("Start filter (only the SELL at ts=9000 is copied):")
    ok &= _check("only 1 fill copied", r["n_fills"] == 1)
    print(f"    n_fills={r['n_fills']}\n")

    # --- 5. helpers ---
    print("Helpers:")
    ok &= _check("cents normalise (27 -> 0.27)", approx(norm_price(27), 0.27))
    ok &= _check("prob passes through (0.27)", approx(norm_price(0.27), 0.27))
    ok &= _check("naive ISO parsed as GMT+3",
                 approx(to_epoch("2026-06-21T09:00:00"), to_epoch("2026-06-21T06:00:00+00:00")))
    print()

    print("RESULT:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run_all())
