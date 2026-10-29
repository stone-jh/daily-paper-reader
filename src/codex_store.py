"""Durable local inbox shared by the HTTP UI and the serial Codex worker."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


DEFAULTS = {
    "keywords": ["agent", "tool use", "reinforcement learning", "evaluation", "benchmark"],
    "sources": ["huggingface", "arxiv"],
    "days": 7,
    "fetch_limit": 60,
    "daily_hour": 9,
    "daily_enabled": True,
    "auto_process": 3,
    "paused": False,
    "timeout_seconds": 900,
}


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.directory = self.root / ".codex-reader"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "inbox.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                    url TEXT NOT NULL, title TEXT NOT NULL, abstract TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}', origin TEXT NOT NULL,
                    status TEXT NOT NULL, depth TEXT NOT NULL DEFAULT 'quick',
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', route TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS papers_status ON papers(status, created);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def config(self):
        return {**DEFAULTS, **self.setting("config", {})}

    def save_config(self, patch):
        config = self.config()
        for key, value in patch.items():
            if key not in DEFAULTS:
                raise ValueError(f"Unknown setting: {key}")
            if key in {"paused", "daily_enabled"}:
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be a boolean")
            elif key == "keywords":
                if not isinstance(value, list) or len(value) > 20 or any(
                    not isinstance(v, str) or not v.strip() or len(v) > 100 for v in value
                ):
                    raise ValueError("Use at most 20 nonempty keywords, each at most 100 characters")
                value = list(dict.fromkeys(v.strip() for v in value))
            elif key == "sources":
                if not isinstance(value, list) or not value or set(value) - {"huggingface", "arxiv"}:
                    raise ValueError("Select huggingface and/or arxiv")
            else:
                limits = {"days": (1, 30), "fetch_limit": (1, 200), "daily_hour": (0, 23),
                          "auto_process": (0, 20), "timeout_seconds": (60, 3600)}
                low, high = limits[key]
                if type(value) is not int or not low <= value <= high:
                    raise ValueError(f"{key} must be between {low} and {high}")
            config[key] = value
        self.set_setting("config", config)
        return config

    def add(self, paper, origin="manual", depth="quick"):
        if depth not in {"quick", "deep"}:
            raise ValueError("depth must be quick or deep")
        identity = paper["identity"]
        paper_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        now = time.time()
        with self.connect() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO papers
                   (id,identity,url,title,abstract,metadata,origin,status,depth,created,updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (paper_id, identity, paper["url"], paper.get("title") or paper["url"],
                 paper.get("abstract", ""), json.dumps(paper, ensure_ascii=False), origin,
                 "queued" if origin == "manual" else "candidate", depth, now, now),
            )
            added = cursor.rowcount == 1
            if not added and origin == "manual":
                db.execute("""UPDATE papers SET status='queued',origin='manual',depth=?,updated=?
                              WHERE id=? AND status IN ('candidate','ignored')""", (depth, now, paper_id))
        return paper_id, added

    def get(self, paper_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return dict(row) if row else None

    def list(self, status="", search="", page=1):
        where, args = ["1=1"], []
        if status:
            where.append("status=?")
            args.append(status)
        if search:
            where.append("(title LIKE ? OR url LIKE ? OR abstract LIKE ?)")
            args.extend([f"%{search}%"] * 3)
        clause = " AND ".join(where)
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM papers WHERE {clause}", args).fetchone()[0]
            rows = db.execute(f"SELECT * FROM papers WHERE {clause} ORDER BY created DESC,id LIMIT 50 OFFSET ?",
                              [*args, (max(1, page) - 1) * 50]).fetchall()
            counts = dict(db.execute("SELECT status,COUNT(*) FROM papers GROUP BY status").fetchall())
        return {"papers": [dict(row) for row in rows], "total": total, "counts": counts, "page": page}

    def action(self, ids, action, depth="quick"):
        if not isinstance(ids, list) or not ids or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
            raise ValueError("Select between 1 and 100 papers")
        if depth not in {"quick", "deep"}:
            raise ValueError("depth must be quick or deep")
        if action not in {"enqueue", "ignore", "retry", "deepen"}:
            raise ValueError("Unknown action")
        allowed = {"enqueue": ("candidate", "ignored"), "retry": ("failed",),
                   "ignore": ("candidate", "queued", "failed"), "deepen": ("completed",)}[action]
        if action == "deepen":
            depth = "deep"
        status = "ignored" if action == "ignore" else "queued"
        changed = 0
        with self.connect() as db:
            for paper_id in ids:
                changed += db.execute(
                    f"""UPDATE papers SET status=?,depth=?,attempts=0,retry_at=0,error='',updated=?
                        WHERE id=? AND status IN ({','.join('?' for _ in allowed)})""",
                    (status, depth, time.time(), paper_id, *allowed),
                ).rowcount
        return changed

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM papers WHERE status='queued' AND retry_at<=?
                                ORDER BY (origin='manual') DESC,created,id LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                return None
            db.execute("UPDATE papers SET status='running',attempts=attempts+1,updated=? WHERE id=?",
                       (time.time(), row["id"]))
        return self.get(row["id"])

    def recover(self):
        # Called only after acquiring the process-wide worker lock.
        with self.connect() as db:
            db.execute("""UPDATE papers SET status='queued',error='Worker interrupted; resuming',updated=?
                          WHERE status='running'""", (time.time(),))

    def finish(self, paper_id, **fields):
        allowed = {"status", "error", "route", "title", "metadata", "retry_at"}
        if set(fields) - allowed:
            raise ValueError("Invalid paper update")
        fields["updated"] = time.time()
        with self.connect() as db:
            db.execute(f"UPDATE papers SET {','.join(k+'=?' for k in fields)} WHERE id=?",
                       (*fields.values(), paper_id))
