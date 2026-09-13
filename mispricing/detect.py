"""No-arbitrage violation detectors, charged through fees.py.

One idea covers every detector: find a basket of contracts whose combined
payout is at least W dollars in EVERY outcome, and check whether it can be
bought for less than W after fees. Every leg is a taker buy at the ask of the
contract bought (selling YES at the bid == buying NO at 1 - bid).

  kind          legs bought                         guaranteed payout W
  ------------  ----------------------------------  ---------------------
  yes_no_cross  YES and NO of one market            1
  ladder        YES of the wider rung, NO of the     1   (2 if X lands
                narrower rung                            between strikes)
  set_long      YES of every outcome                1   ONLY if the set is exhaustive
  set_short     NO of every outcome                 n - 1

Lookahead guard: set_long is the only kind that needs EVERY outcome present.
It is evaluated at time t only if all the event's other markets had closed by
t and all chosen markets had opened. Ladders need no guard (any subset of a
monotone ladder is monotone) and neither does set_short (at most one YES among
any subset of mutually exclusive markets, so n - 1 NOs still pay).

"Wider" and "narrower": for "X above K" the event {X > K2} is inside
{X > K1} when K1 < K2, so the lower strike is wider. For "X below K" the
higher strike is wider.

Output: one CSV row per (violation, snapshot) -- episodes.py turns rows into
lifetimes. Two sources:

    python -m mispricing.detect --source snapshots   # recorded order books, with depth
    python -m mispricing.detect --source history     # 1-minute candles, top of book only
"""
import argparse
import csv
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .fees import load_series_fees, order_fee
from .storage import iter_snapshots, load_universe
from .universe import LADDER_UP, strike_of

ZERO, ONE = Decimal(0), Decimal(1)


# ---- quotes: the ask ladders of the two contracts in a market -----------------

@dataclass(frozen=True)
class Quotes:
    """Levels available to BUY from, best (cheapest) first: [(price, size), ...].
    size is None when depth is unknown (candles)."""
    yes_asks: tuple
    no_asks: tuple


def quotes_from_book(book):
    """Kalshi books hold bids only, ascending. A NO bid at q is a YES ask at 1 - q, and vice versa.
    Reversing the ascending bids gives the best bid first, i.e. the cheapest ask first."""
    def asks(opposite_bids):
        out = []
        for price, size in reversed(opposite_bids):
            p, s = Decimal(price), Decimal(size)
            if s > 0 and ZERO < p < ONE:
                out.append((ONE - p, s))
        return tuple(out)

    return Quotes(yes_asks=asks(book["no"]), no_asks=asks(book["yes"]))


def quotes_from_candle(yes_bid, yes_ask):
    """Top of book only. A bid of 0 or an ask of 1 means that side is EMPTY -- no level, never a price."""
    bid, ask = Decimal(yes_bid or 0), Decimal(yes_ask or 1)
    return Quotes(
        yes_asks=((ask, None),) if ZERO < ask < ONE else (),
        no_asks=((ONE - bid, None),) if ZERO < bid < ONE else (),
    )


# ---- basket P&L: the one computation every detector uses ----------------------

def take(levels, q):
    """The fills a taker order of q contracts gets walking `levels`."""
    fills, left = [], q
    for price, size in levels:
        if left <= 0:
            break
        n = left if size is None else min(size, left)
        fills.append((price, n))
        left -= n
    return fills


def basket_pnl(legs, payout, params, assumed_sizes=(Decimal(1), Decimal(100))):
    """Buy one unit of every leg per basket; each basket pays `payout` for sure.

    legs: list of ask ladders (best first). Returns None if there is no gross
    edge at the top of book, else a dict:
      gross_top   payout - sum of best asks, per basket (before fees)
      top_size    baskets available at the best level of every leg (None if unknown)
      q_max       baskets until the marginal gross edge hits zero (None if depth unknown)
      best_q      basket count with the highest net P&L, fees charged per leg per order
      best_net, best_fees, best_gross   dollars at best_q
      net_1       net dollars for 1 basket (None if not enough depth)
    """
    if any(not leg for leg in legs):
        return None  # an empty side is untradeable, never a zero price
    gross_top = payout - sum(leg[0][0] for leg in legs)
    if gross_top <= 0:
        return None

    depth_known = all(size is not None for leg in legs for _, size in leg)
    if depth_known:
        # Walk all legs together. Asks only get worse, so the marginal edge only
        # falls: stop at the first segment where it is <= 0.
        idx = [0] * len(legs)
        remaining = [leg[0][1] for leg in legs]
        q, breakpoints = ZERO, []
        while True:
            marginal = payout - sum(leg[i][0] for leg, i in zip(legs, idx))
            if marginal <= 0:
                break
            step = min(remaining)
            q += step
            breakpoints.append(q)
            exhausted = False
            for k, leg in enumerate(legs):
                remaining[k] -= step
                if remaining[k] == 0:
                    idx[k] += 1
                    if idx[k] == len(leg):
                        exhausted = True
                    else:
                        remaining[k] = leg[idx[k]][1]
            if exhausted:
                break
        q_max = q
        top_size = min(leg[0][1] for leg in legs)
        # Between breakpoints gross is linear in q and fees only round up, so
        # the optimum is at a breakpoint (or at 1 basket, the smallest order).
        candidates = sorted(set(breakpoints) | ({ONE} if q_max >= ONE else set()))
    else:
        q_max = top_size = None
        candidates = list(assumed_sizes)

    def evaluate(q):
        fills = [take(leg, q) for leg in legs]
        cost = sum(p * n for leg_fills in fills for p, n in leg_fills)
        fees = sum(order_fee(leg_fills, params) for leg_fills in fills)
        gross = payout * q - cost
        return gross, fees, gross - fees

    results = {q: evaluate(q) for q in candidates}
    best_q = max(results, key=lambda q: results[q][2])
    best_gross, best_fees, best_net = results[best_q]
    return {
        "gross_top": gross_top,
        "top_size": top_size,
        "q_max": q_max,
        "best_q": best_q,
        "best_gross": best_gross,
        "best_fees": best_fees,
        "best_net": best_net,
        "net_1": results[ONE][2] if ONE in results else None,
    }


# ---- detectors ------------------------------------------------------------------

def detect_yes_no_cross(ticker, q, params):
    pnl = basket_pnl([q.yes_asks, q.no_asks], ONE, params)
    return [("yes_no_cross", (("YES", ticker), ("NO", ticker)), pnl)] if pnl else []


def detect_ladder(event, quotes, params):
    """All violating (wider, narrower) pairs, not just neighbours: bids and asks
    differ, so rungs 1-2 and 2-3 can both be fine while 1-3 is violated."""
    rungs = [m for m in event["markets"] if m["ticker"] in quotes]
    ascending = sorted(rungs, key=strike_of)
    wide_to_narrow = ascending if rungs and rungs[0]["strike_type"] in LADDER_UP else ascending[::-1]

    found, seen = [], []  # seen: (best yes ask, market) of every wider rung so far
    min_wide_ask = None
    for narrow in wide_to_narrow:
        qn = quotes[narrow["ticker"]]
        if qn.no_asks:
            bid_narrow = ONE - qn.no_asks[0][0]
            # O(1) screen per rung: if even the cheapest wider ask is >= this bid,
            # no pair ending here can violate. Most rungs stop here.
            if min_wide_ask is not None and min_wide_ask < bid_narrow:
                for ask_wide, wide in seen:
                    if ask_wide < bid_narrow:
                        legs = [quotes[wide["ticker"]].yes_asks, qn.no_asks]
                        pnl = basket_pnl(legs, ONE, params)
                        if pnl:
                            found.append(("ladder", (("YES", wide["ticker"]), ("NO", narrow["ticker"])), pnl))
        if qn.yes_asks:
            ask = qn.yes_asks[0][0]
            seen.append((ask, narrow))
            min_wide_ask = ask if min_wide_ask is None else min(min_wide_ask, ask)
    return found


def unix(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def set_complete_from(event):
    """Earliest time at which the chosen markets are ALL the outcomes still possible."""
    if "other_markets" not in event:
        raise KeyError(f"{event['event_ticker']}: run `python -m mispricing.universe --refresh-other-markets` first")
    times = [unix(m["open_time"]) for m in event["markets"]]
    times += [unix(m["close_time"]) for m in event["other_markets"]]
    return max(times)


def detect_outcome_set(event, quotes, params, t):
    tickers = [m["ticker"] for m in event["markets"]]
    if any(tk not in quotes for tk in tickers):
        return []  # a missing leg breaks the set; never evaluate a partial basket
    n = len(tickers)
    found = []
    long_pnl = basket_pnl([quotes[tk].yes_asks for tk in tickers], ONE, params) if t >= set_complete_from(event) else None
    if long_pnl:
        found.append(("set_long", tuple(("YES", t) for t in tickers), long_pnl))
    short_pnl = basket_pnl([quotes[tk].no_asks for tk in tickers], Decimal(n - 1), params)
    if short_pnl:
        found.append(("set_short", tuple(("NO", t) for t in tickers), short_pnl))
    return found


def detect_event(event, quotes, params, t):
    found = []
    for m in event["markets"]:
        if m["ticker"] in quotes:
            found += detect_yes_no_cross(m["ticker"], quotes[m["ticker"]], params)
    if event["structure"] == "ladder":
        found += detect_ladder(event, quotes, params)
    else:
        found += detect_outcome_set(event, quotes, params, t)
    return found


# ---- running over data (plumbing) -----------------------------------------------

COLUMNS = ["source", "t", "session", "cycle", "event_ticker", "kind", "signature", "n_legs", "requires_exhaustive",
           "min_leg_volume_24h", "max_leg_spread", "gross_top", "top_size", "q_max", "best_q", "best_gross",
           "best_fees", "best_net", "net_1"]


def spread(q):
    """YES ask - YES bid = yes_ask + no_ask - 1. None when a side is empty (treat as maximally wide)."""
    return q.yes_asks[0][0] + q.no_asks[0][0] - ONE if q.yes_asks and q.no_asks else None


def to_rows(source, t, session, cycle, event, found, volume_24h, quotes):
    for kind, legs, pnl in found:
        spreads = [spread(quotes[ticker]) for ticker in {ticker for _, ticker in legs}]
        yield {
            "source": source, "t": f"{t:.3f}", "session": session, "cycle": cycle,
            "event_ticker": event["event_ticker"], "kind": kind,
            "signature": kind + "|" + "|".join(f"{side}:{ticker}" for side, ticker in legs),
            "n_legs": len(legs), "requires_exhaustive": kind == "set_long",
            "min_leg_volume_24h": min(volume_24h[ticker] for _, ticker in legs),
            "max_leg_spread": "" if None in spreads else str(max(spreads)),
            **{k: ("" if v is None else str(v)) for k, v in pnl.items()},
        }


class SnapshotDetector:
    """Streaming detection over assembled batches (storage.Assembler output).

    process(rec) -> [(event, violations, quotes_present)] for every event observed
    in the batch. Detection only re-runs for events whose books (or set of
    returned markets) changed; otherwise the previous result is reused, since
    identical books give identical violations.
    """

    def __init__(self, universe, fee_params):
        self.event_of = {m["ticker"]: ev for ev in universe["events"] for m in ev["markets"]}
        self.fee_params = fee_params
        self.last_book, self.quotes, self.cached = {}, {}, {}

    def process(self, rec):
        changed_events = set()
        books = {t: b for t, b in rec["books"].items() if t in self.event_of}
        for ticker, book in books.items():
            if self.last_book.get(ticker) is not book:
                self.last_book[ticker] = book
                self.quotes[ticker] = quotes_from_book(book)
                changed_events.add(self.event_of[ticker]["event_ticker"])
        present = {t: self.quotes[t] for t in books}  # only markets returned in THIS batch
        out = []
        for ev in {self.event_of[t]["event_ticker"]: self.event_of[t] for t in books}.values():
            members = frozenset(m["ticker"] for m in ev["markets"] if m["ticker"] in present)
            hit = self.cached.get(ev["event_ticker"])
            if ev["event_ticker"] in changed_events or hit is None or hit[0] != members:
                found = detect_event(ev, present, self.fee_params[ev["series_ticker"]], rec["t_recv"])
                hit = self.cached[ev["event_ticker"]] = (members, found)
            out.append((ev, hit[1], present))
        return out


def run_snapshots(universe, fee_params, snapshot_dir):
    volume_24h = {m["ticker"]: m["volume_24h"] for ev in universe["events"] for m in ev["markets"]}
    detector = SnapshotDetector(universe, fee_params)
    for rec in iter_snapshots(snapshot_dir):
        if "error" in rec:
            continue
        for ev, found, present in detector.process(rec):
            yield from to_rows("snapshots", rec["t_recv"], rec["session"], rec["cycle"], ev, found, volume_24h, present)


def run_history(universe, fee_params, candles_path):
    events = universe["events"]
    event_of = {m["ticker"]: ev for ev in events for m in ev["markets"]}
    volume_24h = {m["ticker"]: m["volume_24h"] for ev in events for m in ev["markets"]}
    by_minute = defaultdict(list)
    with open(candles_path) as f:
        for row in csv.DictReader(f):
            by_minute[int(row["end_period_ts"])].append(row)
    quotes, cached = {}, {}  # cached: event_ticker -> (event, violations)
    for minute in sorted(by_minute):
        # A minute with no candle for a market means its quote did not change: carry it forward.
        touched = {}
        for row in by_minute[minute]:
            quotes[row["ticker"]] = quotes_from_candle(row["yes_bid_close"], row["yes_ask_close"])
            touched[row["event_ticker"]] = event_of[row["ticker"]]
        for ev in touched.values():
            cached[ev["event_ticker"]] = (ev, detect_event(ev, quotes, fee_params[ev["series_ticker"]], minute))
        for ev, found in cached.values():
            yield from to_rows("history", minute, "", "", ev, found, volume_24h, quotes)


def summarize(path):
    rows = list(csv.DictReader(open(path)))
    print(f"\n{len(rows)} violation-rows (one per violation per snapshot) in {path}")
    print("  gross = before fees. net@1 = after fees for one basket. net@best = after fees at the best size")
    print("  (walking real depth for snapshots; ASSUMED 1 or 100 contracts for history, which has no depth).")
    print(f"  {'kind':13s} {'rows':>7s} | {'distinct violations':>19s}: {'gross':>5s} {'net@1':>5s} {'net@best':>8s} | "
          f"{'median gross':>12s}")
    for kind in ["yes_no_cross", "ladder", "set_short", "set_long"]:
        k = [r for r in rows if r["kind"] == kind]
        sigs = lambda rs: len({r["signature"] for r in rs})
        net1 = [r for r in k if r["net_1"] and Decimal(r["net_1"]) > 0]
        best = [r for r in k if Decimal(r["best_net"]) > 0]
        med_gross = f"{statistics.median(Decimal(r['gross_top']) for r in k) * 100:.2f}c" if k else "-"
        print(f"  {kind:13s} {len(k):7d} | {'':19s}  {sigs(k):5d} {sigs(net1):5d} {sigs(best):8d} | {med_gross:>12s}")


def write_violations(source, universe_path, fees_path, snapshot_dir, candles_path, out=None):
    universe = load_universe(universe_path)
    fee_params = load_series_fees(fees_path)  # KeyError on an unknown series beats a guessed fee
    out = out or f"data/violations_{source}.csv"
    rows = (run_snapshots(universe, fee_params, snapshot_dir) if source == "snapshots"
            else run_history(universe, fee_params, candles_path))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["snapshots", "history"], required=True)
    ap.add_argument("--universe", default="data/universe.json")
    ap.add_argument("--series-fees", default="data/series_fees.json")
    ap.add_argument("--snapshots", default="data/snapshots")
    ap.add_argument("--candles", default="data/history/candles_1m.csv")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = write_violations(args.source, args.universe, args.series_fees, args.snapshots, args.candles, args.out)
    summarize(out)


if __name__ == "__main__":
    main()
