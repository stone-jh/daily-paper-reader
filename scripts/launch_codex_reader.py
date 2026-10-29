#!/usr/bin/env python3
"""Install/start/stop the macOS user service for the local Codex reader."""

import argparse
import os
import plistlib
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--port", type=int, default=8568)
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("On Linux, run .venv/bin/python src/codex_reader_server.py --port 8568")
    label = f"local.daily-paper-reader.codex.{args.port}"
    domain = f"gui/{os.getuid()}"
    plist = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    if args.action == "stop":
        subprocess.run(["launchctl", "bootout", domain + "/" + label], check=False)
        plist.unlink(missing_ok=True)
        print("Codex reader stopped; automatic startup removed")
        return
    url = f"http://127.0.0.1:{args.port}/api/codex/state"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=2) as response:
            if response.status == 200:
                print(f"Already running: http://127.0.0.1:{args.port}/app/codex-reader.html")
                return
    except Exception:
        pass
    runtime = ROOT / ".codex-reader"
    runtime.mkdir(exist_ok=True)
    environment = {key: os.environ[key] for key in ["PATH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "DPR_CODEX_BIN", "DPR_CODEX_MODEL"] if key in os.environ}
    # A gateway-launched development session may have a temporary CODEX_HOME
    # whose credential only exists in that session. Use the persistent CLI login.
    environment["CODEX_HOME"] = os.environ.get("DPR_CODEX_HOME", str(Path.home() / ".codex"))
    environment["NO_PROXY"] = "localhost,127.0.0.1"
    payload = {"Label": label, "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-u", str(ROOT / "src/codex_reader_server.py"), "--port", str(args.port)],
               "WorkingDirectory": str(ROOT), "EnvironmentVariables": environment,
               "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
               "StandardOutPath": str(runtime / "server.log"), "StandardErrorPath": str(runtime / "server.log")}
    plist.parent.mkdir(parents=True, exist_ok=True)
    with plist.open("wb") as handle:
        plistlib.dump(payload, handle)
    plist.chmod(0o600)
    subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    for _ in range(40):
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    print(f"Codex paper inbox: http://127.0.0.1:{args.port}/app/codex-reader.html")
                    return
        except Exception:
            time.sleep(.25)
    raise SystemExit("Service did not become healthy; inspect .codex-reader/server.log")


if __name__ == "__main__":
    main()
