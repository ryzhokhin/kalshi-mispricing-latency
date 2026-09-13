"""Execution replay: would the detected violations actually have filled? Nothing is sent anywhere.

Simulated strategy:
  1. A violation that was NOT present at the event's previous poll appears, and
     it is positive after fees at its best size q*. Plan one order per leg: an
     immediate-or-cancel (IOC) limit BUY of q* contracts at the worst price the
     detection needed on that leg. IOC fills whatever is available at or better
     than the limit at that instant and cancels the rest; it never rests.
  2. The orders reach the exchange R seconds after the book arrived, with
     R = compute + order round trip, each resampled from latency.py's measurements.
  3. Every leg fills independently against the book at that moment.

We only see books once per poll, so "the book at that moment" is the event's
first poll at or after t + R:
  - legs' books unchanged since detection -> nothing traded in between, the
    fill is CERTAIN at detection prices;
  - legs' books changed -> we fill against the changed book, which is
    PESSIMISTIC: the change may have landed after our order would have.

Size: q = min(q*, --max-baskets). The detector's best size can be tens of
thousands of contracts on a deep book; one such signal would dominate the
dollar P&L and no small account could trade it. Planned P&L is re-priced at the
capped size against the detection-time book.

Leg risk: h = the smallest fill across legs is hedged and pays W per basket for
sure. Extra contracts on a leg are unhedged; they are unwound immediately into
that leg's bids (a second fee), and anything no bid absorbs is written off.
    P&L = W*h - cost - buy fees + unwind proceeds - sell fees

Not modeled, all of which make these results OPTIMISTIC: other traders racing
for the same quotes (queue priority), capital locked until settlement, and the
exhaustiveness risk of set_long baskets.

    python -m mispricing.replay
"""
import argparse
import csv
import json
import random
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .detect import ONE, ZERO, quotes_from_book, take
from .fees import load_series_fees, order_fee
from .storage import iter_snapshots, load_universe

REPLAY_COLUMNS = ["signature", "kind", "event_ticker", "session", "cycle", "t_detect", "reaction_s", "t_eval",
                  "book_unchanged", "outcome", "planned_q", "planned_net", "hedged", "unwound", "written_off",
                  "pnl", "shortfall", "min_leg_volume_24h", "max_leg_spread", "top_size"]


# ---- pure pieces -----------------------------------------------------------------------

def parse_legs(signature):
    """'ladder|YES:T1|NO:T2' -> [('YES', 'T1'), ('NO', 'T2')]"""
    return [tuple(part.split(":", 1)) for part in signature.split("|")[1:]]


def asks_for(quotes, side):
    return quotes.yes_asks if side == "YES" else quotes.no_asks


def bids_for(quotes, side):
    """Selling YES hits YES bids, and a YES bid at b is a NO ask at 1 - b (and vice versa)."""
    opposite = quotes.no_asks if side == "YES" else quotes.yes_asks
    return tuple((ONE - price, size) for price, size in opposite)


def ioc_buy(asks, limit, q):
    """Fills of an IOC limit buy for q contracts: take asks priced <= limit, best first."""
    fills, left = [], q
    for price, size in asks:
        if left <= 0 or price > limit:
            break
        n = min(size, left)
        fills.append((price, n))
        left -= n
    return fills


def ioc_sell(bids, q):
    """Sell q contracts into bids at any price (unwinding risk, not seeking edge)."""
    fills, left = [], q
    for price, size in bids:
        if left <= 0:
            break
        n = min(size, left)
        fills.append((price, n))
        left -= n
    return fills


def plan_orders(legs, quotes, q):
    """[(side, ticker, limit)]: the limit is the worst level a q-contract buy used at detection."""
    return [(side, ticker, take(asks_for(quotes[ticker], side), q)[-1][0]) for side, ticker in legs]


def settle(plan, q, later_quotes, payout, params):
    qty, cost, buy_fees = [], ZERO, ZERO
    for side, ticker, limit in plan:
        later = later_quotes.get(ticker)
        fills = ioc_buy(asks_for(later, side), limit, q) if later else []
        qty.append(sum((n for _, n in fills), ZERO))
        cost += sum((p * n for p, n in fills), ZERO)
        buy_fees += order_fee(fills, params) if fills else ZERO
    hedged = min(qty)

    proceeds = sell_fees = unwound = written_off = ZERO
    for (side, ticker, _), n in zip(plan, qty):
        extra = n - hedged
        if extra <= 0:
            continue
        later = later_quotes.get(ticker)
        sells = ioc_sell(bids_for(later, side), extra) if later else []
        sold = sum((s for _, s in sells), ZERO)
        proceeds += sum((p * s for p, s in sells), ZERO)
        sell_fees += order_fee(sells, params) if sells else ZERO
        unwound += sold
        written_off += extra - sold

    if hedged == q:
        outcome = "full"
    elif hedged > 0:
        outcome = "partial"
    elif any(n > 0 for n in qty):
        outcome = "legged"  # one side filled, the other did not: pure leg risk
    else:
        outcome = "miss"
    pnl = payout * hedged - cost - buy_fees + proceeds - sell_fees
    return {"outcome": outcome, "hedged": hedged, "unwound": unwound, "written_off": written_off, "pnl": pnl}


# ---- streaming over a recording (plumbing) --------------------------------------------------

def payout_for(kind, n_legs):
    return Decimal(n_legs - 1) if kind == "set_short" else ONE


class ReplaySimulator:
    """Streaming execution replay. Call step() once per assembled batch, in order.

    step(rec, quotes, rows_by_event) settles orders that have landed by this poll
    and turns new fee-positive detections into orders. Returns (settled, placed):
    settled = result rows, placed = order rows (signature, t_detect, planned P&L).
    """

    def __init__(self, fee_params, event_of, latency, max_gap_s=3.0, seed=11, max_baskets=Decimal(500)):
        self.fee_params, self.event_of, self.latency = fee_params, event_of, latency
        self.max_gap_s, self.max_baskets = max_gap_s, max_baskets
        self.rng = random.Random(seed)
        self.prev_present, self.last_seen, self.pending = {}, {}, defaultdict(list)

    def step(self, rec, quotes, rows_by_event):
        settled, placed = [], []
        for ev_ticker in {self.event_of[t]["event_ticker"] for t in rec["books"] if t in self.event_of}:
            key = (rec["session"], ev_ticker)
            # 1) orders that have landed by now are filled against this poll's books
            still = []
            for order in self.pending[key]:
                legs_present = all(t in quotes for _, t, _ in order["plan"])
                if rec["t_recv"] - order["t_detect"] > self.max_gap_s + order["reaction"] or not legs_present:
                    continue  # no usable book close enough in time: drop, do not guess
                if rec["t_recv"] < order["t_detect"] + order["reaction"]:
                    still.append(order)
                    continue
                unchanged = all(rec["books"][t] is order["books"][t] for _, t, _ in order["plan"])
                res = settle(order["plan"], order["q"], quotes, order["payout"], order["params"])
                settled.append({**order["row"], "t_eval": f"{rec['t_recv']:.3f}", "book_unchanged": unchanged,
                                "reaction_s": f"{order['reaction']:.3f}", **res,
                                "shortfall": res["pnl"] - order["planned_net"]})
            self.pending[key] = still

            # 2) new, fee-positive violations at this poll become orders
            ev = self.event_of[next(t for t in rec["books"]
                                    if t in self.event_of and self.event_of[t]["event_ticker"] == ev_ticker)]
            rows = rows_by_event.get(ev_ticker, [])
            gap = rec["t_recv"] - self.last_seen.get(key, float("-inf"))
            before = self.prev_present.get(key, set()) if gap <= self.max_gap_s else set()
            for r in rows:
                if r["signature"] in before or Decimal(r["best_net"]) <= 0:
                    continue
                legs = parse_legs(r["signature"])
                q = min(Decimal(r["best_q"]), self.max_baskets)
                plan = plan_orders(legs, quotes, q)
                payout = payout_for(r["kind"], len(legs))
                params = self.fee_params[ev["series_ticker"]]
                planned = settle(plan, q, quotes, payout, params)["pnl"]  # the same orders against the detection book
                if planned <= 0:
                    continue  # capping the size removed the edge (per-order fee rounding bites small sizes)
                reaction = self.rng.choice(self.latency["compute_batch"]) + self.rng.choice(self.latency["post_total"])
                row = {"signature": r["signature"], "kind": r["kind"], "event_ticker": ev_ticker,
                       "session": rec["session"], "cycle": rec["cycle"], "t_detect": f"{rec['t_recv']:.3f}",
                       "planned_q": q, "planned_net": planned,
                       "min_leg_volume_24h": r["min_leg_volume_24h"], "max_leg_spread": r["max_leg_spread"],
                       "top_size": r["top_size"]}
                self.pending[key].append({"plan": plan, "q": q, "payout": payout, "params": params,
                                          "t_detect": rec["t_recv"], "reaction": reaction,
                                          "books": {t: rec["books"][t] for _, t in legs},
                                          "planned_net": planned, "row": row})
                placed.append(row)
            self.prev_present[key] = {r["signature"] for r in rows}
            self.last_seen[key] = rec["t_recv"]
        return settled, placed


class QuoteCache:
    """quotes_from_book once per distinct book object."""

    def __init__(self):
        self.cache = {}

    def __call__(self, books):
        out = {}
        for t, b in books.items():
            hit = self.cache.get(t)
            if hit is None or hit[0] is not b:
                hit = self.cache[t] = (b, quotes_from_book(b))
            out[t] = hit[1]
        return out


def run_replay(universe_path, fees_path, snapshot_dir, violations_path, latency_path, max_gap_s=3.0, seed=11,
               max_baskets=Decimal(500)):
    universe = load_universe(universe_path)
    event_of = {m["ticker"]: ev for ev in universe["events"] for m in ev["markets"]}
    latency = json.loads(Path(latency_path).read_text())["samples"]
    sim = ReplaySimulator(load_series_fees(fees_path), event_of, latency, max_gap_s, seed, max_baskets)

    detections = defaultdict(list)  # (session, cycle) -> {event: rows}
    with open(violations_path) as f:
        for r in csv.DictReader(f):
            detections[(int(r["session"]), int(r["cycle"]), r["event_ticker"])].append(r)

    results, quotes_of = [], QuoteCache()
    for rec in iter_snapshots(snapshot_dir):
        if "error" in rec:
            continue
        rows_by_event = {ev: detections.get((rec["session"], rec["cycle"], ev), [])
                         for ev in {event_of[t]["event_ticker"] for t in rec["books"] if t in event_of}}
        settled, _ = sim.step(rec, quotes_of(rec["books"]), rows_by_event)
        results.extend(settled)
    return results


def summarize_replay(results):
    n = len(results)
    by = defaultdict(int)
    for r in results:
        by[(r["outcome"], r["book_unchanged"])] += 1
    certain = [r for r in results if r["book_unchanged"]]
    return {
        "signals": n,
        "outcomes": {f"{o} ({'book unchanged' if u else 'book changed'})": c for (o, u), c in sorted(by.items())},
        "full_fill_rate": sum(r["outcome"] == "full" for r in results) / n if n else None,
        "leg_risk_incidents": sum(r["outcome"] in ("legged", "partial") for r in results),
        "planned_pnl": sum((r["planned_net"] for r in results), ZERO),
        "simulated_pnl": sum((r["pnl"] for r in results), ZERO),
        "simulated_pnl_certain_only": sum((r["pnl"] for r in certain), ZERO),
        "written_off_contracts": sum((r["written_off"] for r in results), ZERO),
    }


def write_replay(results, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REPLAY_COLUMNS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in REPLAY_COLUMNS} for r in results)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", default="data/universe.json")
    ap.add_argument("--series-fees", default="data/series_fees.json")
    ap.add_argument("--snapshots", default="data/snapshots")
    ap.add_argument("--violations", default="data/violations_snapshots.csv")
    ap.add_argument("--latency", default="data/latency.json")
    ap.add_argument("--out", default="data/replay_snapshots.csv")
    ap.add_argument("--max-baskets", type=Decimal, default=Decimal(500))
    args = ap.parse_args()
    results = run_replay(args.universe, args.series_fees, args.snapshots, args.violations, args.latency,
                         max_baskets=args.max_baskets)
    write_replay(results, args.out)
    for k, v in summarize_replay(results).items():
        print(f"  {k}: {v}")
    print(f"wrote {len(results)} simulated signals to {args.out}")


if __name__ == "__main__":
    main()
