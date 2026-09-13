"""Thin client for Kalshi's public market-data REST API (read-only, no auth).

Plumbing only: HTTP, pacing, retries, pagination. Nothing in this file
computes a number that ends up in the README.
"""
import time

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# /markets/orderbooks rejects more than 100 tickers per call (checked 2026-09-12).
# It also ignores the `depth` param, so trimming levels is done client-side.
MAX_ORDERBOOK_BATCH = 100

RETRY_STATUS = {429, 500, 502, 503, 504}


class KalshiClient:
    def __init__(self, max_requests_per_sec=10.0, timeout=10.0, max_retries=5):
        # Basic tier is ~20 reads/s (docs.kalshi.com/getting_started/rate_limits,
        # read 2026-09-12). Default to half of that.
        self.session = requests.Session()
        self.min_gap = 1.0 / max_requests_per_sec
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request = 0.0

    def _pace(self):
        wait = self._last_request + self.min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, path, params=None):
        """GET with pacing and retries. Returns (json, timing).

        timing["t_send"] / ["t_recv"]: wall-clock unix seconds just before the
        request and just after the body arrived. Use t_recv as the observation
        time of the data.
        timing["rtt"]: round trip in seconds, from a monotonic clock (immune to
        system clock adjustments, so use it for durations).
        """
        url = BASE_URL + path
        err = None
        for attempt in range(self.max_retries):
            self._pace()
            t_send = time.time()
            p0 = time.perf_counter()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                err = exc
            else:
                rtt = time.perf_counter() - p0
                t_recv = time.time()
                if resp.status_code == 200:
                    return resp.json(), {"t_send": t_send, "t_recv": t_recv, "rtt": rtt}
                if resp.status_code not in RETRY_STATUS:
                    raise requests.HTTPError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
                err = f"HTTP {resp.status_code}"
            time.sleep(min(0.5 * 2**attempt, 10))
        raise RuntimeError(f"GET {path} failed after {self.max_retries} tries: {err}")

    def paginate(self, path, params, key):
        """Yield every item under `key`, following Kalshi's cursor."""
        params = dict(params)
        seen = set()
        while True:
            data, _ = self.get(path, params)
            yield from data.get(key) or []
            cursor = data.get("cursor")
            if not cursor or cursor in seen:
                return
            seen.add(cursor)
            params["cursor"] = cursor

    def open_events(self):
        return self.paginate(
            "/events",
            {"status": "open", "with_nested_markets": "true", "limit": 200},
            "events",
        )

    def orderbooks(self, tickers):
        """One HTTP call for up to 100 tickers. Returns (books, timing).

        books: ticker -> {"yes": [[price, size], ...], "no": [[price, size], ...]}
        Both sides are BIDS, as decimal strings, in ascending price order as the
        API sends them -- so the best bid is the LAST level.
        """
        if len(tickers) > MAX_ORDERBOOK_BATCH:
            raise ValueError(f"at most {MAX_ORDERBOOK_BATCH} tickers per call")
        data, timing = self.get("/markets/orderbooks", {"tickers": list(tickers)})
        books = {}
        for ob in data.get("orderbooks") or []:
            fp = ob.get("orderbook_fp") or {}
            books[ob["ticker"]] = {"yes": fp.get("yes_dollars") or [], "no": fp.get("no_dollars") or []}
        return books, timing

    def candlesticks(self, series_ticker, ticker, start_ts, end_ts, period_minutes=1, include_latest_before_start=False):
        params = {"start_ts": int(start_ts), "end_ts": int(end_ts), "period_interval": period_minutes}
        if include_latest_before_start:
            params["include_latest_before_start"] = "true"
        data, _ = self.get(f"/series/{series_ticker}/markets/{ticker}/candlesticks", params)
        return data.get("candlesticks") or []
