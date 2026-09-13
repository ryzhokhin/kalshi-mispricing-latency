"""Live analysis engine behind the dashboard.

Tails the recorder's output and, for every new batch, runs the same code as the
offline report: detection (charged through the fee model), episode tracking,
the stale-quote filter and the execution replay. On start it replays the whole
recording so far, then keeps up with new batches. Nothing here places orders.
"""
import json
import threading
import time
from collections import deque
from decimal import Decimal
from pathlib import Path

from mispricing.detect import SnapshotDetector, to_rows
from mispricing.episodes import (EpisodeTracker, budgets, capture_share, kaplan_meier, km_at, km_inputs,
                                 make_episode, quantile)
from mispricing.fees import load_series_fees
from mispricing.replay import QuoteCache, ReplaySimulator, parse_legs
from mispricing.robustness import BASELINE, passes
from mispricing.storage import Assembler, SnapshotTailer, load_universe

MAX_ALERTS = 500
MAX_EPISODES = 5000


def _f(x):
    return None if x in (None, "") else float(x)


class LiveEngine:
    def __init__(self, universe_path="data/universe.json", fees_path="data/series_fees.json",
                 snapshot_dir="data/snapshots", latency_path="data/latency.json", max_gap_intervals=3.0,
                 filters=None, max_baskets=Decimal(500)):
        self.universe = load_universe(universe_path)
        self.fee_params = load_series_fees(fees_path)
        self.event_of = {m["ticker"]: ev for ev in self.universe["events"] for m in ev["markets"]}
        self.title_of = {ev["event_ticker"]: ev["title"] for ev in self.universe["events"]}
        self.volume_24h = {m["ticker"]: m["volume_24h"] for ev in self.universe["events"] for m in ev["markets"]}
        self.latency = json.loads(Path(latency_path).read_text()) if Path(latency_path).exists() else None
        self.filters = filters or dict(BASELINE)
        self.max_gap_intervals = max_gap_intervals

        self.tailer = SnapshotTailer(snapshot_dir)
        self.assembler = Assembler()
        self.detector = SnapshotDetector(self.universe, self.fee_params)
        self.quotes_of = QuoteCache()
        self.interval = 1.0
        self.tracker = EpisodeTracker(max_gap=max_gap_intervals * self.interval)
        self.replay = (ReplaySimulator(self.fee_params, self.event_of, self.latency["samples"], max_gap_s=3.0,
                                       max_baskets=max_baskets) if self.latency else None)

        self.lock = threading.Lock()
        self.version = 0
        self.started_wall = time.time()
        self.caught_up = False
        self.episodes = deque(maxlen=MAX_EPISODES)
        self.alerts = deque(maxlen=MAX_ALERTS)
        self.alert_index = {}      # (signature, t_detect str) -> alert, to attach replay outcomes
        self.replays = []
        self.minutes = {}          # minute -> {"active": set(sig), "positive": set(sig)}
        self.health = {"batches": 0, "failed": 0, "sessions": set(), "t_first": None, "t_last": None,
                       "rtts": deque(maxlen=600), "recent": deque(maxlen=600)}
        self.books = {}            # ticker -> latest book, for drill-down
        self._next_alert_id = 1
        self._stats_cache = (0, None)

    # ---- ingestion ---------------------------------------------------------------------------

    def poll(self):
        """Consume everything the recorder appended since the last call. Returns batches processed."""
        n = 0
        for raw in self.tailer.poll():
            if raw.get("type") == "header":
                self.interval = raw["interval"]
                self.tracker.max_gap = self.max_gap_intervals * self.interval
                if self.replay:
                    self.replay.max_gap_s = self.max_gap_intervals * self.interval
            rec = self.assembler.feed(raw)
            if rec is None:
                continue
            with self.lock:
                self._process(rec)
            n += 1
        with self.lock:
            last = self.health["t_last"]
            if not self.caught_up and (n == 0 or (last is not None and time.time() - last < 15)):
                self.caught_up = True  # nothing left to read, or reading data from the last few seconds
            if n:
                self.version += 1
        return n

    def _process(self, rec):
        h = self.health
        h["batches"] += 1
        h["sessions"].add(rec["session"])
        h["t_first"] = rec["t_recv"] if h["t_first"] is None else h["t_first"]
        h["t_last"] = rec["t_recv"]
        h["recent"].append((rec["t_recv"], "error" in rec))
        if "error" in rec:
            h["failed"] += 1
            return
        h["rtts"].append(rec["rtt"])
        self.books.update(rec["books"])

        rows_by_event = {}
        for ev, found, present in self.detector.process(rec):
            rows = list(to_rows("snapshots", rec["t_recv"], rec["session"], rec["cycle"], ev, found,
                                self.volume_24h, present))
            rows_by_event[ev["event_ticker"]] = rows
            closed, started = self.tracker.observe(ev["event_ticker"], rec["session"], rec["t_recv"],
                                                   {r["signature"]: r for r in rows})
            for e in closed:
                e["title"] = self.title_of.get(e["event_ticker"])
                e["passes_filter"] = passes(e, self.filters)
                self.episodes.append(e)
            by_sig = {r["signature"]: r for r in rows}
            for sig in started:
                self._maybe_alert(by_sig[sig], rec)

        minute = int(rec["t_recv"] // 60) * 60
        bucket = self.minutes.setdefault(minute, {"active": set(), "positive": set()})
        for rows in rows_by_event.values():
            for r in rows:
                bucket["active"].add(r["signature"])
                if Decimal(r["best_net"]) > 0:
                    bucket["positive"].add(r["signature"])

        if self.replay:
            settled, _ = self.replay.step(rec, self.quotes_of(rec["books"]), rows_by_event)
            for res in settled:
                res = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in res.items()}
                self.replays.append(res)
                alert = self.alert_index.get((res["signature"], res["t_detect"]))
                if alert is not None:
                    alert["replay"] = {k: res[k] for k in ("outcome", "pnl", "hedged", "book_unchanged", "planned_net",
                                                           "reaction_s", "written_off")}

    def _maybe_alert(self, row, rec):
        if Decimal(row["best_net"]) <= 0:
            return
        episode_like = {"min_leg_volume_24h": _f(row["min_leg_volume_24h"]), "first_top_size": _f(row["top_size"]),
                        "first_max_leg_spread": _f(row["max_leg_spread"])}
        alert = {
            "id": self._next_alert_id, "t": rec["t_recv"], "live": rec["t_recv"] >= self.started_wall - 5,
            "event_ticker": row["event_ticker"], "title": self.title_of.get(row["event_ticker"]),
            "kind": row["kind"], "signature": row["signature"], "legs": parse_legs(row["signature"]),
            "gross_top": _f(row["gross_top"]), "best_net": _f(row["best_net"]), "best_q": _f(row["best_q"]),
            "top_size": _f(row["top_size"]), "max_leg_spread": _f(row["max_leg_spread"]),
            "min_leg_volume_24h": _f(row["min_leg_volume_24h"]), "passes_filter": passes(episode_like, self.filters),
            "requires_exhaustive": row["kind"] == "set_long", "replay": None,
        }
        self._next_alert_id += 1
        self.alerts.append(alert)
        self.alert_index[(row["signature"], f"{rec['t_recv']:.3f}")] = alert

    # ---- views ---------------------------------------------------------------------------------

    def _active(self):
        now = self.health["t_last"]
        out = []
        for ev_ticker, sig, run in self.tracker.active():
            last = run["last"]
            episode_like = {"min_leg_volume_24h": run["agg"]["min_leg_volume_24h"],
                            "first_top_size": run["agg"]["first_top_size"],
                            "first_max_leg_spread": run["agg"]["first_max_leg_spread"]}
            out.append({
                "signature": sig, "event_ticker": ev_ticker, "title": self.title_of.get(ev_ticker), "kind": run["kind"],
                "legs": parse_legs(sig), "age": (now - run["start"]) if now else 0, "start": run["start"],
                "gross_top": _f(last["gross_top"]), "best_net": _f(last["best_net"]), "net_1": _f(last["net_1"]),
                "best_q": _f(last["best_q"]), "top_size": _f(last["top_size"]), "max_leg_spread": _f(last["max_leg_spread"]),
                "min_leg_volume_24h": _f(last["min_leg_volume_24h"]), "passes_filter": passes(episode_like, self.filters),
                "left_censored": run["t_prev"] is None,
            })
        out.sort(key=lambda a: (-(a["best_net"] or -1e9), -(a["gross_top"] or 0)))
        return out

    def _all_episodes_for_stats(self):
        """Closed episodes plus open runs as right-censored (all we know is they have lasted this long)."""
        eps = list(self.episodes)
        for ev_ticker, sig, run in self.tracker.active():
            eps.append({"signature": sig, "event_ticker": ev_ticker, "kind": run["kind"],
                        **make_episode(run["session"], run["start"], run["end"], run["t_prev"], None, run["agg"])})
        return eps

    def _meta(self):
        return {"interval": self.interval, "rtts": list(self.health["rtts"]),
                "latency": self.latency["samples"] if self.latency else None}

    def _stats(self):
        # Kaplan-Meier and the catch-probability simulation are the expensive part: refresh every 10 s at most.
        if self._stats_cache[1] is not None and time.time() - self._stats_cache[0] < 10:
            return self._stats_cache[1]
        eps = self._all_episodes_for_stats()
        meta = self._meta()
        budget, how = budgets(meta)
        positive = [e for e in eps if e["max_best_net"] > 0]
        tradeable = [e for e in positive if passes(e, self.filters)]
        unc = [e for e in eps if e["hi"] is not None]
        curve = kaplan_meier(*km_inputs(eps)) if eps else []
        cap = capture_share(eps, meta, n_draws=1500) if meta["latency"] and eps else None
        grid = []
        if eps:
            positives = [e["lo"] for e in eps if e["lo"] > 0] + [e["hi"] for e in eps if e["hi"]]
            x_min = max(0.05, min([self.interval / 4] + positives))
            x_max = max(positives + [budget["worst"] * 4])
            xs = [x_min * (x_max / x_min) ** (i / 119) for i in range(120)]
            n = len(eps)
            grid = [{"t": x, "km": km_at(curve, x), "lo": sum(e["lo"] > x for e in eps) / n,
                     "hi": sum(e["hi"] is None or e["hi"] > x for e in eps) / n} for x in xs]
        by_kind = {}
        for k in ("yes_no_cross", "ladder", "set_short", "set_long"):
            ke = [e for e in eps if e["kind"] == k]
            by_kind[k] = {"episodes": len(ke), "positive": sum(e["max_best_net"] > 0 for e in ke),
                          "tradeable": sum(e["max_best_net"] > 0 and passes(e, self.filters) for e in ke)}
        stats = {
            "episodes": len(eps), "events": len({e["event_ticker"] for e in eps}),
            "positive": len(positive), "tradeable": len(tradeable), "by_kind": by_kind,
            "median_lo": quantile([e["lo"] for e in unc], 0.5), "median_hi": quantile([e["hi"] for e in unc], 0.5),
            "catch": {"definitely": cap[0], "possibly": cap[1], "km": cap[2]} if cap else None,
            "budget": budget, "budget_how": how, "survival": grid,
        }
        self._stats_cache = (time.time(), stats)
        return stats

    def _health_view(self):
        h = self.health
        now = time.time()
        recent = [t for t, _ in h["recent"] if t >= (h["t_last"] or 0) - 60]
        span = (recent[-1] - recent[0]) if len(recent) > 1 else None
        return {
            "status": ("catching up" if not self.caught_up else
                       "live" if h["t_last"] and now - h["t_last"] < 10 else
                       "stalled" if h["t_last"] else "waiting"),
            "last_poll_age": (now - h["t_last"]) if h["t_last"] else None,
            "batches": h["batches"], "failed": h["failed"], "sessions": len(h["sessions"]),
            "t_first": h["t_first"], "t_last": h["t_last"], "interval": self.interval,
            "batches_per_s": (len(recent) - 1) / span if span else None,
            "rtt_p50": quantile(list(h["rtts"]), 0.5), "rtt_p95": quantile(list(h["rtts"]), 0.95),
            "markets": sum(len(ev["markets"]) for ev in self.universe["events"]),
            "events": len(self.universe["events"]),
        }

    def _replay_view(self):
        r = self.replays
        n = len(r)
        outcomes = {}
        for x in r:
            outcomes[x["outcome"]] = outcomes.get(x["outcome"], 0) + 1
        return {"signals": n, "outcomes": outcomes,
                "full_fill_rate": outcomes.get("full", 0) / n if n else None,
                "pnl": sum(float(x["pnl"]) for x in r), "planned": sum(float(x["planned_net"]) for x in r),
                "pnl_certain": sum(float(x["pnl"]) for x in r if x["book_unchanged"]),
                "recent": r[-50:][::-1]}

    def live_view(self, since_alert=0):
        with self.lock:
            return {"version": self.version, "health": self._health_view(), "active": self._active()[:200],
                    "alerts": [a for a in self.alerts if a["id"] > since_alert][-100:],
                    "kpis": self._kpis(), "filters": self.filters}

    def _kpis(self):
        s = self._stats()
        return {"active": sum(1 for _ in self.tracker.active()), "episodes": s["episodes"], "positive": s["positive"],
                "tradeable": s["tradeable"], "median_lo": s["median_lo"], "median_hi": s["median_hi"],
                "catch_km": s["catch"]["km"] if s["catch"] else None, "budget": s["budget"],
                "replay_pnl": sum(float(x["pnl"]) for x in self.replays), "replay_signals": len(self.replays)}

    def full_view(self):
        with self.lock:
            timeline = [{"t": m, "active": len(b["active"]), "positive": len(b["positive"])}
                        for m, b in sorted(self.minutes.items())]
            latency = None
            if self.latency:
                s = self.latency["summary"]
                latency = {"measured_utc": self.latency["measured_utc"], "edge": self.latency.get("cdn_edge"),
                           "parts": {k: {"p50": s[k]["p50"], "p95": s[k]["p95"]}
                                     for k in ("get_total", "compute_batch", "post_total", "tcp_connect")}}
            return {"version": self.version, "health": self._health_view(), "stats": self._stats(),
                    "timeline": timeline, "episodes": list(self.episodes)[-1000:][::-1],
                    "alerts": list(self.alerts)[::-1], "replay": self._replay_view(), "latency": latency,
                    "filters": self.filters}

    def book_view(self, ticker):
        with self.lock:
            book = self.books.get(ticker)
            if book is None:
                return None
            best_first = lambda side: [[float(p), float(s)] for p, s in reversed(book[side])][:5]
            return {"ticker": ticker, "yes_bids": best_first("yes"), "no_bids": best_first("no")}


def run_forever(engine, period=0.5, stop=None):
    while stop is None or not stop.is_set():
        try:
            engine.poll()
        except Exception as exc:  # keep serving; surface the problem in the log
            print(f"engine error: {exc!r}", flush=True)
        time.sleep(period)
