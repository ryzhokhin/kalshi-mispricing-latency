"""The live dashboard must compute exactly what the offline report computes."""
import gzip
import json
import random

from mispricing.episodes import EpisodeTracker, build_episodes, make_episode
from mispricing.storage import SnapshotTailer, iter_snapshots

ROW = {"kind": "ladder", "gross_top": "0.02", "net_1": "-0.01", "best_net": "-0.01", "min_leg_volume_24h": "10",
       "top_size": "5", "max_leg_spread": "0.03"}


def recording(n_batches=30):
    """Recorder-shaped lines: one header, then batches that change one book at a time."""
    lines = [{"type": "header", "session": 1, "batches": [["A", "B"]], "interval": 1.0, "depth": 10}]
    for i in range(n_batches):
        books = {"A": {"yes": [["0.40", str(i + 1)]], "no": [["0.50", "3"]]}} if i % 3 == 0 else {}
        if i == 0:
            books["B"] = {"yes": [["0.10", "1"]], "no": [["0.80", "2"]]}
        lines.append({"type": "batch", "session": 1, "cycle": i, "batch": 0, "t_send": i, "t_recv": i + 0.1,
                      "rtt": 0.1, "books": books})
    return [json.dumps(x) + "\n" for x in lines]


def test_tailer_matches_a_full_read_across_partial_writes_and_rotation(tmp_path):
    lines = recording()
    path = tmp_path / "20260913_05_1.jsonl"
    tailer, seen = SnapshotTailer(tmp_path), []

    with path.open("w") as f:                  # first 10 lines, then half of the 11th
        f.writelines(lines[:10])
        f.write(lines[10][:15])
    seen += tailer.poll()
    assert len(seen) == 10                     # the half-written line is not consumed

    with path.open("a") as f:                  # finish the 11th line and add up to 20
        f.write(lines[10][15:])
        f.writelines(lines[11:20])
    seen += tailer.poll()
    assert len(seen) == 20

    with path.open("a") as f:                  # the hour ends: rest of the lines, then gzip
        f.writelines(lines[20:])
    with path.open("rb") as src, gzip.open(tmp_path / (path.name + ".gz"), "wb") as dst:
        dst.write(src.read())
    path.unlink()
    seen += tailer.poll()
    seen += tailer.poll()                      # nothing new, nothing repeated

    assert [json.dumps(x) + "\n" for x in seen] == lines
    assert len(list(iter_snapshots(tmp_path))) == len(lines) - 1


def test_streaming_tracker_equals_batch_episodes_on_random_timelines():
    rng = random.Random(0)
    for trial in range(200):
        # one event observed with occasional restarts and polling gaps; three signatures flicker on and off
        obs, t, session = [], 0.0, "S1"
        for i in range(rng.randint(1, 60)):
            if rng.random() < 0.05:
                session = f"S{i}"
            t += 1.0 if rng.random() > 0.1 else rng.choice([2.5, 4.0, 10.0])
            obs.append((session, i, t))
        present = {sig: {o[:2]: ROW for o in obs if rng.random() < 0.5} for sig in ("x", "y", "z")}

        expected = sorted((sig, e["start"], e["end"], e["lo"], e["hi"], e["n_obs"])
                          for sig, pres in present.items() for e in build_episodes(obs, pres, max_gap=3.0))

        tracker, got = EpisodeTracker(max_gap=3.0), []
        for session, key, t in obs:
            rows = {sig: pres[(session, key)] for sig, pres in present.items() if (session, key) in pres}
            closed, _ = tracker.observe("E", session, t, rows)
            got += [(e["signature"], e["start"], e["end"], e["lo"], e["hi"], e["n_obs"]) for e in closed]
        for _, sig, run in tracker.active():
            e = make_episode(run["session"], run["start"], run["end"], run["t_prev"], None, run["agg"])
            got.append((sig, e["start"], e["end"], e["lo"], e["hi"], e["n_obs"]))

        assert sorted(got) == expected, f"trial {trial}"
