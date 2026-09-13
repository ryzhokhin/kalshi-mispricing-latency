"""Turn per-snapshot violation rows (detect.py) into episodes and lifetimes.

An episode is a maximal run of consecutive observations of an event in which
the same violation (same signature) is present. Seen first at t_s, last at t_e:

    true start in (t_prev, t_s]        true end in [t_e, t_next)
    lifetime L in [lo, hi] = [t_e - t_s,  t_next - t_prev]

Seen in a single 1 s snapshot: lo = 0, hi ~ 2 s. Nothing shorter than the poll
interval can be resolved, so both bounds are always reported -- never a point.

Censoring: if an episode touches the first or last observation of a recording
session, or a gap in observations longer than --max-gap (failed polls), there
is no t_prev or t_next on that side. Then hi is unknown and we only know L >= lo.
Quantiles use uncensored episodes only; Kaplan-Meier uses all of them.

Latency budget: a violation can only be caught if it outlives
    detection lag (Uniform(0, poll interval): mean interval/2, worst interval)
  + round trip of the order.
The recording's batch RTT stands in for the order round trip until it is
measured directly.

    python -m mispricing.episodes --source snapshots
    python -m mispricing.episodes --source history
"""
import argparse
import csv
import json
import math
import random
from collections import defaultdict
from pathlib import Path

from .storage import iter_observations, load_universe


# ---- observation timelines (plumbing) -------------------------------------------
# timeline[event_ticker] = [(session, key, t), ...] in time order: every moment the
# event was actually observed. `key` matches detect.py rows: cycle for snapshots,
# the candle minute for history.

def snapshot_timeline(universe, snapshot_dir):
    event_of = {m["ticker"]: ev["event_ticker"] for ev in universe["events"] for m in ev["markets"]}
    timeline, rtts, intervals = defaultdict(list), [], set()
    sessions, errors, batches, t_first, t_last = set(), 0, 0, None, None
    for rec in iter_observations(snapshot_dir):
        if rec["type"] == "header":
            intervals.add(rec["interval"])
            continue
        batches += 1
        sessions.add(rec["session"])
        t_first = rec["t_recv"] if t_first is None else t_first
        t_last = rec["t_recv"]
        if "error" in rec:
            errors += 1
            continue
        rtts.append(rec["rtt"])
        for ev in {event_of[t] for t in rec["tickers"]}:
            timeline[ev].append((rec["session"], rec["cycle"], rec["t_recv"]))
    return timeline, {"interval": max(intervals), "rtts": rtts, "sessions": len(sessions), "batches": batches,
                      "failed_batches": errors, "t_first": t_first, "t_last": t_last}


def history_timeline(universe, candles_path):
    """Every candle minute is an observation of every event whose markets have started
    reporting: a minute without a candle means "unchanged", not "unobserved"."""
    event_of = {m["ticker"]: ev["event_ticker"] for ev in universe["events"] for m in ev["markets"]}
    minutes, first_seen = set(), {}
    with open(candles_path) as f:
        for row in csv.DictReader(f):
            m, ev = int(row["end_period_ts"]), event_of[row["ticker"]]
            minutes.add(m)
            first_seen[ev] = min(first_seen.get(ev, m), m)
    minutes = sorted(minutes)
    timeline = {ev: [("", m, m) for m in minutes if m >= start] for ev, start in first_seen.items()}
    return timeline, {"interval": 60.0, "rtts": [], "sessions": 1, "batches": len(minutes), "failed_batches": 0,
                      "t_first": minutes[0] if minutes else None, "t_last": minutes[-1] if minutes else None}


def load_rows(path, source):
    """signature -> {(session, key): row}"""
    present = defaultdict(dict)
    with open(path) as f:
        for r in csv.DictReader(f):
            key = (int(r["session"]), int(r["cycle"])) if source == "snapshots" else ("", int(float(r["t"])))
            present[r["signature"]][key] = r
    return present


# ---- episodes -----------------------------------------------------------------------

def _num(row, key):
    return float(row[key]) if row.get(key) not in (None, "") else None


def aggregate_start(row):
    """Economics of an episode, updated as it continues. First-seen fields are what a filter could know at detection."""
    return {"n_obs": 1, "max_gross": _num(row, "gross_top"), "max_net_1": _num(row, "net_1"),
            "max_best_net": _num(row, "best_net"), "min_leg_volume_24h": _num(row, "min_leg_volume_24h"),
            "first_top_size": _num(row, "top_size"), "first_max_leg_spread": _num(row, "max_leg_spread"),
            "first_best_net": _num(row, "best_net")}


def aggregate_add(agg, row):
    agg["n_obs"] += 1
    agg["max_gross"] = max(agg["max_gross"], _num(row, "gross_top"))
    net1 = _num(row, "net_1")
    if net1 is not None:
        agg["max_net_1"] = net1 if agg["max_net_1"] is None else max(agg["max_net_1"], net1)
    agg["max_best_net"] = max(agg["max_best_net"], _num(row, "best_net"))
    agg["min_leg_volume_24h"] = min(agg["min_leg_volume_24h"], _num(row, "min_leg_volume_24h"))


def make_episode(session, start, end, t_prev, t_next, agg):
    return {"session": session, "start": start, "end": end, "lo": end - start,
            "hi": None if t_prev is None or t_next is None else t_next - t_prev,
            "left_censored": t_prev is None, "right_censored": t_next is None, **agg}


def build_episodes(obs, present, max_gap=None):
    """obs: [(session, key, t)] for one event, time-ordered.
    present: {(session, key): row} where this violation was detected.
    Returns a list of episode dicts."""
    episodes = []
    run = None  # [start index, t_prev, aggregate]

    for i, (session, key, t) in enumerate(obs):
        segment_break = i == 0 or obs[i - 1][0] != session or (max_gap is not None and t - obs[i - 1][2] > max_gap)
        if segment_break and run is not None:
            # recording stopped or polls failed: we never saw it end
            episodes.append(make_episode(obs[run[0]][0], obs[run[0]][2], obs[i - 1][2], run[1], None, run[2]))
            run = None
        row = present.get((session, key))
        if row is not None:
            if run is None:
                # present at a segment start: we never saw it begin
                run = [i, None if segment_break else obs[i - 1][2], aggregate_start(row)]
            else:
                aggregate_add(run[2], row)
        elif run is not None:
            episodes.append(make_episode(obs[run[0]][0], obs[run[0]][2], obs[i - 1][2], run[1], t, run[2]))
            run = None
    if run is not None:
        episodes.append(make_episode(obs[run[0]][0], obs[run[0]][2], obs[-1][2], run[1], None, run[2]))
    return episodes


class EpisodeTracker:
    """Streaming build_episodes across all events: feed each observation of an event as it happens.

    observe() returns the episodes that closed at this observation and the
    signatures that started. active() lists the runs still open.
    """

    def __init__(self, max_gap=None):
        self.max_gap = max_gap
        self.prev = {}   # event -> (session, t) of its previous observation
        self.runs = {}   # event -> {signature: run}

    def observe(self, event_ticker, session, t, rows_by_signature):
        closed, started = [], []
        prev = self.prev.get(event_ticker)
        segment_break = prev is None or prev[0] != session or (self.max_gap is not None and t - prev[1] > self.max_gap)
        runs = self.runs.setdefault(event_ticker, {})
        for sig in list(runs):
            if segment_break or sig not in rows_by_signature:
                run = runs.pop(sig)
                t_next = None if segment_break else t
                closed.append({"signature": sig, "event_ticker": event_ticker, "kind": run["kind"],
                               **make_episode(run["session"], run["start"], run["end"], run["t_prev"], t_next, run["agg"])})
        for sig, row in rows_by_signature.items():
            run = runs.get(sig)
            if run is None:
                runs[sig] = {"session": session, "start": t, "end": t, "kind": row["kind"],
                             "t_prev": None if segment_break else prev[1], "agg": aggregate_start(row), "last": row}
                started.append(sig)
            else:
                run["end"], run["last"] = t, row
                aggregate_add(run["agg"], row)
        self.prev[event_ticker] = (session, t)
        return closed, started

    def active(self):
        for event_ticker, runs in self.runs.items():
            for sig, run in runs.items():
                yield event_ticker, sig, run


# ---- statistics ---------------------------------------------------------------------

def quantile(xs, q):
    """Linear interpolation between order statistics (numpy's default)."""
    xs = sorted(xs)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def kaplan_meier(durations, observed):
    """Survival curve S(t) = P(L > t) with right-censoring.
    S(t) = prod over event times t_i <= t of (1 - d_i / n_i)
      d_i = episodes that ended at t_i, n_i = episodes with duration >= t_i (still at risk).
    Returns [(t_i, S just after t_i)]."""
    pairs = sorted(zip(durations, observed))
    curve, s, n = [], 1.0, len(pairs)
    i = 0
    while i < len(pairs):
        t = pairs[i][0]
        d = c = 0
        while i < len(pairs) and pairs[i][0] == t:
            d += pairs[i][1]
            c += 1
            i += 1
        if d:
            s *= 1 - d / n
            curve.append((t, s))
        n -= c
    return curve


def km_at(curve, t):
    s = 1.0
    for ti, si in curve:
        if ti > t:
            break
        s = si
    return s


def wilson(k, n, z=1.96):
    """95% interval for a proportion k/n; stays inside [0, 1] even for k = 0 or small n."""
    if n == 0:
        return None, None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def km_inputs(eps):
    """Uncensored: duration = midpoint of [lo, hi], observed. Censored: duration = lo, not observed."""
    durations = [(e["lo"] + e["hi"]) / 2 if e["hi"] is not None else e["lo"] for e in eps]
    observed = [e["hi"] is not None for e in eps]
    return durations, observed


def outlived(eps, T):
    """Share of episodes with L > T: definitely (lo > T), possibly (hi > T or unknown), and KM."""
    n = len(eps)
    if n == 0:
        return None
    definitely = sum(e["lo"] > T for e in eps)
    possibly = sum(e["hi"] is None or e["hi"] > T for e in eps)
    return {"n": n, "definitely": definitely / n, "possibly": possibly / n,
            "definitely_ci": wilson(definitely, n), "km": km_at(kaplan_meier(*km_inputs(eps)), T)}


LATENCY_PARTS = ["get_total", "compute_batch", "post_total"]  # fetch the book, run detectors, send the order


def budgets(meta):
    """typical = D/2 + p50 of each measured part; worst = D + p95 of each part.
    D (poll interval) comes from the recording. The parts come from latency.py when
    it has been run; otherwise the recording's batch RTT stands in for fetch + order."""
    interval, lat = meta["interval"], meta.get("latency")
    if lat:
        parts = {k: lat[k] for k in LATENCY_PARTS}
        typical = interval / 2 + sum(quantile(v, 0.5) for v in parts.values())
        worst = interval + sum(quantile(v, 0.95) for v in parts.values())
        how = "interval/2 + p50(fetch + compute + order); worst = interval + p95 of each (latency.py)"
    else:
        rtts = meta["rtts"]
        typical = interval / 2 + (quantile(rtts, 0.5) or 0.0)
        worst = interval + (quantile(rtts, 0.95) or 0.0)
        how = "interval/2 + batch RTT p50; worst = interval + RTT p95 (proxy: run latency.py to measure)"
    return {"typical": typical, "worst": worst}, how


def capture_share(eps, meta, n_draws=4000, seed=7):
    """P(violation outlives our reaction) with the reaction time drawn from its whole distribution,
    not a single budget number:  E[ S(D + fetch + compute + order) ],  D ~ Uniform(0, interval),
    each part resampled from latency.py's measurements. Returns (definitely, possibly, KM)."""
    lat = meta.get("latency")
    if not lat or not eps:
        return None
    rng = random.Random(seed)
    draws = [rng.uniform(0, meta["interval"]) + sum(rng.choice(lat[k]) for k in LATENCY_PARTS)
             for _ in range(n_draws)]
    curve = kaplan_meier(*km_inputs(eps))
    n = len(eps)
    definitely = sum(sum(e["lo"] > T for e in eps) / n for T in draws) / n_draws
    possibly = sum(sum(e["hi"] is None or e["hi"] > T for e in eps) / n for T in draws) / n_draws
    km = sum(km_at(curve, T) for T in draws) / n_draws
    return definitely, possibly, km


# ---- report --------------------------------------------------------------------------

def fmt_s(x):
    return "-" if x is None else (f"{x * 1000:.0f}ms" if x < 1 else f"{x:.1f}s" if x < 120 else f"{x / 60:.1f}m")


def summarize(eps, meta, source):
    budget, how = budgets(meta)
    print(f"\nsource={source}  poll interval {fmt_s(meta['interval'])}")
    print(f"latency budget: typical {fmt_s(budget['typical'])}, worst {fmt_s(budget['worst'])}  [{how}]")
    groups = [(k, [e for e in eps if e["kind"] == k]) for k in ["yes_no_cross", "ladder", "set_short", "set_long"]]
    groups += [("ALL before fees", eps),
               ("ALL net@1 > 0", [e for e in eps if (e["max_net_1"] or 0) > 0]),
               ("ALL net@best > 0", [e for e in eps if e["max_best_net"] > 0])]
    print(f"\n  {'group':17s} {'episodes':>8s} {'censored':>8s} | {'median L (lo-hi)':>17s} {'p90 L (lo-hi)':>17s} | "
          f"outlived typical budget: {'definitely':>10s} {'possibly':>8s} {'KM':>5s}")
    for name, g in groups:
        unc = [e for e in g if e["hi"] is not None]
        med = f"{fmt_s(quantile([e['lo'] for e in unc], .5))}-{fmt_s(quantile([e['hi'] for e in unc], .5))}" if unc else "-"
        p90 = f"{fmt_s(quantile([e['lo'] for e in unc], .9))}-{fmt_s(quantile([e['hi'] for e in unc], .9))}" if unc else "-"
        o = outlived(g, budget["typical"])
        share = (f"{o['definitely']:>10.0%} {o['possibly']:>8.0%} {o['km']:>5.0%}" if o else f"{'-':>10s} {'-':>8s} {'-':>5s}")
        print(f"  {name:17s} {len(g):8d} {len(g) - len(unc):8d} | {med:>17s} {p90:>17s} | {'':24s} {share}")

    if eps:
        o = outlived(eps, budget["typical"])
        lo_ci, hi_ci = o["definitely_ci"]
        unc = [e for e in eps if e["hi"] is not None]
        print(f"\nheadline: {len(eps)} violation episodes before fees, "
              f"{sum(e['max_best_net'] > 0 for e in eps)} ever positive after fees; "
              f"median lifetime between {fmt_s(quantile([e['lo'] for e in unc], .5))} and "
              f"{fmt_s(quantile([e['hi'] for e in unc], .5))}; "
              f"{o['definitely']:.0%} (95% CI {lo_ci:.0%}-{hi_ci:.0%}) definitely outlived a "
              f"{fmt_s(budget['typical'])} budget.")
        print("caveat: episodes on the same market are not independent draws, so the CI is optimistic.")
        cap = capture_share(eps, meta)
        if cap:
            print(f"catch probability over the full measured latency distribution: "
                  f"definitely {cap[0]:.0%}, possibly {cap[1]:.0%}, Kaplan-Meier {cap[2]:.0%}")
            net = [e for e in eps if e["max_best_net"] > 0]
            cap_net = capture_share(net, meta)
            if cap_net:
                print(f"  ...for the {len(net)} episodes positive after fees: definitely {cap_net[0]:.0%}, "
                      f"possibly {cap_net[1]:.0%}, Kaplan-Meier {cap_net[2]:.0%}")


def plot(eps, meta, source, out_png, title="How long Kalshi no-arbitrage violations survive"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Reference palette (dataviz skill, light mode): surface, ink, categorical slots 1-2.
    SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e7e6e2"
    SERIES = ["#2a78d6", "#eb6834"]

    budget, _ = budgets(meta)
    positives = [e["lo"] for e in eps if e["lo"] > 0] + [e["hi"] for e in eps if e["hi"]]
    x_min = min([meta["interval"] / 4] + positives)
    x_max = max(positives + [budget["worst"] * 4])
    xs = [x_min * (x_max / x_min) ** (i / 399) for i in range(400)]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=200, facecolor=SURFACE)
    ax.set_facecolor(SURFACE)
    series = [("all violations, before fees", eps)]
    net = [e for e in eps if e["max_best_net"] > 0]
    if len(net) >= 5:
        series.append(("positive after fees", net))
    for (label, g), color in zip(series, SERIES):
        n = len(g)
        s_lo = [sum(e["lo"] > x for e in g) / n for x in xs]
        s_hi = [sum(e["hi"] is None or e["hi"] > x for e in g) / n for x in xs]
        curve = kaplan_meier(*km_inputs(g))
        ax.fill_between(xs, s_lo, s_hi, step="post", color=color, alpha=0.18, linewidth=0,
                        label=f"{label}: lifetime bounds (n={n})")
        ax.step(xs, [km_at(curve, x) for x in xs], where="post", color=color, linewidth=2,
                label=f"{label}: Kaplan-Meier")
    # Budgets go in the legend, not as text on the plot, so no label ever covers a curve.
    for (name, T), dash in zip(budget.items(), ((0, (4, 3)), (0, (1, 2)))):
        ax.axvline(T, color=INK2, linewidth=1, linestyle=dash, label=f"{name} reaction-time budget ({fmt_s(T)})")

    ax.set_xscale("log")
    ax.set_ylim(0, 1.0)
    ax.set_xlim(x_min, x_max)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    nice = [0.1, 0.3, 1, 3, 10, 30, 60, 300, 600, 1800, 3600, 3 * 3600, 10 * 3600, 86400]
    nice_labels = ["100ms", "300ms", "1s", "3s", "10s", "30s", "1m", "5m", "10m", "30m", "1h", "3h", "10h", "1d"]
    ticks = [(t, lab) for t, lab in zip(nice, nice_labels) if x_min <= t <= x_max]
    ax.set_xticks([t for t, _ in ticks], [lab for _, lab in ticks])
    ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    ax.set_xlabel("lifetime t", color=INK2, fontsize=8)
    ax.set_ylabel("share of episodes lasting longer than t", color=INK2, fontsize=8)
    ax.grid(True, which="major", color=GRID, linewidth=0.6)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=7)
    fig.suptitle(title, x=0.02, ha="left", color=INK, fontsize=11)
    ax.set_title(f"{source}: poll interval {fmt_s(meta['interval'])}; lifetimes below it cannot be resolved",
                 loc="left", color=INK2, fontsize=8, pad=14)
    ax.legend(frameon=False, fontsize=7, labelcolor=INK2, loc="upper right")
    fig.tight_layout()
    fig.savefig(out_png, facecolor=SURFACE)
    print(f"wrote {out_png}")


# ---- main ------------------------------------------------------------------------------

EPISODE_COLUMNS = ["signature", "kind", "event_ticker", "session", "start", "end", "n_obs", "lo", "hi",
                   "left_censored", "right_censored", "max_gross", "max_net_1", "max_best_net", "min_leg_volume_24h",
                   "first_top_size", "first_max_leg_spread", "first_best_net"]


def compute_episodes(source, universe_path, snapshot_dir, candles_path, violations_path, latency_path,
                     max_gap_intervals=3.0):
    """Returns (episodes, meta). meta: interval, rtts, latency samples (if measured), max_gap."""
    universe = load_universe(universe_path)
    if source == "snapshots":
        timeline, meta = snapshot_timeline(universe, snapshot_dir)
        meta["max_gap"] = max_gap_intervals * meta["interval"]
    else:
        timeline, meta = history_timeline(universe, candles_path)
        meta["max_gap"] = None  # candle minutes are continuous by construction
    if latency_path and Path(latency_path).exists():
        meta["latency"] = json.loads(Path(latency_path).read_text())["samples"]
    eps = []
    for signature, rows in load_rows(violations_path, source).items():
        first = next(iter(rows.values()))
        for e in build_episodes(timeline.get(first["event_ticker"], []), rows, meta["max_gap"]):
            eps.append({"signature": signature, "kind": first["kind"], "event_ticker": first["event_ticker"], **e})
    return eps, meta


def write_episodes(eps, path):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EPISODE_COLUMNS)
        writer.writeheader()
        writer.writerows({k: ("" if e[k] is None else e[k]) for k in EPISODE_COLUMNS} for e in eps)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["snapshots", "history"], required=True)
    ap.add_argument("--universe", default="data/universe.json")
    ap.add_argument("--snapshots", default="data/snapshots")
    ap.add_argument("--candles", default="data/history/candles_1m.csv")
    ap.add_argument("--violations", default=None, help="default data/violations_<source>.csv")
    ap.add_argument("--latency", default="data/latency.json", help="output of latency.py, if it has been run")
    ap.add_argument("--max-gap-intervals", type=float, default=3.0,
                    help="a gap longer than this many poll intervals breaks an episode (snapshots only)")
    args = ap.parse_args()

    eps, meta = compute_episodes(args.source, args.universe, args.snapshots, args.candles,
                                 args.violations or f"data/violations_{args.source}.csv", args.latency,
                                 args.max_gap_intervals)
    out_csv = f"data/episodes_{args.source}.csv"
    write_episodes(eps, out_csv)
    print(f"wrote {len(eps)} episodes to {out_csv}")
    summarize(eps, meta, args.source)
    if eps:
        plot(eps, meta, args.source, f"data/lifetimes_{args.source}.png")


if __name__ == "__main__":
    main()
