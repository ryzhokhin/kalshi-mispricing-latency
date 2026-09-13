"""Local web dashboard: live opportunities, alerts, lifetimes, replay and history.

    python -m dashboard.server              # http://127.0.0.1:8050
    python -m dashboard.server --port 9000 --universe data/universe.json

Endpoints
  GET /                      the dashboard
  GET /api/live?since=<id>   health, KPIs, open violations, alerts newer than <id>
  GET /api/stream            the same payload as server-sent events, once a second
  GET /api/full              everything above plus charts, closed episodes, replay, latency
  GET /api/history           the 7-day candle study (results/history/summary.json + episodes)
  GET /api/book?ticker=T     current top of book for one market
  GET /api/export/<name>.csv episodes | alerts | replay, for backups and offline work
"""
import argparse
import csv
import io
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dashboard.engine import LiveEngine, run_forever
from mispricing.episodes import kaplan_meier, km_at, km_inputs, quantile

STATIC = Path(__file__).parent / "static"


def history_payload(summary_path="results/history/summary.json", episodes_path="data/episodes_history.csv"):
    if not Path(summary_path).exists():
        return None
    summary = json.loads(Path(summary_path).read_text())
    eps = []
    if Path(episodes_path).exists():
        with open(episodes_path) as f:
            for r in csv.DictReader(f):
                num = lambda k: float(r[k]) if r.get(k) not in (None, "") else None
                eps.append({"signature": r["signature"], "kind": r["kind"], "event_ticker": r["event_ticker"],
                            "start": num("start"), "lo": num("lo"), "hi": num("hi"),
                            "max_gross": num("max_gross"), "max_best_net": num("max_best_net"),
                            "min_leg_volume_24h": num("min_leg_volume_24h")})
    grid = []
    if eps:
        curve = kaplan_meier(*km_inputs(eps))
        positives = [e["lo"] for e in eps if e["lo"] > 0] + [e["hi"] for e in eps if e["hi"]]
        x_min, x_max = max(1.0, min(positives)), max(positives)
        xs = [x_min * (x_max / x_min) ** (i / 119) for i in range(120)]
        n = len(eps)
        grid = [{"t": x, "km": km_at(curve, x), "lo": sum(e["lo"] > x for e in eps) / n,
                 "hi": sum(e["hi"] is None or e["hi"] > x for e in eps) / n} for x in xs]
    unc = [e for e in eps if e["hi"] is not None]
    return {"summary": summary, "episodes": sorted(eps, key=lambda e: -(e["start"] or 0))[:2000], "survival": grid,
            "median_lo": quantile([e["lo"] for e in unc], 0.5), "median_hi": quantile([e["hi"] for e in unc], 0.5)}


def to_csv(rows):
    rows = list(rows)
    if not rows:
        return ""
    keys = [k for k in rows[0].keys() if k not in ("legs", "replay")]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, ctype="application/json", status=200):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, status=200):
            self._send(json.dumps(obj, default=str), status=status)

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            path = url.path
            if path == "/":
                return self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/static/"):
                f = (STATIC / path.removeprefix("/static/")).resolve()
                if STATIC.resolve() in f.parents and f.is_file():
                    return self._send(f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
                return self._send("not found", "text/plain", 404)
            if path == "/api/live":
                return self._json(engine.live_view(int(q.get("since", ["0"])[0])))
            if path == "/api/full":
                return self._json(engine.full_view())
            if path == "/api/history":
                return self._json(history_payload())
            if path == "/api/book":
                book = engine.book_view(q.get("ticker", [""])[0])
                return self._json(book) if book else self._json({"error": "unknown ticker"}, 404)
            if path == "/api/stream":
                return self._stream(int(q.get("since", ["0"])[0]))
            if path.startswith("/api/export/") and path.endswith(".csv"):
                name = path.removeprefix("/api/export/").removesuffix(".csv")
                view = engine.full_view()
                source = {"episodes": view["episodes"], "alerts": view["alerts"], "replay": view["replay"]["recent"]}
                if name not in source:
                    return self._send("unknown export", "text/plain", 404)
                if name == "replay":
                    source["replay"] = engine.replays
                return self._send(to_csv(source[name]), "text/csv; charset=utf-8")
            return self._send("not found", "text/plain", 404)

        def _stream(self, since):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    payload = engine.live_view(since)
                    if payload["alerts"]:
                        since = payload["alerts"][-1]["id"]
                    self.wfile.write(f"event: live\ndata: {json.dumps(payload, default=str)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(1.0)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8050)))
    ap.add_argument("--universe", default="data/universe.json")
    ap.add_argument("--series-fees", default="data/series_fees.json")
    ap.add_argument("--snapshots", default="data/snapshots")
    ap.add_argument("--latency", default="data/latency.json")
    args = ap.parse_args()

    engine = LiveEngine(args.universe, args.series_fees, args.snapshots, args.latency)
    t0 = time.time()
    print("replaying the recording so far ...", flush=True)
    while engine.poll() and not engine.caught_up:  # stop once within seconds of the recorder, even while it writes
        pass
    print(f"caught up: {engine.health['batches']:,} batches in {time.time() - t0:.1f}s", flush=True)
    threading.Thread(target=run_forever, args=(engine,), daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine))
    server.daemon_threads = True
    print(f"dashboard on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
