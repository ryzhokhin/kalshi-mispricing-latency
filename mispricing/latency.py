"""Measure the detect-to-order latency budget, piece by piece. Never places an order.

A violation that starts at time 0 is only catchable if it is still there when
our order reaches the exchange:

    T = detection lag      we poll every D seconds, so we see it after Uniform(0, D)
      + data fetch         GET /markets/orderbooks round trip (the poll itself)
      + compute            quotes + detectors for one batch, in Python
      + order round trip   POST to the order endpoint until the exchange answers

Order probe: POST /portfolio/events/orders (Kalshi's V2 create-order path, per
docs.kalshi.com, read 2026-09-12) with an EMPTY body and NO credentials. The
exchange's auth layer rejects it with 401 before anything else, so this is a
LOWER bound on a real order: it skips RSA request signing (~1 ms), signature
verification, risk checks and matching.

Where requests go: TCP connect lands on an Amazon CloudFront edge near us
(the x-amz-cf-pop header, e.g. LAX), so the connect time is the RTT to the
EDGE, not to the exchange. The edge-to-exchange leg hides inside time-to-first-byte.

Scale: a colocated order is tens of microseconds. This is a Python poller on a
residential connection, measured in hundreds of milliseconds. The goal is to
quantify that reaction time and what it rules out, not to compete on speed.

    python -m mispricing.latency --samples 40
"""
import argparse
import http.client
import json
import socket
import ssl
import statistics
import time
from pathlib import Path
from urllib.parse import urlencode

from .detect import detect_event, quotes_from_book
from .fees import load_series_fees
from .api import BASE_URL, KalshiClient
from .storage import load_universe
from .recorder import pack_batches

HOST = BASE_URL.split("/")[2]
PREFIX = "/" + BASE_URL.split("/", 3)[3]
ORDER_PATH = PREFIX + "/portfolio/events/orders"


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else None


def describe(xs):
    return {"n": len(xs), "p50": pct(xs, 0.5), "p95": pct(xs, 0.95), "p99": pct(xs, 0.99),
            "min": min(xs) if xs else None, "mean": statistics.fmean(xs) if xs else None}


# ---- probes -------------------------------------------------------------------------

def probe_fresh_connection():
    """DNS, TCP connect (~1 RTT to the CDN edge) and TLS handshake on a brand-new connection."""
    t0 = time.perf_counter()
    family, kind, proto, _, addr = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)[0]
    t1 = time.perf_counter()
    sock = socket.socket(family, kind, proto)
    sock.settimeout(10)
    sock.connect(addr)
    t2 = time.perf_counter()
    tls = ssl.create_default_context().wrap_socket(sock, server_hostname=HOST)
    t3 = time.perf_counter()
    tls.close()
    return {"dns": t1 - t0, "tcp_connect": t2 - t1, "tls_handshake": t3 - t2}


class WarmConnection:
    """One persistent HTTPS connection, like a real trading loop keeps open."""

    def __init__(self):
        self.conn = http.client.HTTPSConnection(HOST, timeout=10)
        self.edge = None

    def timed(self, method, path, body=None):
        headers = {"Connection": "keep-alive", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        reused = self.conn.sock is not None  # False = http.client silently reconnects (handshake inside timing)
        t0 = time.perf_counter()
        self.conn.request(method, path, body=body, headers=headers)
        resp = self.conn.getresponse()  # returns once status line + headers arrived
        t1 = time.perf_counter()
        data = resp.read()
        t2 = time.perf_counter()
        self.edge = resp.getheader("x-amz-cf-pop") or self.edge
        return resp.status, data, {"ttfb": t1 - t0, "total": t2 - t0, "reused": reused}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=40, help="requests per network probe (paced, ~2/s)")
    ap.add_argument("--compute-rounds", type=int, default=20)
    ap.add_argument("--no-order-probe", action="store_true", help="skip the rejected POST to the order endpoint")
    ap.add_argument("--universe", default="data/universe.json")
    ap.add_argument("--out", default="data/latency.json")
    args = ap.parse_args()

    universe = load_universe(args.universe)
    fees = load_series_fees()
    batches = pack_batches(universe["events"])
    s = {k: [] for k in ["dns", "tcp_connect", "tls_handshake", "get_ttfb", "get_total", "json_parse",
                         "compute_batch", "post_ttfb", "post_total"]}
    statuses = {"get": {}, "post": {}}

    print(f"probing {HOST}: {args.samples} fresh connections ...", flush=True)
    for _ in range(args.samples):
        for k, v in probe_fresh_connection().items():
            s[k].append(v)
        time.sleep(0.25)

    warm = WarmConnection()
    get_path = PREFIX + "/markets/orderbooks?" + urlencode([("tickers", t) for t in batches[0]])
    warm.timed("GET", get_path)  # first request pays the handshake; discard it
    print(f"warm GET orderbooks ({len(batches[0])} tickers) x{args.samples} ...", flush=True)
    for _ in range(args.samples):
        status, data, t = warm.timed("GET", get_path)
        statuses["get"][status] = statuses["get"].get(status, 0) + 1
        if status == 429:
            print("  rate limited; stopping this probe")
            break
        if status != 200 or not t["reused"]:
            continue  # a failed or reconnected request is not a warm poll
        p0 = time.perf_counter()
        json.loads(data)
        s["json_parse"].append(time.perf_counter() - p0)
        s["get_ttfb"].append(t["ttfb"])
        s["get_total"].append(t["total"])
        time.sleep(0.5)

    if not args.no_order_probe:
        print(f"warm POST {ORDER_PATH} (empty body, no credentials -> expect 401) x{args.samples} ...", flush=True)
        warm.timed("POST", ORDER_PATH, body="{}")
        for _ in range(args.samples):
            status, _, t = warm.timed("POST", ORDER_PATH, body="{}")
            statuses["post"][status] = statuses["post"].get(status, 0) + 1
            if status == 429:
                print("  rate limited; stopping this probe")
                break
            if status in (200, 201):  # impossible without credentials; refuse to continue if it ever happens
                raise SystemExit("order endpoint accepted a request -- stopping immediately")
            if not t["reused"]:
                statuses["post_reconnects"] = statuses.get("post_reconnects", 0) + 1
                continue
            s["post_ttfb"].append(t["ttfb"])
            s["post_total"].append(t["total"])
            time.sleep(0.5)

    print(f"compute: detectors over {len(batches)} live batches x{args.compute_rounds} ...", flush=True)
    client = KalshiClient()
    event_of = {m["ticker"]: ev for ev in universe["events"] for m in ev["markets"]}
    live = [client.orderbooks(b)[0] for b in batches]
    now = time.time()
    for _ in range(args.compute_rounds):
        for books in live:
            t0 = time.perf_counter()
            quotes = {t: quotes_from_book(b) for t, b in books.items()}
            for ev in {event_of[t]["event_ticker"]: event_of[t] for t in quotes}.values():
                detect_event(ev, quotes, fees[ev["series_ticker"]], now)
            s["compute_batch"].append(time.perf_counter() - t0)

    summary = {k: describe(v) for k, v in s.items()}
    out = {"measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "host": HOST,
           "cdn_edge": warm.edge, "order_path": ORDER_PATH, "statuses": statuses, "summary": summary, "samples": s}
    Path(args.out).write_text(json.dumps(out, indent=1))

    ms = lambda x: "-" if x is None else f"{x * 1000:7.1f}"
    print(f"\nCDN edge: {warm.edge}   GET statuses {statuses['get']}   POST statuses {statuses['post']}")
    print(f"  {'component (ms)':42s} {'p50':>7s} {'p95':>7s} {'p99':>7s} {'n':>4s}")
    rows = [("DNS lookup (cached by the OS)", "dns"),
            ("TCP connect = RTT to CDN edge", "tcp_connect"),
            ("TLS handshake (fresh connection only)", "tls_handshake"),
            ("GET orderbooks, time to first byte", "get_ttfb"),
            ("GET orderbooks, total (the poll)", "get_total"),
            ("  JSON parse", "json_parse"),
            ("compute: quotes + detectors, one batch", "compute_batch"),
            ("POST order endpoint (401), first byte", "post_ttfb"),
            ("POST order endpoint (401), total", "post_total")]
    for label, k in rows:
        d = summary[k]
        print(f"  {label:42s} {ms(d['p50'])} {ms(d['p95'])} {ms(d['p99'])} {d['n']:4d}")
    if s["post_ttfb"] and s["tcp_connect"]:
        beyond = pct(s["post_ttfb"], 0.5) - pct(s["tcp_connect"], 0.5)
        print(f"\n  order first byte minus edge RTT ~ {beyond * 1000:.0f} ms: edge-to-exchange leg + auth rejection")
    print(f"\nwrote {args.out}. episodes.py now uses these measurements for the budget.")


if __name__ == "__main__":
    main()
