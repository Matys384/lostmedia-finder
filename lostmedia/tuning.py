"""Per-platform download pacing that adapts to how the platform actually responds.

The pace climbs one rung at a time only after a full observation window with
no throttling and no degradation (transient errors, latency, throughput).
Any throttle signal (HTTP 429, anti-bot page) moves the pace to a slower rung,
pauses the platform with exponential backoff (or the server's Retry-After) and
caps future climbs below the rung that was refused. State is persisted, so the
fastest stable pace survives restarts. Nothing here retries around a refusal.
"""
import json
import statistics
import time

# Seconds between download starts. Index 0 is the slowest rung.
LADDERS = {"youtube": [8.0, 6.0, 5.0, 4.0, 3.0, 2.5, 2.0, 1.5, 1.0],
           "dailymotion": [4.0, 3.0, 2.0, 1.5, 1.0, 0.75, 0.5],
           "archive": [6.0, 4.0, 3.0, 2.0, 1.5]}
START = {"youtube": 3.0, "dailymotion": 2.0, "archive": 1.5}
WINDOW_SECONDS = 480
WINDOW_ATTEMPTS = 60
BASE_BACKOFF, MAX_BACKOFF = 60, 3600
BOT_CHECK_BACKOFF = 3600

THROTTLED = ("http error 429", "too many requests", "confirm you're not a bot", "confirm you’re not a bot",
             "rate-limit", "rate limit", "throttled")
TRANSIENT = ("timed out", "10060", "10054", "10061", "connecttimeout", "connection reset", "connection refused",
             "remote end closed", "http error 500", "http error 502", "http error 503", "http error 504",
             "temporary failure", "getaddrinfo failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS download_attempts(id INTEGER PRIMARY KEY, source TEXT, spacing REAL, finished REAL,
  seconds REAL, outcome TEXT, bytes INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS download_attempts_source ON download_attempts(source, finished);
CREATE TABLE IF NOT EXISTS tuning(source TEXT PRIMARY KEY, step INTEGER, stable INTEGER, ceiling INTEGER,
  step_started REAL, throttles INTEGER DEFAULT 0, baseline TEXT, history TEXT DEFAULT '[]');
"""


def classify(message):
    """Outcome of one download attempt, from the error text (None = success)."""
    if message is None:
        return "ok"
    text = message.lower()
    if any(term in text for term in THROTTLED):
        return "throttled"
    if any(term in text for term in TRANSIENT):
        return "transient"
    return "content"  # unavailable, restricted, over limits: says nothing about pacing


def retry_after(exc):
    """Best-effort Retry-After seconds from a yt-dlp/httpx error chain."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if headers:
            value = headers.get("Retry-After") or headers.get("retry-after")
            if value:
                try:
                    return float(value)
                except ValueError:
                    from email.utils import parsedate_to_datetime
                    return max(0., parsedate_to_datetime(value).timestamp()-time.time())
        info = getattr(exc, "exc_info", None)
        exc = (info[1] if info else None) or exc.__cause__ or exc.__context__
    return None


def current_spacing(db, source):
    """Pace used by download threads; defaults apply before a Tuner initializes the table."""
    try:
        rows = db.rows("SELECT step FROM tuning WHERE source=?", (source,))
    except Exception:
        rows = []
    if rows and source in LADDERS:
        return LADDERS[source][rows[0]["step"]]
    return START.get(source, 3.0)


class Tuner:
    def __init__(self, db, adaptive=True):
        self.db, self.adaptive = db, adaptive
        with db.connect() as c:
            c.executescript(SCHEMA)
            for source, ladder in LADDERS.items():
                start = ladder.index(START[source])
                c.execute("INSERT OR IGNORE INTO tuning(source,step,stable,ceiling,step_started) VALUES(?,?,?,?,?)",
                          (source, start, start, len(ladder)-1, time.time()))

    def state(self, source):
        rows = self.db.rows("SELECT * FROM tuning WHERE source=?", (source,))
        return rows[0] if rows else None

    def spacing(self, source):
        row = self.state(source)
        return LADDERS[source][row["step"]] if row else 3.0

    def _move(self, source, row, step, reason, **changes):
        history = json.loads(row["history"] or "[]")[-49:]
        history.append({"at": time.time(), "from": LADDERS[source][row["step"]], "to": LADDERS[source][step], "reason": reason})
        fields = {"step": step, "step_started": time.time(), "history": json.dumps(history), **changes}
        self.db.execute("UPDATE tuning SET "+",".join(f"{k}=?" for k in fields)+" WHERE source=?", (*fields.values(), source))

    def window(self, source, since, spacing):
        rows = self.db.rows("SELECT outcome,seconds,finished FROM download_attempts WHERE source=? AND finished>=? AND spacing=?",
                            (source, since, spacing))
        paced = [r for r in rows if r["outcome"] != "content"]
        ok = [r for r in rows if r["outcome"] == "ok"]
        elapsed = max(1., time.time()-since)
        return {"attempts": len(rows), "elapsed": elapsed,
                "rate_per_min": len(ok)*60/elapsed,
                "transient_rate": sum(r["outcome"] == "transient" for r in paced)/max(1, len(paced)),
                "median_seconds": statistics.median([r["seconds"] for r in ok]) if ok else None}

    def record(self, source, spacing, seconds, outcome, size=None, detail=None, wait=None):
        """Log one attempt; returns a pause in seconds when the platform must back off."""
        if source not in LADDERS:
            return None
        self.db.execute("INSERT INTO download_attempts(source,spacing,finished,seconds,outcome,bytes,detail) VALUES(?,?,?,?,?,?,?)",
                        (source, spacing, time.time(), seconds, outcome, size, (detail or "")[:300]))
        row = self.state(source)
        if outcome == "throttled":
            return self._throttled(source, row, detail or "", wait)
        if outcome == "transient":
            recent = self.db.rows("SELECT outcome FROM download_attempts WHERE source=? AND finished>? ORDER BY id DESC LIMIT 3",
                                  (source, time.time()-120))
            if len(recent) == 3 and all(r["outcome"] == "transient" for r in recent):
                pause = min(MAX_BACKOFF, BASE_BACKOFF*2**min(row["throttles"], 6))
                self.db.execute("UPDATE tuning SET throttles=throttles+1 WHERE source=?", (source,))
                return pause
            return None
        if outcome == "ok" and row["throttles"] and LADDERS[source][row["step"]] == spacing:
            # Healthy traffic again: the next refusal starts from a short backoff.
            self.db.execute("UPDATE tuning SET throttles=0 WHERE source=? AND step_started<?", (source, time.time()-WINDOW_SECONDS))
        if self.adaptive:
            self._evaluate(source, row)
        return None

    def _throttled(self, source, row, detail, wait):
        history = json.loads(row["history"] or "[]")
        if row["step_started"] > time.time()-5 and history and history[-1]["reason"].startswith("throttled"):
            # In-flight requests started before the slowdown report the same refusal.
            return None
        # Refused while probing: back to the last proven pace. Refused at the proven pace: one rung slower.
        slower = row["stable"] if row["step"] > row["stable"] else max(0, row["step"]-1)
        backoff = BOT_CHECK_BACKOFF if "not a bot" in detail.lower() or "not a bot" in detail.lower().replace("’", "'") \
            else min(MAX_BACKOFF, BASE_BACKOFF*2**min(row["throttles"], 6))
        pause = max(wait or 0, backoff)
        self._move(source, row, slower, f"throttled at {LADDERS[source][row['step']]}s: {detail[:80]}",
                   stable=slower, ceiling=slower, throttles=row["throttles"]+1, baseline=None)
        return pause

    def _evaluate(self, source, row):
        ladder, spacing = LADDERS[source], LADDERS[source][row["step"]]
        current = self.window(source, row["step_started"], spacing)
        if current["elapsed"] < WINDOW_SECONDS or current["attempts"] < WINDOW_ATTEMPTS:
            return
        baseline = json.loads(row["baseline"]) if row["baseline"] else None
        healthy = baseline is None or (
            current["transient_rate"] <= baseline["transient_rate"]+0.02
            and (current["median_seconds"] or 0) <= 1.5*(baseline["median_seconds"] or float("inf"))
            and current["rate_per_min"] >= 0.95*baseline["rate_per_min"])
        if not healthy:
            if row["step"] > row["stable"]:
                self._move(source, row, row["stable"], f"degraded at {spacing}s: {json.dumps(current)}", ceiling=row["step"]-1)
            else:
                # Drift at the stable rung (time of day, mix of videos): re-baseline, keep the pace.
                self.db.execute("UPDATE tuning SET baseline=?,step_started=? WHERE source=?", (json.dumps(current), time.time(), source))
            return
        stable = row["step"]
        if stable+1 <= min(row["ceiling"], len(ladder)-1):
            self._move(source, row, stable+1, f"stable at {spacing}s, trying faster", stable=stable, baseline=json.dumps(current))
        elif row["stable"] != stable or not baseline:
            self.db.execute("UPDATE tuning SET stable=?,baseline=?,step_started=? WHERE source=?",
                            (stable, json.dumps(current), time.time(), source))

    def report(self):
        out = {}
        for row in self.db.rows("SELECT * FROM tuning"):
            ladder = LADDERS.get(row["source"])
            if not ladder:
                continue
            out[row["source"]] = {"spacing": ladder[row["step"]], "stable_spacing": ladder[row["stable"]],
                                  "fastest_allowed": ladder[row["ceiling"]], "throttles": row["throttles"],
                                  "baseline": json.loads(row["baseline"]) if row["baseline"] else None,
                                  "history": json.loads(row["history"] or "[]")[-10:]}
        return out
