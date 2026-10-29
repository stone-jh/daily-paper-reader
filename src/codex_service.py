"""Single-worker scheduling and daily discovery for the local reader."""

from __future__ import annotations

import fcntl
import json
import re
import threading
import time
from datetime import datetime

from codex_processor import process_paper, publish_index
from codex_sources import discover, normalize_link, parse_links
from codex_store import Store


class ReaderService:
    def __init__(self, root):
        self.store = Store(root)
        self.stop_event = threading.Event()
        self.fetch_lock = threading.Lock()
        self.fetching = False
        self.worker = None
        self.lock_file = None

    def start(self):
        self.lock_file = (self.store.directory / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            raise RuntimeError("Another Codex reader worker is already using this inbox")
        self.store.recover()
        publish_index(self.store)
        self.worker = threading.Thread(target=self.loop, name="codex-reader", daemon=True)
        self.worker.start()

    def import_links(self, text, depth="quick"):
        if not isinstance(text, str) or len(text) > 100000:
            raise ValueError("Import text must be under 100 KB")
        links = parse_links(text)
        if not links or len(links) > 100:
            raise ValueError("Provide between 1 and 100 paper links")
        added, duplicates, errors = [], [], []
        for link in links:
            try:
                paper_id, fresh = self.store.add(normalize_link(link), depth=depth)
                (added if fresh else duplicates).append(paper_id)
            except ValueError as exc:
                errors.append({"url": link, "error": str(exc)})
        return {"added": added, "duplicates": duplicates, "errors": errors}

    def fetch(self):
        if not self.fetch_lock.acquire(blocking=False):
            raise ValueError("Discovery is already running")
        self.fetching = True
        try:
            config = self.store.config()
            papers, reports, errors = discover(config)
            added, duplicates, eligible = [], 0, []
            # Within each feed, aliases for an unversioned HF item and the latest
            # arXiv item resolve to the explicit version before entering the queue.
            versions = {re.sub(r"v\d+$", "", p["arxiv_id"]): p for p in papers if re.search(r"v\d+$", p.get("arxiv_id", ""))}
            for paper in papers:
                if paper.get("arxiv_id") in versions:
                    paper = {**paper, **versions[paper["arxiv_id"]]}
                paper_id, fresh = self.store.add(paper, origin=paper["source"])
                if fresh:
                    added.append(paper_id)
                else:
                    duplicates += 1
                if paper_id not in eligible and self.store.get(paper_id)["status"] == "candidate":
                    eligible.append(paper_id)
            today = datetime.now().date().isoformat()
            quota = self.store.setting("auto_quota", {})
            used = quota.get("count", 0) if quota.get("date") == today else 0
            selected = eligible[:max(0, config["auto_process"] - used)]
            if selected:
                self.store.action(selected, "enqueue")
            self.store.set_setting("auto_quota", {"date": today, "count": used + len(selected)})
            report = {"date": today, "finished_at": time.time(), "added": len(added),
                      "duplicates": duplicates, "queued": len(selected), "sources": reports,
                      "errors": errors, "status": "partial" if errors and reports else "failed" if errors else "completed"}
            self.store.set_setting("last_fetch", report)
            return report
        finally:
            self.fetching = False
            self.fetch_lock.release()

    def fetch_async(self):
        if self.fetching:
            return
        def run():
            try:
                self.fetch()
            except Exception as exc:
                self.store.set_setting("last_fetch", {"date": datetime.now().date().isoformat(),
                    "finished_at": time.time(), "status": "failed", "errors": [{"source": "discovery", "error": str(exc)}]})
        threading.Thread(target=run, name="paper-discovery", daemon=True).start()

    def process_one(self):
        row = self.store.claim()
        if not row:
            return False
        try:
            process_paper(self.store, row)
        except Exception as exc:
            retry = row["attempts"] < 2
            self.store.finish(row["id"], status="queued" if retry else "failed",
                              error=f"{type(exc).__name__}: {exc}"[:1000],
                              retry_at=time.time() + 60 if retry else 0)
        return True

    def loop(self):
        while not self.stop_event.is_set():
            try:
                config = self.store.config()
                now = datetime.now()
                last_fetch = self.store.setting("last_fetch", {})
                if config["daily_enabled"] and now.hour >= config["daily_hour"] and last_fetch.get("date") != now.date().isoformat():
                    self.fetch_async()
                if not config["paused"] and self.process_one():
                    continue
            except Exception as exc:
                self.store.set_setting("worker_error", str(exc)[:1000])
            self.stop_event.wait(3)
