"""Readers for what universe.py and recorder.py write: whole recordings, or a live tail of one."""
import gzip
import json
from pathlib import Path


def load_universe(path="data/universe.json"):
    return json.loads(Path(path).read_text())


def _lines(path):
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt") as f:
            yield from f
    except EOFError:  # gzip cut short by a hard kill: keep what was readable
        return


def _snapshot_files(snapshot_dir):
    d = Path(snapshot_dir)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz")))


class Assembler:
    """Turns raw recorder lines into batches with FULL books.

    feed(rec) returns None for headers, the record unchanged for error batches,
    and otherwise {"session", "cycle", "batch", "t_send", "t_recv", "rtt",
    "books": {ticker: {"yes": [[price, size], ...], "no": [...]}}}.

    Unchanged books are filled from that ticker's last recorded book within the
    same session, and the SAME object is handed back, so `is` tells callers what
    changed. A new session never inherits books: a gap is unknown, not unchanged.
    """

    def __init__(self):
        self.batches, self.last, self.session = None, {}, None

    def feed(self, rec):
        if rec["session"] != self.session:
            self.batches, self.last, self.session = None, {}, rec["session"]
        if rec["type"] == "header":
            self.batches = rec["batches"]
            return None
        if "error" in rec or self.batches is None:
            return rec if "error" in rec else None
        self.last.update(rec.pop("books"))
        missing = set(rec.pop("missing", ()))
        requested = self.batches[rec["batch"]]
        rec["books"] = {t: self.last[t] for t in requested if t not in missing and t in self.last}
        return rec


def iter_snapshots(snapshot_dir="data/snapshots"):
    """Every recorded batch in order, with full books (see Assembler). A truncated last line is skipped."""
    assembler = Assembler()
    for path in _snapshot_files(snapshot_dir):
        for line in _lines(path):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            out = assembler.feed(rec)
            if out is not None:
                yield out


def iter_observations(snapshot_dir="data/snapshots"):
    """Cheap pass over a recording that skips rebuilding books. Yields header
    records unchanged, error records unchanged, and for every good batch:

        {"type": "batch", "session", "cycle", "batch", "t_recv", "rtt",
         "tickers": [tickers the API actually returned in this batch]}
    """
    batches, session = None, None
    for path in _snapshot_files(snapshot_dir):
        for line in _lines(path):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec["session"] != session:
                batches, session = None, rec["session"]
            if rec["type"] == "header":
                batches = rec["batches"]
                yield rec
                continue
            if "error" in rec:
                yield rec
                continue
            if batches is None:
                continue
            missing = set(rec.get("missing", ()))
            yield {"type": "batch", "session": rec["session"], "cycle": rec["cycle"], "batch": rec["batch"],
                   "t_recv": rec["t_recv"], "rtt": rec["rtt"],
                   "tickers": [t for t in batches[rec["batch"]] if t not in missing]}


class SnapshotTailer:
    """Reads a recording that is still being written, without re-reading or skipping lines.

    The recorder appends to YYYYMMDD_HH_<session>.jsonl and gzips the file when
    the hour ends. The tailer remembers, per file stem, the byte offset and line
    count already consumed. Only complete lines (ending in a newline) are
    consumed, so a line being written is picked up on the next poll. When a
    stem's .jsonl turns into .jsonl.gz, the lines already consumed are skipped.
    """

    def __init__(self, snapshot_dir):
        self.dir = Path(snapshot_dir)
        self.offsets = {}   # stem -> byte offset into the .jsonl
        self.lines = {}     # stem -> lines consumed
        self.finished = set()

    def poll(self):
        """Parsed records appended since the last call, in recording order."""
        out = []
        for path in _snapshot_files(self.dir):
            stem = path.name.removesuffix(".gz")
            if stem in self.finished:
                continue
            if path.suffix == ".gz":
                skip = self.lines.get(stem, 0)
                for i, line in enumerate(_lines(path)):
                    if i >= skip and line.endswith("\n"):
                        out.append(line)
                self.finished.add(stem)
                continue
            try:
                with path.open("rb") as f:
                    f.seek(self.offsets.get(stem, 0))
                    chunk = f.read()
            except FileNotFoundError:  # compressed between listing and opening: handled next poll
                continue
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            complete = chunk[: end + 1]
            self.offsets[stem] = self.offsets.get(stem, 0) + len(complete)
            text = complete.decode()
            self.lines[stem] = self.lines.get(stem, 0) + text.count("\n")
            out.extend(text.splitlines(keepends=True))
        records = []
        for line in out:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records
