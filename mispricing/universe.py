"""Choose which Kalshi markets to watch and save the choice to data/universe.json.

Only two event structures can carry the violations we test:

  ladder       one event, >= 3 markets of the form "X above K" (strike_type
               greater / greater_or_equal) or "X below K" (less / less_or_equal),
               same underlying and expiry, one distinct K per market.
  outcome_set  one event Kalshi flags mutually_exclusive: at most one market
               resolves YES. The flag does NOT promise exhaustiveness -- read
               rules_primary before trusting a long basket.

Each event also records its OTHER markets (not active at selection time, e.g.
players already knocked out of a tournament) with their close times. Without
them, today's market list applied to last week's data is lookahead: the
2 US Open finalists look like a complete outcome set a week before the final.

Whole events are kept or dropped, never single markets: dropping one leg of an
outcome set would make its sum meaningless. Liquidity thresholds are CLI
arguments because they decide what gets counted; they are reported with the
results.

    python -m mispricing.universe --min-event-volume-24h 5000 --max-markets 300
"""
import argparse
import collections
import json
import time
from pathlib import Path

from .api import MAX_ORDERBOOK_BATCH, KalshiClient

LADDER_UP = {"greater", "greater_or_equal"}
LADDER_DOWN = {"less", "less_or_equal"}


def strike_of(market):
    """The K in "X above K" is floor_strike; in "X below K" it is cap_strike."""
    return market.get("floor_strike") if market.get("strike_type") in LADDER_UP else market.get("cap_strike")


def classify(event, markets):
    """Return (structure or None, reason)."""
    if len(markets) < 2:
        return None, "too_few_markets"
    if event.get("mutually_exclusive"):
        return "outcome_set", "mutually_exclusive"
    strike_types = {m.get("strike_type") for m in markets}
    if len(markets) < 3 or len(strike_types) != 1 or not (strike_types <= LADDER_UP or strike_types <= LADDER_DOWN):
        return None, "not_a_ladder"
    strikes = [strike_of(m) for m in markets]
    # One rung per strike. Duplicates mean the event mixes several underlyings
    # (e.g. sports spreads hold "Texas wins by > K" AND "Ohio St. wins by > K";
    # BTC "above 85000 by <date>" varies the date, not K). Comparing across
    # those produces fake monotonicity violations.
    if None in strikes or len(set(strikes)) != len(strikes):
        return None, "ladder_duplicate_strikes"
    return "ladder", "ladder"


def other_markets(event, markets):
    active = {m["ticker"] for m in markets}
    return [{k: m.get(k) for k in ("ticker", "status", "result", "open_time", "close_time")}
            for m in event.get("markets") or [] if m["ticker"] not in active]


def event_record(event, markets, structure):
    return {
        "event_ticker": event["event_ticker"],
        "series_ticker": event["series_ticker"],
        "title": event.get("title"),
        "category": event.get("category"),
        "structure": structure,
        "mutually_exclusive": event.get("mutually_exclusive"),
        "volume_24h": sum(float(m.get("volume_24h_fp") or 0) for m in markets),
        "rules_primary": markets[0].get("rules_primary"),
        "markets": [
            {
                "ticker": m["ticker"],
                "strike_type": m.get("strike_type"),
                "floor_strike": m.get("floor_strike"),
                "cap_strike": m.get("cap_strike"),
                "yes_sub_title": m.get("yes_sub_title"),
                "volume_24h": float(m.get("volume_24h_fp") or 0),
                "open_time": m.get("open_time"),
                "close_time": m.get("close_time"),
            }
            for m in markets
        ],
        "other_markets": other_markets(event, markets),
    }


def refresh_other_markets(path):
    universe = json.loads(Path(path).read_text())
    client = KalshiClient()
    for ev in universe["events"]:
        data, _ = client.get(f"/events/{ev['event_ticker']}", {"with_nested_markets": "true"})
        chosen = {m["ticker"] for m in ev["markets"]}
        markets = data.get("markets") or data["event"].get("markets") or []
        ev["other_markets"] = [{k: m.get(k) for k in ("ticker", "status", "result", "open_time", "close_time")}
                               for m in markets if m["ticker"] not in chosen]
        print(f"  {ev['event_ticker']:35s} {len(ev['markets']):3d} chosen, {len(ev['other_markets']):3d} other")
    Path(path).write_text(json.dumps(universe, indent=1))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-event-volume-24h", type=float, default=1000, help="contracts traded in 24h, summed over the event")
    ap.add_argument("--max-markets", type=int, default=300, help="total markets to watch (sets the poll interval)")
    ap.add_argument("--max-markets-per-event", type=int, default=60)
    ap.add_argument("--max-markets-per-category", type=int, default=0, help="0 = no cap")
    ap.add_argument("--category", action="append", help="keep only these categories (repeatable), e.g. Economics")
    ap.add_argument("--out", default="data/universe.json")
    ap.add_argument("--refresh-other-markets", action="store_true",
                    help="only (re)fill other_markets in the existing --out file; keeps the selection")
    args = ap.parse_args()

    if args.refresh_other_markets:
        refresh_other_markets(args.out)
        return

    client = KalshiClient()
    candidates = []
    counts = collections.Counter()
    for event in client.open_events():
        markets = [m for m in event.get("markets") or [] if m.get("status") == "active"]
        structure, reason = classify(event, markets)
        counts[(event.get("category"), reason)] += 1
        if structure is None or len(markets) > args.max_markets_per_event:
            continue
        if args.category and event.get("category") not in args.category:
            continue
        rec = event_record(event, markets, structure)
        if rec["volume_24h"] >= args.min_event_volume_24h:
            candidates.append(rec)

    # Most-traded events first, until the market budget is spent.
    candidates.sort(key=lambda r: r["volume_24h"], reverse=True)
    chosen, n_markets = [], 0
    per_category = collections.Counter()
    for rec in candidates:
        size = len(rec["markets"])
        if n_markets + size > args.max_markets:
            continue
        if args.max_markets_per_category and per_category[rec["category"]] + size > args.max_markets_per_category:
            continue
        chosen.append(rec)
        n_markets += size
        per_category[rec["category"]] += size

    out = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "params": vars(args),
        "n_markets": n_markets,
        "events": chosen,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1))

    print("all open events by (category, classification):")
    for (cat, reason), n in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
        print(f"  {n:6d}  {cat!s:25s} {reason}")
    print(f"\n{len(candidates)} events pass the volume filter; chose {len(chosen)} events / {n_markets} markets")
    by_type = collections.Counter(r["structure"] for r in chosen)
    print(f"  {dict(by_type)}  by category {dict(per_category)}")
    print(f"  -> {-(-n_markets // MAX_ORDERBOOK_BATCH)} orderbook requests per poll")
    for r in chosen[:40]:
        print(f"  {r['structure']:11s} {len(r['markets']):3d} mkts  vol24h {r['volume_24h']:>12,.0f}  {r['event_ticker']:35s} {r['title'][:50]}")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
