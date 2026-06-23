# edgeFoundry

A co-pilot for prediction-market trading: collects data, scores traders, and
simulates copy-trade outcomes on Polymarket.

## Copy-trade PnL simulator (`copytrade.py`)

Answers: *"if I mirror trader X from time T with N% of his size and C cents of
adverse slippage, what is my PnL?"*

```bash
# illustrative run on example prices (no network needed)
python copytrade.py --demo --copy-fraction 0.65 --slippage 0.03

# real run, auto-fetch his fills (needs data-api.polymarket.com allowlisted)
python copytrade.py --address 0x<his_wallet> \
    --start 2026-06-21T09:00:00+03:00 --copy-fraction 0.65 --slippage 0.03

# real run from a CSV you paste off polymarket.com/profile/<addr> (Activity tab)
python copytrade.py --csv his_fills.csv \
    --start 2026-06-21T09:00:00+03:00 --copy-fraction 0.65 --slippage 0.03
```

**Slippage rule:** `--slippage 0.03` means every fill is 3c worse for you — you
BUY 3c above his price and SELL 3c below it (he buys 0.27 → you 0.30; he sells
0.15 → you 0.12). **Sizing:** `--copy-fraction 0.65` = 65% of his share size.
Open positions are valued at the last traded price (`--mark last`) or at a
known resolution payout; use `--mark zero` for a realised-only view.

Get the real number two ways: allowlist `data-api.polymarket.com` and use
`--address`, or fill `his_fills_template.csv` from the Activity tab you're
looking at and use `--csv`. The tool never invents fills — with no data it
refuses to print a PnL.

Self-test (offline, hand-computed): `python -m tests.test_copytrade`

## Weather copy-trader scanner (`scanner/`)

Scans the weather/climate trader universe and returns one finalist passing 11
hard filters, plus reserves and a rejection journal. See `scanner/README.md`.
Self-test: `python run_scan.py --selftest`.

> Both tools require egress to the Polymarket APIs (`data-api`, `gamma-api`,
> `clob.polymarket.com`). In a locked-down sandbox those hosts 403; the tools
> detect this and abort with instructions rather than guessing.
