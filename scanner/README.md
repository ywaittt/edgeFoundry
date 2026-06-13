# Polymarket Weather Copy-Trader Scanner

Scans the universe of weather/climate traders on Polymarket and returns **one**
finalist that passes all 11 hard filters, plus 3–5 reserves and a rejection
journal. Built for a copier with a ~$500 bankroll in **GMT+3 (Europe/Bucharest)**
who wants to mirror the *side and timing* of a clean directional forecaster.

> **Every metric is computed from live Polymarket data.** Nothing is hard-coded
> or invented. If the data hosts are unreachable, the scan aborts instead of
> emitting a winner (see *Network* below).

## Run

```bash
pip install -r requirements.txt

# full scan: build universe (leaderboard + order flow), score, rank, report
python run_scan.py --max-wallets 250 --workers 8 --out report.md --json report.json

# score a fixed set of wallets (skips universe build)
python run_scan.py --wallets 0xabc...,0xdef...

# offline logic check (no network) — proves the filters reject the 3 archetypes
python run_scan.py --selftest
```

## Network requirement (current blocker)

The scan needs egress to:

| Host | Used for |
|---|---|
| `data-api.polymarket.com` | `/activity`, `/positions`, `/trades` |
| `gamma-api.polymarket.com` | market metadata, tags, resolution, volume |
| `clob.polymarket.com` | order-book depth (real liquidity, filter #9) |
| `lb-api.polymarket.com` *(optional)* | leaderboard seed |

In the sandbox where this was written, **all outbound hosts are 403-blocked by
the egress allowlist** (`curl` and WebFetch both fail). The scan therefore can't
fetch live data here; `preflight()` detects this and aborts with instructions
rather than guessing. Add the hosts above to the environment's network egress
settings and re-run — the report is then produced end to end with no code
changes.

## Pipeline

1. **Universe** (`universe.py`) — leaderboard seed across
   `{monthly,weekly,all} × {profit,volume}` **+** order-flow expansion (wallets
   that traded the most active weather markets in the last ~30 days). Deduped.
2. **Scorecard** (`metrics.scorecard`) — weather-only fills →
   efficiency (net/volume), median hold, rebuy-after-sell ratio, PnL
   concentration, distinct resolved markets, capital-weighted win rate, account
   age, bond-style share, GMT+3 buy-hour histogram + awake share. Realized PnL
   prefers `/positions.realizedPnl`, falling back to the sell−buy proxy.
3. **Filters** (`metrics.hard_filters`) — the 11 hard gates; any failure ⇒
   rejected with the exact reason.
4. **Anti-bot/wash** (`metrics.antibot_flags`, `linked_wallet_flags`) — two-sided
   symmetric quoting, sub-minute round-trips, churn, circular flow between linked
   wallets.
5. **Liquidity** (`metrics.absorb_within`) — CLOB book depth: USD absorbable
   within 2¢ of the wallet's fill price vs. his typical fill (filter #9). Run for
   finalists only.
6. **Rank** (`metrics.rank_score`) — 0–100 over the 6 weighted dimensions; #1
   clean survivor = winner. Winner gets an entry-side cross-check.

## The 11 hard filters

| # | Filter | Threshold |
|---|---|---|
| 1 | Realized weather PnL | $1,000–$20,000 |
| 2 | Efficiency = net/volume | ≥ 15% |
| 3 | Rebuy-after-sell ratio | < 0.30 |
| 4 | Median hold | ≥ 6 h |
| 5 | PnL concentration (top-3) | ≤ 55% |
| 6 | Account age | ≥ 90 days |
| 7 | Distinct resolved markets | ≥ 40 |
| 8 | Win rate (capital-weighted) | ≥ 55% |
| 9 | Liquidity (book absorbs typical fill ≤ 2¢) | pass |
| 10 | Not bond-style (PnL parked ≥95¢) | < 40% |
| 11 | Not bot/MM/wash | no flags |

Thresholds live at the top of `metrics.py`.

## Assumptions & things to verify against docs

- **Endpoint shapes** in `pmclient.py` marked `# VERIFY` — especially the
  leaderboard host/params (`lb-api`) and `/trades` counterparty fields used by
  the wash check. If the leaderboard endpoint is unavailable, order-flow
  discovery alone carries the universe.
- **Account age** uses the earliest weather fill timestamp as a proxy for the
  profile join date (true join date needs the profile API / on-chain first tx).
- **Win rate** is capital-weighted over markets Gamma reports as resolved;
  per-outcome win/loss is taken from `/positions.realizedPnl`. The
  `resolution_map` placeholder marks resolved markets — wire it to each market's
  winning outcome for a stricter check if needed.
- **Sizing map** assumes the trader's median fill ≈ 5% of his working bankroll
  to derive the proportional copy ratio for $500; adjust if his bankroll is known.
