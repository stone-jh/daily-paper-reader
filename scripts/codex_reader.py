#!/usr/bin/env python3
"""CLI for bulk imports and inspection of the local Codex inbox."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codex_service import ReaderService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import")
    importer.add_argument("links", nargs="*")
    importer.add_argument("--file", type=Path)
    importer.add_argument("--depth", choices=["quick", "deep"], default="quick")
    commands.add_parser("status")
    commands.add_parser("fetch")
    args = parser.parse_args()
    service = ReaderService(ROOT)
    if args.command == "import":
        text = args.file.read_text(encoding="utf-8") if args.file else "\n".join(args.links)
        result = service.import_links(text, args.depth)
    elif args.command == "fetch":
        # Use the running server so quotas and discovery locks have one owner.
        import urllib.request
        base = "http://127.0.0.1:8568"
        with urllib.request.urlopen(base + "/api/codex/state") as response:
            state = json.load(response)
        request = urllib.request.Request(base + "/api/codex/fetch", data=b"{}", headers={
            "Content-Type": "application/json", "X-DPR-Local": state["csrf"]})
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
    else:
        result = service.store.list()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
