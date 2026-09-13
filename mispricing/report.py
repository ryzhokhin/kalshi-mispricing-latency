"""One command from raw data to every number in the README. Numbers are never typed by hand.

    python -m mispricing.report --source snapshots
    python -m mispricing.report --source history --universe data/universe_history.json --series-fees data/series_fees_history.json

Pipeline: detect -> episodes -> stale-quote filter -> replay (snapshots only)
-> cluster bootstrap -> sensitivity -> charts. Writes results/<source>/:
results.md, summary.json, lifetimes_all.png, lifetimes_filtered.png. For
--source snapshots it also rewrites the block between the RESULTS markers in
README.md.
"""
import argparse
import json
import time
from decimal import Decimal
from pathlib import Path

from .detect import write_violations
from .episodes import budgets, capture_share, compute_episodes, fmt_s, plot, quantile, write_episodes
from .storage import load_universe
from .replay import run_replay, summarize_replay, write_replay
from .robustness import BASELINE, apply_filter, bootstrap_table, passes, sensitivity_table, stats

KINDS = ["yes_no_cross", "ladder", "set_short", "set_long"]


# ---- formatting ---------------------------------------------------------------------------

def pct(x):
    return "–" if x is None else f"{x:.0%}"


def money(x):
    return "–" if x is None else f"${Decimal(x):,.2f}"


def ci(pair, fmt):
    lo, hi = pair
    return "–" if lo is None else f"{fmt(lo)} – {fmt(hi)}"


def table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def utc(t):
    return "–" if t is None else time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(t))


# ---- pieces ------------------------------------------------------------------------------------

def latency_summary(path):
    if not Path(path).exists():
        return None
    lat = json.loads(Path(path).read_text())
    return {"measured_utc": lat["measured_utc"], "edge": lat.get("cdn_edge"), "summary": lat["summary"]}


def headline(s_all, s_f, meta, budget, rep, source):
    duration_h = (meta["t_last"] - meta["t_first"]) / 3600 if meta["t_first"] else 0
    poll = fmt_s(meta["interval"])
    text = (f"Over {duration_h:.1f} h of {poll} {'order-book polling' if source == 'snapshots' else 'candles'}, "
            f"I found {s_all['episodes']} no-arbitrage violation episodes before fees across {s_all['events']} events; ")
    if s_all["net_best_pos"] == 0:
        text += "none was profitable after Kalshi's fees, so there was nothing to trade. "
    else:
        text += (f"{s_all['net_best_pos']} were profitable after fees at the best available size "
                 f"({s_f['net_best_pos']} after the stale-quote filter). ")
    if s_all["median_lo"] is not None:
        text += f"Median lifetime was between {fmt_s(s_all['median_lo'])} and {fmt_s(s_all['median_hi'])}. "
    if source == "snapshots":
        text += f"Measured reaction time is ~{fmt_s(budget['typical'])} ({fmt_s(budget['worst'])} worst case)"
        if s_all["catch_km"] is not None:
            text += f"; an estimated {pct(s_all['catch_km'])} of violations outlived it"
        text += ". "
        if rep and rep["signals"]:
            full = sum(v for k, v in rep["outcomes"].items() if k.startswith("full"))
            text += (f"In replay, {full} of {rep['signals']} fee-positive signals filled both legs, "
                     f"for simulated P&L of {money(rep['simulated_pnl'])} "
                     f"({money(rep['simulated_pnl_certain_only'])} counting only fills that were certain).")
        elif rep is not None:
            text += "No fee-positive signal appeared, so the execution replay had nothing to simulate."
    else:
        text += "Candle data cannot resolve anything faster than a minute, so it says nothing about a sub-second budget."
    return text


def build(args):
    source = args.source
    out_dir = Path("results") / source
    out_dir.mkdir(parents=True, exist_ok=True)
    universe = load_universe(args.universe)

    print("1/6 detect ...", flush=True)
    violations = write_violations(source, args.universe, args.series_fees, args.snapshots, args.candles)
    print("2/6 episodes ...", flush=True)
    eps, meta = compute_episodes(source, args.universe, args.snapshots, args.candles, violations, args.latency)
    write_episodes(eps, f"data/episodes_{source}.csv")
    filtered = apply_filter(eps, BASELINE)
    budget, budget_how = budgets(meta)

    groups = {
        "all": eps,
        "stale-quote filtered": filtered,
        "positive after fees": [e for e in eps if e["max_best_net"] > 0],
        "filtered and positive after fees": [e for e in filtered if e["max_best_net"] > 0],
    }
    group_stats = {name: stats(g, meta) for name, g in groups.items()}

    rep = rep_f = None
    if source == "snapshots" and meta.get("latency"):
        print("3/6 replay ...", flush=True)
        results = run_replay(args.universe, args.series_fees, args.snapshots, violations, args.latency,
                             max_gap_s=3 * meta["interval"])
        write_replay(results, f"data/replay_{source}.csv")
        rep = summarize_replay(results)

        def as_episode(r):
            num = lambda k: float(r[k]) if r[k] not in (None, "") else None
            return {"min_leg_volume_24h": num("min_leg_volume_24h"), "first_top_size": num("top_size"),
                    "first_max_leg_spread": num("max_leg_spread")}

        rep_f = summarize_replay([r for r in results if passes(as_episode(r), BASELINE)])

    print("4/6 bootstrap ...", flush=True)
    boot_on = filtered if filtered else eps
    boot = bootstrap_table(boot_on, meta, B=args.bootstrap)
    print("5/6 sensitivity ...", flush=True)
    sens = sensitivity_table(eps, meta)
    gap_rows = []
    if source == "snapshots":
        for gap in (2.0, 3.0, 5.0):
            e_gap, m_gap = compute_episodes(source, args.universe, args.snapshots, args.candles, violations,
                                            args.latency, max_gap_intervals=gap)
            gap_rows.append((gap, stats(apply_filter(e_gap, BASELINE), m_gap)))

    print("6/6 charts and report ...", flush=True)
    charts = []
    if eps:
        plot(eps, meta, source, out_dir / "lifetimes_all.png")
        charts.append("lifetimes_all.png")
    if filtered:
        plot(filtered, meta, f"{source}, stale-quote filtered", out_dir / "lifetimes_filtered.png",
             title="How long tradeable-looking Kalshi violations survive")
        charts.append("lifetimes_filtered.png")

    lat = latency_summary(args.latency) if source == "snapshots" else None
    text = headline(group_stats["all"], group_stats["stale-quote filtered"], meta, budget, rep, source)
    md = render_markdown(source, universe, meta, eps, filtered, group_stats, budget, budget_how, lat, rep, rep_f,
                         boot, len(boot_on) == len(eps) and not filtered, sens, gap_rows, charts, text, args)
    (out_dir / "results.md").write_text(md)
    summary = {"generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "source": source,
               "headline": text, "budget": budget, "filters": BASELINE, "groups": group_stats,
               "replay": rep, "replay_filtered": rep_f,
               "data": {k: meta[k] for k in ("interval", "sessions", "batches", "failed_batches", "t_first", "t_last")}}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    if source == "snapshots":
        update_readme(text, group_stats, rep, budget, out_dir, charts)
    update_paper(source, md)
    print(f"\n{text}\n\nwrote {out_dir}/results.md")


def render_markdown(source, universe, meta, eps, filtered, group_stats, budget, budget_how, lat, rep, rep_f,
                    boot, boot_unfiltered, sens, gap_rows, charts, text, args):
    L = [f"# Results: {source}", "", f"_Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} by `report.py`._", "",
         "## Headline", "", text, ""]

    fail = meta["failed_batches"] / meta["batches"] if meta["batches"] else None
    L += ["## Data", "", table(["", ""], [
        ["source", "recorded order books (`data/snapshots`)" if source == "snapshots" else "1-minute candles"],
        ["window", f"{utc(meta['t_first'])} → {utc(meta['t_last'])}"],
        ["poll interval", fmt_s(meta["interval"])],
        ["markets / events watched", f"{sum(len(e['markets']) for e in universe['events'])} / {len(universe['events'])}"],
        ["recording sessions", meta["sessions"]],
        ["polls (batches) / failed", f"{meta['batches']:,} / {meta['failed_batches']:,} ({pct(fail)})"],
    ]), ""]

    rows = []
    for kind in KINDS:
        k_all = [e for e in eps if e["kind"] == kind]
        k_f = [e for e in filtered if e["kind"] == kind]
        rows.append([kind, len(k_all), sum(e["max_best_net"] > 0 for e in k_all), len(k_f),
                     sum(e["max_best_net"] > 0 for e in k_f)])
    rows.append(["**total**", len(eps), sum(e["max_best_net"] > 0 for e in eps), len(filtered),
                 sum(e["max_best_net"] > 0 for e in filtered)])
    L += ["## Violations before and after fees", "",
          "An episode is one continuous stretch during which a violation was visible. "
          "\"After fees\" = positive net P&L at the best size the book allowed"
          + (" (history has no depth: 1 or 100 contracts assumed)." if source == "history" else "."), "",
          table(["kind", "episodes", "positive after fees", "episodes (filtered)", "positive after fees (filtered)"], rows),
          "", f"Stale-quote filter: 24h volume ≥ {BASELINE['min_volume_24h']:g} contracts on every leg, "
              f"≥ {BASELINE['min_top_size']:g} baskets at the best prices"
              + (" (not applied to history)" if source == "history" else "")
              + f", every leg's spread ≤ {BASELINE['max_spread'] * 100:g}¢.", ""]

    L += ["## Lifetimes", "",
          f"Lifetimes are intervals, not points: a violation seen at polls t_s..t_e lived between t_e − t_s and "
          f"t_next − t_prev. Nothing shorter than the {fmt_s(meta['interval'])} poll can be resolved.", "",
          table(["group", "episodes", "events", "median lifetime", "definitely outlived typical budget",
                 "Kaplan–Meier outlived", "catch probability (KM)"],
                [[name, s["episodes"], s["events"],
                  "–" if s["median_lo"] is None else f"{fmt_s(s['median_lo'])} – {fmt_s(s['median_hi'])}",
                  pct(s["definitely_outlived_typical"]), pct(s["km_outlived_typical"]), pct(s["catch_km"])]
                 for name, s in group_stats.items()]), ""]
    L += [f"![lifetimes]({c})" for c in charts] + [""]

    L += ["## Latency budget", "", f"Budget: typical **{fmt_s(budget['typical'])}**, worst **{fmt_s(budget['worst'])}** "
          f"({budget_how}).", ""]
    if lat:
        s = lat["summary"]
        L += [table(["component", "p50", "p95"], [
            ["detection lag (Uniform over the poll interval)", fmt_s(meta["interval"] / 2), fmt_s(meta["interval"])],
            ["fetch order books (warm GET, ~100 tickers)", fmt_s(s["get_total"]["p50"]), fmt_s(s["get_total"]["p95"])],
            ["compute (quotes + detectors, one batch)", fmt_s(s["compute_batch"]["p50"]), fmt_s(s["compute_batch"]["p95"])],
            ["order round trip (unauthenticated POST, rejected 401 — a lower bound)",
             fmt_s(s["post_total"]["p50"]), fmt_s(s["post_total"]["p95"])],
            ["TCP connect (RTT to the CDN edge, not the exchange)", fmt_s(s["tcp_connect"]["p50"]), fmt_s(s["tcp_connect"]["p95"])],
        ]), "", f"Measured {lat['measured_utc']} via CDN edge {lat['edge']}. No order was ever placed.", ""]

    if rep is not None:
        L += ["## Execution replay", "",
              "Each new fee-positive violation becomes IOC limit buys on every leg, landing after a reaction time "
              "resampled from the latency measurements, and fills against the next recorded book. "
              "Unchanged books make a fill certain; changed books are evaluated pessimistically. "
              "Size is capped at 500 baskets per signal. "
              "Queue priority and competing traders are not modeled, which makes this optimistic.", ""]
        if rep["signals"]:
            L += [table(["", "all signals", "stale-quote filtered"], [
                ["signals", rep["signals"], rep_f["signals"]],
                ["full fill rate (both legs, full size)", pct(rep["full_fill_rate"]), pct(rep_f["full_fill_rate"])],
                ["leg-risk incidents (one leg short)", rep["leg_risk_incidents"], rep_f["leg_risk_incidents"]],
                ["P&L the detector expected", money(rep["planned_pnl"]), money(rep_f["planned_pnl"])],
                ["simulated P&L", money(rep["simulated_pnl"]), money(rep_f["simulated_pnl"])],
                ["simulated P&L, certain fills only", money(rep["simulated_pnl_certain_only"]),
                 money(rep_f["simulated_pnl_certain_only"])],
                ["contracts written off (no bid to unwind)", rep["written_off_contracts"], rep_f["written_off_contracts"]],
            ]), "", table(["outcome", "count"], sorted(rep["outcomes"].items())), ""]
        else:
            L += ["No fee-positive violation appeared, so there was nothing to replay.", ""]

    L += ["## Robustness", "", "### Error bars: episodes are not independent", "",
          ("95% bootstrap intervals on the " + ("unfiltered" if boot_unfiltered else "stale-quote filtered")
           + " episodes. *Naive* resamples episodes; *cluster* resamples whole events. "
             "When the cluster interval is much wider, the naive one was overconfident."
           + (f" **Only {len({e['event_ticker'] for e in (filtered or eps)})} events: with fewer than ~10 clusters "
              "the bootstrap itself is unreliable (it can even come out narrower than the naive interval); "
              "treat every interval here as indicative.**"
              if len({e['event_ticker'] for e in (filtered or eps)}) < 10 else "")), "",
          table(["statistic", "point", "naive 95% CI", "cluster 95% CI (by event)"],
                [[r["statistic"],
                  "–" if r["point"] is None else (fmt_s(r["point"]) if "(s)" in r["statistic"] else pct(r["point"])),
                  ci(r["naive_ci"], fmt_s if "(s)" in r["statistic"] else pct),
                  ci(r["cluster_ci"], fmt_s if "(s)" in r["statistic"] else pct)] for r in boot]), ""]
    L += ["### Sensitivity to the filter thresholds", "", "One threshold moves, the others stay at baseline (bold).", "",
          table(["threshold", "value", "episodes", "events", "positive after fees", "median lifetime", "catch probability (KM)"],
                [[r["param"], f"**{r['value']:g}**" if r["baseline"] else f"{r['value']:g}", r["episodes"], r["events"],
                  r["net_best_pos"],
                  "–" if r["median_lo"] is None else f"{fmt_s(r['median_lo'])} – {fmt_s(r['median_hi'])}",
                  pct(r["catch_km"])] for r in sens]), ""]
    if gap_rows:
        L += ["### Sensitivity to the episode gap rule (filtered)", "",
              table(["max gap (poll intervals)", "episodes", "median lifetime", "catch probability (KM)"],
                    [[f"**{g:g}**" if g == 3.0 else f"{g:g}", s["episodes"],
                      "–" if s["median_lo"] is None else f"{fmt_s(s['median_lo'])} – {fmt_s(s['median_hi'])}",
                      pct(s["catch_km"])] for g, s in gap_rows]), ""]
    L += ["## Reproduce", "", "```", f"python -m mispricing.report --source {source}"
          + (f" --universe {args.universe} --series-fees {args.series_fees}" if source == "history" else ""), "```", ""]
    return "\n".join(L)


PAPER_SECTIONS = ["Headline", "Data", "Violations before and after fees", "Lifetimes", "Latency budget",
                  "Execution replay", "Robustness"]


def update_paper(source, md, paper_path="paper/paper.md"):
    """Copy the generated sections into the paper, between RESULTS:<SOURCE> markers, headings demoted two levels."""
    paper = Path(paper_path)
    start, end = f"<!-- RESULTS:{source.upper()}:START -->", f"<!-- RESULTS:{source.upper()}:END -->"
    if not paper.exists() or start not in paper.read_text():
        return
    keep, current = [], None
    for line in md.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
        if current in PAPER_SECTIONS and not line.startswith("# Results"):
            if current == "Robustness" and line.startswith("### Sensitivity"):
                current = None
                continue
            line = ("###" + line[1:]) if line.startswith("##") else line
            line = line.replace("![lifetimes](", f"![lifetimes](../results/{source}/")
            keep.append(line)
    content = paper.read_text()
    head, rest = content.split(start, 1)
    _, tail = rest.split(end, 1)
    note = f"_Generated by `python -m mispricing.report --source {source}`; full tables in [results/{source}/results.md](../results/{source}/results.md)._"
    paper.write_text(head + start + "\n" + note + "\n\n" + "\n".join(keep).strip() + "\n" + end + tail)


def update_readme(text, group_stats, rep, budget, out_dir, charts):
    readme = Path("README.md")
    if not readme.exists():
        return
    s_all, s_f = group_stats["all"], group_stats["stale-quote filtered"]
    block = [text, "",
             table(["", "all", "stale-quote filtered"], [
                 ["violation episodes (before fees)", s_all["episodes"], s_f["episodes"]],
                 ["positive after fees", s_all["net_best_pos"], s_f["net_best_pos"]],
                 ["median lifetime", "–" if s_all["median_lo"] is None else f"{fmt_s(s_all['median_lo'])} – {fmt_s(s_all['median_hi'])}",
                  "–" if s_f["median_lo"] is None else f"{fmt_s(s_f['median_lo'])} – {fmt_s(s_f['median_hi'])}"],
                 ["reaction time (typical / worst)", f"{fmt_s(budget['typical'])} / {fmt_s(budget['worst'])}", ""],
                 ["catch probability (Kaplan–Meier)", pct(s_all["catch_km"]), pct(s_f["catch_km"])],
                 ["replay: signals / full fills / simulated P&L",
                  "–" if not rep else f"{rep['signals']} / {pct(rep['full_fill_rate'])} / {money(rep['simulated_pnl'])}", ""],
             ]), ""]
    if charts:
        block += [f"![How long violations survive]({out_dir.as_posix()}/{charts[0]})", ""]
    block += [f"Full tables, error bars and sensitivity: [{out_dir.as_posix()}/results.md]({out_dir.as_posix()}/results.md)"]
    content = readme.read_text()
    start, end = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
    head, rest = content.split(start, 1)
    _, tail = rest.split(end, 1)
    readme.write_text(head + start + "\n" + "\n".join(block) + "\n" + end + tail)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["snapshots", "history"], required=True)
    ap.add_argument("--universe", default="data/universe.json")
    ap.add_argument("--series-fees", default="data/series_fees.json")
    ap.add_argument("--snapshots", default="data/snapshots")
    ap.add_argument("--candles", default="data/history/candles_1m.csv")
    ap.add_argument("--latency", default="data/latency.json")
    ap.add_argument("--bootstrap", type=int, default=1000)
    build(ap.parse_args())


if __name__ == "__main__":
    main()
