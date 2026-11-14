"""SQLite: the single source of truth for every crawl (settings, queue, pages, progress, log).

The crawler process writes; the Streamlit app reads. WAL mode lets both use the file at the same time. Because the queue
and every crawled page are saved as the crawl goes, a crawl that was paused, or whose computer shut down, can resume.
"""
import json
import os
import sqlite3
import time
from pathlib import Path

HOME = Path(os.environ.get("SEOLENS_HOME") or Path.home() / ".seolens")
DB_PATH = HOME / "crawls.db"
REPORTS = HOME / "reports"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL, updated REAL,
    target TEXT, settings TEXT,
    status TEXT,              -- queued | running | paused | finishing | done | stopped | failed | interrupted
    control TEXT DEFAULT '',  -- '' | pause | finish   (set by the app, read by the crawler)
    pid INTEGER,
    progress TEXT DEFAULT '{}',
    meta TEXT DEFAULT '{}',   -- crawler state needed to resume (host, scope, sitemap, robots, counts)
    result TEXT DEFAULT '{}', -- report paths and score
    error TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS queue (
    run_id INTEGER, idx INTEGER, url TEXT, source TEXT,
    PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS pages (
    run_id INTEGER, idx INTEGER, url TEXT,
    status TEXT, access TEXT,
    row TEXT, extras TEXT,
    PRIMARY KEY (run_id, idx)
);
CREATE TABLE IF NOT EXISTS log (
    run_id INTEGER, ts REAL, msg TEXT
);
CREATE TABLE IF NOT EXISTS snap (
    run_id INTEGER PRIMARY KEY, data TEXT   -- everything the report is built from (snapshot.py), for the app and filtered reports
);
CREATE INDEX IF NOT EXISTS log_run ON log (run_id, ts);
CREATE INDEX IF NOT EXISTS pages_access ON pages (run_id, access);
"""


def _json(v):
    return json.dumps(v, ensure_ascii=False, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o))


class Store:
    def __init__(self, path=None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---------------------------------------------------------------- runs

    def create_run(self, settings):
        now = time.time()
        target = settings.get("site") or ("%d URLs from a list" % len(settings.get("urls") or []))
        cur = self.db.execute("INSERT INTO runs (created, updated, target, settings, status) VALUES (?,?,?,?, 'queued')",
                              (now, now, target, _json(settings)))
        self.db.commit()
        return cur.lastrowid

    def run(self, run_id):
        r = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("settings", "progress", "meta", "result"):
            d[k] = json.loads(d[k] or "{}")
        return d

    def runs(self, limit=50):
        return [dict(r) for r in self.db.execute(
            "SELECT id, created, updated, target, status FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def update_run(self, run_id, **fields):
        if not fields:
            return
        fields["updated"] = time.time()
        for k in ("settings", "progress", "meta", "result"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = _json(fields[k])
        cols = ", ".join("%s=?" % k for k in fields)
        self.db.execute("UPDATE runs SET %s WHERE id=?" % cols, (*fields.values(), run_id))
        self.db.commit()

    def control(self, run_id):
        r = self.db.execute("SELECT control FROM runs WHERE id=?", (run_id,)).fetchone()
        return r[0] if r else ""

    def delete_run(self, run_id):
        for t in ("queue", "pages", "log", "snap"):
            self.db.execute("DELETE FROM %s WHERE run_id=?" % t, (run_id,))
        self.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
        self.db.commit()

    # ---------------------------------------------------------------- crawl data (written in batches by the crawler)

    def add_queue(self, run_id, items):
        """items: [(idx, url, source)]"""
        self.db.executemany("INSERT OR IGNORE INTO queue (run_id, idx, url, source) VALUES (?,?,?,?)",
                            [(run_id, i, u, s) for i, u, s in items])

    def add_pages(self, run_id, items):
        """items: [(idx, row, extras, access)]"""
        self.db.executemany(
            "INSERT OR REPLACE INTO pages (run_id, idx, url, status, access, row, extras) VALUES (?,?,?,?,?,?,?)",
            [(run_id, i, r.get("URL"), str(r.get("Final Status", "")), a, _json(r), _json(e)) for i, r, e, a in items])

    def save_pages_rows(self, run_id, rows_by_idx):
        """After the audit: the final rows (with scores, duplicates, link data) replace the crawl-time rows."""
        self.db.executemany("UPDATE pages SET row=? WHERE run_id=? AND idx=?",
                            [(_json(r), run_id, i) for i, r in rows_by_idx])
        self.db.commit()

    def save_snapshot(self, run_id, data):
        self.db.execute("INSERT OR REPLACE INTO snap (run_id, data) VALUES (?,?)", (run_id, _json(data)))
        self.db.commit()

    def snapshot(self, run_id):
        r = self.db.execute("SELECT data FROM snap WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(r[0]) if r else None

    def commit(self):
        self.db.commit()

    def log(self, run_id, msg):
        self.db.execute("INSERT INTO log (run_id, ts, msg) VALUES (?,?,?)", (run_id, time.time(), msg))

    def logs(self, run_id, limit=200):
        rows = self.db.execute("SELECT ts, msg FROM log WHERE run_id=? ORDER BY rowid DESC LIMIT ?", (run_id, limit)).fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]

    def load_queue(self, run_id):
        return [(r[0], r[1], r[2]) for r in self.db.execute("SELECT idx, url, source FROM queue WHERE run_id=? ORDER BY idx", (run_id,))]

    def load_pages(self, run_id):
        """[(idx, row, extras, access)] in queue order."""
        return [(r[0], json.loads(r[1]), json.loads(r[2]), r[3]) for r in
                self.db.execute("SELECT idx, row, extras, access FROM pages WHERE run_id=? ORDER BY idx", (run_id,))]

    def page_rows(self, run_id):
        return [json.loads(r[0]) for r in self.db.execute("SELECT row FROM pages WHERE run_id=? ORDER BY idx", (run_id,))]

    def access_summary(self, run_id):
        return {r[0]: r[1] for r in self.db.execute("SELECT access, COUNT(*) FROM pages WHERE run_id=? GROUP BY access", (run_id,))}

    def problem_pages(self, run_id):
        return [dict(r) for r in self.db.execute(
            "SELECT url, status, access FROM pages WHERE run_id=? AND access != 'OK' ORDER BY idx", (run_id,))]
