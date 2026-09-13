"""Does the headline survive realistic filtering and clustered error bars?

1. Stale-quote filter. A violation between quotes nobody is trading is not an
   opportunity. An episode passes if, when first seen:
     - every leg's market traded >= min_volume_24h contracts in the 24h before selection
       (24h volume is from universe selection time; it goes stale over a long recording)
     - top_size >= min_top_size baskets available at the best price of every leg
       (unknown for candle history: not applied there)
     - every leg's bid-ask spread <= max_spread (an empty side counts as infinitely wide)

2. Sensitivity. Move one threshold at a time and watch the headline. A result
   that flips when a threshold moves slightly is a result about the threshold.

3. Cluster bootstrap. Episodes on the same event share the same traders,
   quotes and news, so they are not independent. Resampling episodes treats
   50 episodes on one ladder as 50 observations and gives too-narrow intervals.
   Instead resample whole EVENTS with replacement, recompute the statistic,
   and take the 2.5th / 97.5th percentiles. With few events the interval is
   wide, which is the correct conclusion rather than a defect.
"""
import random
from collections import defaultdict

from .episodes import budgets, capture_share, kaplan_meier, km_at, km_inputs, quantile

BASELINE = {"min_volume_24h": 100.0, "min_top_size": 5.0, "max_spread": 0.10}
GRID = {
    "min_volume_24h": [0.0, 10.0, 100.0, 1000.0, 10000.0],
    "min_top_size": [0.0, 1.0, 5.0, 25.0, 100.0],
    "max_spread": [1.0, 0.20, 0.10, 0.05, 0.02],
}


def passes(e, f):
    if (e["min_leg_volume_24h"] or 0) < f["min_volume_24h"]:
        return False
    if e.get("first_top_size") is not None and e["first_top_size"] < f["min_top_size"]:
        return False
    spread = e.get("first_max_leg_spread")
    if f["max_spread"] < 1.0 and (spread is None or spread > f["max_spread"]):
        return False
    return True


def apply_filter(eps, f):
    return [e for e in eps if passes(e, f)]


# ---- statistics used in tables and the bootstrap ---------------------------------------------

def stats(eps, meta):
    """Headline numbers for a set of episodes. None where undefined."""
    budget, _ = budgets(meta)
    unc = [e for e in eps if e["hi"] is not None]
    n = len(eps)
    curve = kaplan_meier(*km_inputs(eps)) if eps else []
    cap = capture_share(eps, meta, n_draws=1000) if meta.get("latency") else None
    return {
        "episodes": n,
        "events": len({e["event_ticker"] for e in eps}),
        "net_best_pos": sum(e["max_best_net"] > 0 for e in eps),
        "net_1_pos": sum((e["max_net_1"] or 0) > 0 for e in eps),
        "share_net_best_pos": sum(e["max_best_net"] > 0 for e in eps) / n if n else None,
        "median_lo": quantile([e["lo"] for e in unc], 0.5),
        "median_hi": quantile([e["hi"] for e in unc], 0.5),
        "definitely_outlived_typical": sum(e["lo"] > budget["typical"] for e in eps) / n if n else None,
        "km_outlived_typical": km_at(curve, budget["typical"]) if eps else None,
        "catch_km": cap[2] if cap else None,
    }


def bootstrap(eps, statistic, clusters=True, B=1000, seed=3):
    """95% percentile interval for statistic(sample). clusters=True resamples events, False resamples episodes."""
    if not eps:
        return None, None
    rng = random.Random(seed)
    if clusters:
        groups = defaultdict(list)
        for e in eps:
            groups[e["event_ticker"]].append(e)
        units = list(groups.values())
    else:
        units = [[e] for e in eps]
    values = []
    for _ in range(B):
        sample = [e for unit in (rng.choice(units) for _ in units) for e in unit]
        v = statistic(sample)
        if v is not None:
            values.append(v)
    return (quantile(values, 0.025), quantile(values, 0.975)) if values else (None, None)


def bootstrap_table(eps, meta, B=1000):
    budget, _ = budgets(meta)
    unc_median = lambda which: (lambda s: quantile([e[which] for e in s if e["hi"] is not None], 0.5))
    stats_to_test = {
        "share positive after fees (best size)": lambda s: sum(e["max_best_net"] > 0 for e in s) / len(s),
        "median lifetime, lower bound (s)": unc_median("lo"),
        "median lifetime, upper bound (s)": unc_median("hi"),
        "share definitely outliving typical budget": lambda s: sum(e["lo"] > budget["typical"] for e in s) / len(s),
    }
    rows = []
    for name, fn in stats_to_test.items():
        point = fn(eps) if eps else None
        rows.append({"statistic": name, "point": point,
                     "naive_ci": bootstrap(eps, fn, clusters=False, B=B),
                     "cluster_ci": bootstrap(eps, fn, clusters=True, B=B)})
    return rows


def sensitivity_table(eps, meta):
    """One threshold at a time around BASELINE."""
    rows = []
    for param, values in GRID.items():
        for v in values:
            f = {**BASELINE, param: v}
            s = stats(apply_filter(eps, f), meta)
            rows.append({"param": param, "value": v, "baseline": v == BASELINE[param], **s})
    return rows
