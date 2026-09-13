"""Record order-book snapshots for the universe. Detection happens later, offline.

Each poll cycle requests every universe ticker in batches of <= 100 (one HTTP
call per batch; events are packed so their markets share a call and are seen
at the same instant). One JSON line per batch goes to
data/snapshots/YYYYMMDD_HH_<session>.jsonl (UTC hour, gzipped when the hour ends):

  {"type": "header", "session": 1789..., "batches": [[tickers], ...], "interval": 1.0, ...}
  {"type": "batch", "session": 1789..., "cycle": 17, "batch": 0,
   "t_send": ..., "t_recv": ..., "rtt": 0.13,
   "books": {ticker: {"yes": [[price, size], ...], "no": [...]}},
   "missing": [tickers requested but not returned]}          # only if any

"books" holds only books that CHANGED since that ticker's previous poll, which
keeps a night of recording to a few hundred MB. A requested ticker that is not
in "books" and not in "missing" was polled and unchanged.
load_data.iter_snapshots() rebuilds full books. Levels keep the API order (ascending, best bid last), trimmed to the
best --depth levels. A new "session" means the recorder restarted: treat the
gap as missing data, not as "nothing changed".

    python -m mispricing.recorder --interval 1.0 --hours 16
"""
import argparse
import gzip
import json
import shutil
import signal
import time
from pathlib import Path

from .api import MAX_ORDERBOOK_BATCH, KalshiClient


def pack_batches(events, size=MAX_ORDERBOOK_BATCH):
    """Greedy packing: keep each event's markets in one request where possible."""
    batches, current = [], []
    for ev in events:
        tickers = [m["ticker"] for m in ev["markets"]]
        if current and len(current) + len(tickers) > size:
            batches.append(current)
            current = []
        current.extend(tickers)
        while len(current) > size:
            batches.append(current[:size])
            current = current[size:]
    if current:
        batches.append(current)
    return batches


def compress(path):
    with path.open("rb") as src, gzip.open(path.with_name(path.name + ".gz"), "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


def trim(levels, depth):
    return sorted(levels, key=lambda lvl: float(lvl[0]))[-depth:]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--universe", default="data/universe.json")
    ap.add_argument("--interval", type=float, default=1.0, help="target seconds between poll cycles")
    ap.add_argument("--depth", type=int, default=10, help="levels kept per side")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--out-dir", default="data/snapshots")
    args = ap.parse_args()

    universe = json.loads(Path(args.universe).read_text())
    batches = pack_batches(universe["events"])
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = KalshiClient(max_requests_per_sec=15)

    stop = False

    def handle_stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    session = int(time.time())
    deadline = time.monotonic() + args.hours * 3600
    last = {}  # ticker -> last recorded book, for change detection
    header = {"type": "header", "session": session, "batches": batches, "interval": args.interval,
              "depth": args.depth, "universe_created_utc": universe["created_utc"]}
    cycle, overruns, t_start, current_path = 0, 0, time.monotonic(), None
    print(f"session {session}: {universe['n_markets']} markets in {len(batches)} batches, "
          f"interval {args.interval}s, depth {args.depth}, writing to {out_dir}/", flush=True)

    while not stop and time.monotonic() < deadline:
        cycle_start = time.monotonic()
        # Session in the name: a restart within the same hour must not overwrite the earlier file.
        path = out_dir / time.strftime(f"%Y%m%d_%H_{session}.jsonl", time.gmtime())
        if path != current_path:
            if current_path is not None:
                compress(current_path)
            current_path = path
            with path.open("a") as f:
                f.write(json.dumps(header) + "\n")
        n_changed = 0
        with path.open("a") as f:
            for b, tickers in enumerate(batches):
                try:
                    books, timing = client.orderbooks(tickers)
                except Exception as exc:  # log and keep recording; a gap is data too
                    f.write(json.dumps({"type": "batch", "session": session, "cycle": cycle, "batch": b,
                                        "t_recv": time.time(), "error": str(exc)[:300]}) + "\n")
                    continue
                changed = {}
                for t, book in books.items():
                    book = {"yes": trim(book["yes"], args.depth), "no": trim(book["no"], args.depth)}
                    if last.get(t) != book:
                        changed[t] = book
                        last[t] = book
                n_changed += len(changed)
                rec = {"type": "batch", "session": session, "cycle": cycle, "batch": b, **timing, "books": changed}
                missing = sorted(set(tickers) - set(books))
                if missing:
                    rec["missing"] = missing
                f.write(json.dumps(rec) + "\n")

        elapsed = time.monotonic() - cycle_start
        if elapsed > args.interval:
            overruns += 1
        if cycle % 60 == 0:
            rate = (cycle + 1) / (time.monotonic() - t_start)
            print(f"cycle {cycle}: {elapsed:.2f}s, {n_changed} books changed, "
                  f"{rate:.2f} cycles/s, overruns {overruns}", flush=True)
        cycle += 1
        time.sleep(max(0.0, args.interval - elapsed))

    if current_path is not None:
        compress(current_path)
    print(f"stopped after {cycle} cycles", flush=True)


if __name__ == "__main__":
    main()
