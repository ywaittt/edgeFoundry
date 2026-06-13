"""
Polymarket weather copy-trader scanner.

Scans the universe of weather/climate traders on Polymarket and returns a single
finalist that passes 11 hard filters, plus reserves and a rejection journal.

All metrics are computed from live Polymarket APIs. Nothing is hard-coded or
invented -- if the network is unavailable the scan refuses to emit a winner.
"""

__all__ = ["pmclient", "metrics", "report"]
