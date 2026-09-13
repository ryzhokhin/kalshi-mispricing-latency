"""Download 1-minute candlesticks for every universe market over the past N days.

Why: the live recorder started on a weekend and only sees the future. Candles
give weekday history, at the cost of 1-minute resolution and no depth.

Output: data/history/candles_1m.csv, one row per (ticker, minute) that Kalshi
returned. Kalshi only returns minutes in which something happened, so a missing
minute means "unchanged" -- carry the previous quote forward, don't treat it as
missing. The first candle per market comes from include_latest_before_start so
there is a value to carry forward from. Prices stay as the API's decimal strings.

    python -m mispricing.history --days 7
"""
import argparse
import csv
import json
import time
from datetime import datetime
from pathlib import Path

from .api import KalshiClient

CHUNK_SECONDS = 86400  # one day of 1-minute candles per request

COLUMNS = [
    "event_ticker", "ticker", "end_period_ts",
    "yes_bid_open", "yes_bid_low", "yes_bid_high", "yes_bid_close",
    "yes_ask_open", "yes_ask_low", "yes_ask_high", "yes_ask_close",
    "price_close", "volume", "open_interest",
]


def to_row(event_ticker, ticker, c):
    bid, ask, price = c.get("yes_bid") or {}, c.get("yes_ask") or {}, c.get("price") or {}
    return {
        "event_ticker": event_ticker,
        "ticker": ticker,
        "end_period_ts": c["end_period_ts"],
        **{f"yes_bid_{k}": bid.get(f"{k}_dollars") for k in ("open", "low", "high", "close")},
        **{f"yes_ask_{k}": ask.get(f"{k}_dollars") for k in ("open", "low", "high", "close")},
        "price_close": price.get("close_dollars"),
        "volume": c.get("volume_fp"),
        "open_interest": c.get("open_interest_fp"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", default="data/universe.json")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--out", default="data/history/candles_1m.csv")
    args = ap.parse_args()

    universe = json.loads(Path(args.universe).read_text())
    client = KalshiClient(max_requests_per_sec=10)
    end_ts = int(time.time())
    start_ts = end_ts - int(args.days * 86400)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    n_rows = 0
    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        for i, ev in enumerate(universe["events"]):
            for m in ev["markets"]:
                opened = int(datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp())
                chunk_start = max(start_ts, opened)
                seen = set()  # chunk edges are inclusive, so dedupe on timestamp
                first = True
                while chunk_start < end_ts:
                    chunk_end = min(chunk_start + CHUNK_SECONDS, end_ts)
                    try:
                        candles = client.candlesticks(ev["series_ticker"], m["ticker"], chunk_start, chunk_end,
                                                      include_latest_before_start=first)
                    except Exception as exc:
                        print(f"  skip {m['ticker']} {chunk_start}: {exc}")
                        candles = []
                    for c in candles:
                        if c["end_period_ts"] not in seen:
                            seen.add(c["end_period_ts"])
                            writer.writerow(to_row(ev["event_ticker"], m["ticker"], c))
                            n_rows += 1
                    first = False
                    chunk_start = chunk_end
            print(f"[{i + 1}/{len(universe['events'])}] {ev['event_ticker']}: {n_rows} rows so far", flush=True)

    print(f"wrote {n_rows} rows to {args.out}")


if __name__ == "__main__":
    main()
