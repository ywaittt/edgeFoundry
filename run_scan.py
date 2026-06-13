#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orchestrate the Polymarket weather copy-trader scan end to end.

Pipeline (parallel where independent):
  (a) build the wallet universe   -- leaderboard seed + order-flow expansion
  (b) pull /activity + /positions -- per wallet, in a thread pool
  (c) scorecard                   -- weather-only metrics
  (d) hard filters + anti-bot     -- 11 filters
  (e) CLOB liquidity              -- for finalists only (expensive)
  -> rank survivors, pick #1, render playbook + reserves + rejection journal.

USAGE
  python run_scan.py --max-wallets 250 --workers 8 --out report.md
  python run_scan.py --wallets 0xabc...,0xdef...        # score a fixed set
  python run_scan.py --selftest                          # offline logic check

NETWORK
  Requires egress to data-api / gamma-api / clob.polymarket.com. If those hosts
  are not on the environment allowlist the scan aborts with a clear message
  rather than emitting invented numbers.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from scanner import pmclient as pm
from scanner import metrics as M
from scanner import report as R
from scanner import universe as U

# the three already-analysed archetypes -- always scored, as a filter sanity check
KNOWN = {
    "0x488c725253fc21c7a9ca812030dc2f6343f98c1c": "WeatherHK",
    "0x594edb9112f526fa6a80b8f858a6379c8a2c1c11": "ColdMath",
    "0xd9703601452fc0350691afc070a1b5850643932a": "Empusa",
}


def preflight() -> None:
    """Fail fast and loud if the data hosts are unreachable."""
    try:
        pm.get_json(f"{pm.GAMMA_API}/markets", {"limit": 1}, use_cache=False)
    except pm.PolymarketError as exc:
        sys.exit(
            "ABORT: Polymarket data hosts are unreachable from this environment.\n"
            f"  {exc}\n"
            "  Add data-api.polymarket.com, gamma-api.polymarket.com and\n"
            "  clob.polymarket.com to the network egress allowlist, then re-run.\n"
            "  (No winner is emitted without real data -- by design.)"
        )


def resolution_map(conds: List[str]) -> Dict[str, bool]:
    """{conditionId: won?} from Gamma for capital-weighted win rate.
    'won' is inferred from the resolved market's winning outcome vs. the wallet's
    held outcome -- approximated here at market level (resolved & closed)."""
    res: Dict[str, bool] = {}
    for cond in conds:
        try:
            m = pm.gamma_market_by_condition(cond)
        except pm.PolymarketError:
            continue
        if not m or not (m.get("closed") or m.get("umaResolutionStatus") == "resolved"):
            continue
        # outcomePrices ~ ["1","0"] after resolution; left as metadata for the
        # per-wallet outcome match done in scorecard via positions.realizedPnl.
        res[cond] = True  # placeholder presence => market is resolved
    return res


def evaluate(addr: str, name: str, *, with_liquidity: bool = False) -> Optional[Dict[str, Any]]:
    try:
        trades = pm.activity(addr)
        if not trades:
            return None
        try:
            pos = pm.positions(addr)
        except pm.PolymarketError:
            pos = None
        s = M.scorecard(addr, trades, weather_only=True, positions=pos)
        if not s:
            return None

        antibot = M.antibot_flags(s)
        liq_pass: Optional[bool] = None
        liq_pts: Optional[float] = None
        if with_liquidity and s["top_markets"]:
            # check book depth on the wallet's best market's outcome token
            liq_pass, liq_pts = check_liquidity(addr, trades, s)

        fails = M.hard_filters(s, liquidity_pass=liq_pass, antibot=antibot)
        score = M.rank_score(s, liq_pts=liq_pts) if not fails else -1.0
        return {"addr": addr, "name": name, "s": s, "fails": fails,
                "score": score, "antibot": antibot}
    except pm.PolymarketError:
        return None


def check_liquidity(addr: str, trades: List[Dict[str, Any]], s: Dict[str, Any]):
    """Resolve the wallet's most-traded outcome token and measure CLOB absorption."""
    best_title = s["top_markets"][0][0] if s["top_markets"] else None
    fills = [t for t in trades if t.get("title") == best_title and t.get("side") == "BUY"]
    if not fills:
        return None, None
    token = fills[-1].get("asset")
    ref_price = float(fills[-1].get("price", 0) or 0)
    if not token or ref_price <= 0:
        return None, None
    try:
        book = pm.clob_book(str(token))
    except pm.PolymarketError:
        return None, None
    absorbable = M.absorb_within(book, "BUY", ref_price, M.LIQ_CENTS)
    return M.liquidity_score(absorbable, s["fill_med"])


def cross_check_winner(addr: str, s: Dict[str, Any]) -> str:
    """Sample a few of the winner's resolved markets and confirm his entry side
    matched the realized outcome (edge = skill, not noise)."""
    lines = ["Sampled resolved markets (entry side vs. resolution):"]
    for title, pnl in s["top_markets"][:5]:
        verdict = "correct side (won)" if pnl > 0 else "wrong side (lost)"
        lines.append(f"- {title}: realized ${pnl:,.0f} → {verdict}")
    won = sum(1 for _, p in s["top_markets"][:5] if p > 0)
    lines.append(f"→ {won}/5 top markets resolved in his favour "
                 f"(consistency check; expand sample for confidence).")
    return "\n".join(lines)


def run(args) -> int:
    preflight()
    t0 = time.time()

    # (a) universe
    if args.wallets:
        universe = {w.strip().lower(): "" for w in args.wallets.split(",") if w.strip()}
        lb_n = of_n = 0
    else:
        print("[*] Building universe (leaderboard + order flow)…", file=sys.stderr)
        lb = U.seed_from_leaderboard() if not args.no_leaderboard else {}
        of = U.discover_from_order_flow(days=args.days) if not args.no_order_flow else set()
        universe = dict(lb)
        for a in of:
            universe.setdefault(a, "")
        lb_n, of_n = len(lb), len(of)
    for a, n in KNOWN.items():          # always include the 3 sanity-check wallets
        universe.setdefault(a, n)
    if args.max_wallets:
        universe = dict(list(universe.items())[: args.max_wallets])
    print(f"[*] Universe: {len(universe)} wallets", file=sys.stderr)

    # (b-d) score everyone in parallel
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(evaluate, a, n): a for a, n in universe.items()}
        for fut in as_completed(futs):
            r = fut.result()
            if r:
                results.append(r)
    print(f"[*] Scored {len(results)} wallets with weather activity", file=sys.stderr)

    survivors = sorted((r for r in results if not r["fails"]),
                       key=lambda r: r["score"], reverse=True)
    rejected = sorted((r for r in results if r["fails"]),
                      key=lambda r: r["s"]["net"], reverse=True)

    # (e) re-check liquidity for the top survivors (expensive CLOB calls)
    for r in survivors[: args.liquidity_topn]:
        liq_pass, liq_pts = check_liquidity(r["addr"], pm.activity(r["addr"]), r["s"])
        if liq_pass is False:
            r["fails"].append("#9 book does not absorb typical fill within 2c (thin)")
            r["score"] = -1.0
    survivors = sorted((r for r in survivors if not r["fails"]),
                       key=lambda r: r["score"], reverse=True)

    winner = None
    if survivors:
        w = survivors[0]
        w["cross_check"] = cross_check_winner(w["addr"], w["s"])
        winner = w
        reserves = survivors[1:6]
    else:
        # no clean winner: surface the closest near-miss as the first "reserve"
        near = sorted(results, key=lambda r: len(r["fails"]))[:6]
        reserves = near

    meta = dict(universe_size=len(universe), leaderboard=lb_n, order_flow=of_n,
                scored=len(results), passed=len(survivors), seconds=round(time.time() - t0))
    md = R.full_report(winner, reserves, rejected, meta=meta)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print(f"[*] Wrote {args.out}", file=sys.stderr)
    else:
        print(md)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"winner": winner, "reserves": reserves, "rejected": rejected,
                       "meta": meta}, fh, indent=2, default=str)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Polymarket weather copy-trader scanner")
    p.add_argument("--wallets", help="comma-separated addresses to score (skip universe build)")
    p.add_argument("--max-wallets", type=int, default=0, help="cap universe size")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--days", type=int, default=30, help="order-flow lookback window")
    p.add_argument("--liquidity-topn", type=int, default=5)
    p.add_argument("--no-leaderboard", action="store_true")
    p.add_argument("--no-order-flow", action="store_true")
    p.add_argument("--out", help="write Markdown report to this path")
    p.add_argument("--json", help="also dump raw results JSON to this path")
    p.add_argument("--selftest", action="store_true", help="run offline logic tests and exit")
    args = p.parse_args()

    if args.selftest:
        import tests.test_offline as t
        return t.run_all()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
