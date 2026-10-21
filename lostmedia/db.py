import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
 url TEXT NOT NULL, title TEXT NOT NULL, published TEXT, local_path TEXT,
 is_reference INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
 UNIQUE(source, external_id, is_reference));
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, video_id INTEGER NOT NULL REFERENCES videos(id),
 state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 lease_until REAL, owner TEXT, error TEXT, updated REAL NOT NULL,
 UNIQUE(video_id));
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, lease_until, id);
CREATE INDEX IF NOT EXISTS videos_published ON videos(published,source);
CREATE TABLE IF NOT EXISTS features (
 video_id INTEGER NOT NULL REFERENCES videos(id), signature TEXT NOT NULL,
 path TEXT NOT NULL, frames INTEGER NOT NULL, PRIMARY KEY(video_id, signature));
CREATE TABLE IF NOT EXISTS matches (
 video_id INTEGER NOT NULL REFERENCES videos(id), reference_id INTEGER NOT NULL REFERENCES videos(id),
 signature TEXT NOT NULL, score REAL NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(video_id, reference_id, signature));
CREATE INDEX IF NOT EXISTS matches_score ON matches(score DESC);
CREATE TABLE IF NOT EXISTS searches (
 key TEXT PRIMARY KEY, cursor TEXT, done INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rate_limits (name TEXT PRIMARY KEY, next_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS quota (day TEXT, provider TEXT, calls INTEGER NOT NULL, PRIMARY KEY(day, provider));
CREATE TABLE IF NOT EXISTS processing_metrics(video_id INTEGER PRIMARY KEY, accessible INTEGER NOT NULL DEFAULT 0, seconds REAL, frames INTEGER, feature_bytes INTEGER, completed REAL);
"""


class Database:
    def __init__(self, root):
        self.path = root / "catalog.sqlite3"
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Runtime/channel (targeted campaigns) and queue priority, added after the first audits.
            for table, column in (("videos","duration REAL"),("videos","channel TEXT"),("jobs","priority INTEGER NOT NULL DEFAULT 0")):
                if column.split()[0] not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_priority ON jobs(state, priority DESC, id)")
            # Pre-tuning builds stored long pauses on the spacing key itself.
            conn.execute("INSERT OR REPLACE INTO rate_limits SELECT 'cooldown:'||name,next_at FROM rate_limits WHERE name LIKE 'download:%' AND next_at>?",(time.time()+60,))
            conn.execute("UPDATE rate_limits SET next_at=? WHERE name LIKE 'download:%' AND next_at>?",(time.time(),time.time()+60))
            # Per-stage timings, added after the first audits (older rows stay NULL).
            known={r[1] for r in conn.execute("PRAGMA table_info(processing_metrics)")}
            for column in ("download_seconds REAL","extract_seconds REAL","match_seconds REAL","media_bytes INTEGER","decode_seconds REAL","gpu_seconds REAL"):
                if column.split()[0] not in known:
                    conn.execute("ALTER TABLE processing_metrics ADD COLUMN "+column)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def rows(self, sql, args=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as c:
            return c.execute(sql, args).rowcount

    def add(self, source, external_id, url, title, published=None, local_path=None, reference=False):
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO videos(source,external_id,url,title,published,local_path,is_reference,created) VALUES(?,?,?,?,?,?,?,?)",
                      (source, external_id, url, title, published, local_path, int(reference), time.time()))
            row = c.execute("SELECT id FROM videos WHERE source=? AND external_id=? AND is_reference=?", (source, external_id, int(reference))).fetchone()
            vid = row[0]
            if local_path:
                c.execute("UPDATE videos SET local_path=? WHERE id=?", (local_path, vid))
            if not reference:
                c.execute("INSERT OR IGNORE INTO jobs(video_id,updated) VALUES(?,?)", (vid, time.time()))
                if published and c.execute("SELECT name FROM sqlite_master WHERE name='periods'").fetchone():
                    c.execute("INSERT OR IGNORE INTO period_videos SELECT id,?,? FROM periods WHERE start<=? AND end>=?",(source,vid,published[:10],published[:10]))
            return vid

    def claim(self, lease=300,period_id=None,source=None,cached=None):
        owner = uuid.uuid4().hex
        now = time.time()
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            clause=" AND video_id IN (SELECT video_id FROM period_videos WHERE period_id=?)" if period_id is not None else ""
            args=(now,period_id) if period_id is not None else (now,)
            if source:
                clause+=" AND video_id IN (SELECT id FROM videos WHERE source=?)"
                args+= (source,)
            if cached:
                # Rematch pass: only videos whose features are already stored for this signature.
                clause+=" AND video_id IN (SELECT video_id FROM features WHERE signature=?)"
                args+= (cached,)
            row = c.execute("SELECT id,video_id FROM jobs WHERE (state='queued' OR (state='running' AND lease_until<?))"+clause+" ORDER BY priority DESC, id LIMIT 1", args).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET state='running',owner=?,lease_until=?,attempts=attempts+1,updated=? WHERE id=?", (owner, now+lease, now, row[0]))
            return dict(c.execute("SELECT videos.*,jobs.id job_id,jobs.owner FROM videos JOIN jobs ON videos.id=jobs.video_id WHERE jobs.id=?", (row[0],)).fetchone())

    def finish(self, job, error=None):
        self.execute("UPDATE jobs SET state=?,error=?,lease_until=NULL,updated=? WHERE id=? AND owner=?",
                     ("failed" if error else "done", error, time.time(), job["job_id"], job["owner"]))

    def requeue(self, job, refund=True):
        """Return a claimed job untouched by its outcome (cooldown, storage pause, stop)."""
        self.execute("UPDATE jobs SET state='queued',owner=NULL,lease_until=NULL,attempts=attempts-?,updated=? WHERE id=? AND owner=?",
                     (int(refund), time.time(), job["job_id"], job["owner"]))

    def save_match(self, vid, ref, signature, payload):
        self.execute("INSERT OR REPLACE INTO matches VALUES(?,?,?,?,?)", (vid, ref, signature, payload["score"], json.dumps(payload)))
