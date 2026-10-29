"""Local Codex inbox, hosted alongside the existing Daily Paper Reader."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import signal
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from codex_service import ReaderService
from codex_processor import stop_active_process
from local_debug_server import Handler

ROOT = Path(__file__).resolve().parents[1]


class CodexHandler(Handler):
    service: ReaderService
    csrf_token: str

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        SimpleHTTPRequestHandler.end_headers(self)

    def trusted_request(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return host in allowed and (not origin or origin == "http://" + host)

    def do_OPTIONS(self):
        self._json({"ok": False, "error": "Cross-origin access is disabled"}, status=403)

    def do_GET(self):
        if not self.trusted_request():
            return self._json({"ok": False, "error": "Local same-origin access required"}, status=403)
        parsed = urlsplit(self.path)
        if parsed.path == "/api/codex/state":
            query = parse_qs(parsed.query)
            try:
                page = max(1, int(query.get("page", ["1"])[0]))
            except ValueError:
                return self._json({"ok": False, "error": "Invalid page"}, status=400)
            store = self.service.store
            data = store.list(query.get("status", [""])[0], query.get("search", [""])[0], page)
            for paper in data["papers"]:
                metadata = json.loads(paper.pop("metadata"))
                paper["source_kind"] = metadata.get("source_kind", "")
                paper["analysis_scope"] = metadata.get("analysis_scope", "")
                paper["matched_keywords"] = metadata.get("matched_keywords", [])
            return self._json({"ok": True, **data, "config": store.config(),
                "csrf": self.csrf_token, "fetching": self.service.fetching,
                "last_fetch": store.setting("last_fetch"), "worker_error": store.setting("worker_error"),
                "codex_available": bool(shutil.which("codex")), "worker_alive": bool(self.service.worker and self.service.worker.is_alive())})
        if parsed.path.startswith("/api/codex/log/"):
            paper_id = parsed.path.rsplit("/", 1)[-1]
            if not self.service.store.get(paper_id):
                return self._json({"ok": False, "error": "Unknown task"}, status=404)
            path = self.service.store.directory / "papers" / paper_id / "codex-events.jsonl"
            log = ""
            if path.exists():
                with path.open("rb") as handle:
                    handle.seek(max(0, path.stat().st_size - 20000))
                    log = handle.read().decode("utf-8", errors="replace")
            return self._json({"ok": True, "log": log})
        if parsed.path.startswith("/api/"):
            return super().do_GET()
        path = unquote(parsed.path)
        public = path in {"/", "/index.html", "/config.yaml", "/favicon.ico"} or any(path.startswith(p) for p in ["/app/", "/docs/", "/others/"])
        resolved = (ROOT / path.lstrip("/")).resolve()
        if not public or not resolved.is_relative_to(ROOT) or any(part.startswith(".") for part in Path(path).parts if part not in {"/"}):
            return self._json({"ok": False, "error": "Not a public reader asset"}, status=404)
        return super().do_GET()

    def do_POST(self):
        if not self.trusted_request():
            return self._json({"ok": False, "error": "Local same-origin access required"}, status=403)
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/codex/"):
            if "application/json" not in self.headers.get("Content-Type", ""):
                return self._json({"ok": False, "error": "JSON required"}, status=400)
            return super().do_POST()
        if self.headers.get("X-DPR-Local") != self.csrf_token:
            return self._json({"ok": False, "error": "Refresh this local page before submitting"}, status=403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 150000:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            if parsed.path == "/api/codex/import":
                result = self.service.import_links(payload.get("text", ""), payload.get("depth", "quick"))
            elif parsed.path == "/api/codex/fetch":
                self.service.fetch_async()
                result = {"started": True}
            elif parsed.path == "/api/codex/action":
                result = {"changed": self.service.store.action(payload.get("ids"), payload.get("action"), payload.get("depth", "quick"))}
            elif parsed.path == "/api/codex/config":
                result = {"config": self.service.store.save_config(payload)}
            else:
                return self._json({"ok": False, "error": "Unknown endpoint"}, status=404)
            return self._json({"ok": True, **result})
        except (ValueError, TypeError, KeyError) as exc:
            return self._json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, status=500)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8568)
    args = parser.parse_args()
    service = ReaderService(ROOT)
    CodexHandler.service = service
    CodexHandler.csrf_token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), CodexHandler)
    service.start()
    def stop(signum, frame):
        service.stop_event.set()
        stop_active_process()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"Codex paper inbox: http://127.0.0.1:{args.port}/app/codex-reader.html", flush=True)
    try:
        server.serve_forever()
    finally:
        service.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
