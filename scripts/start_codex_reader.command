#!/bin/zsh
set -eu
DPR_CODEX_ROOT="${0:A:h:h}"
cd "$DPR_CODEX_ROOT"
"$DPR_CODEX_ROOT/.venv/bin/python" scripts/launch_codex_reader.py start
open 'http://127.0.0.1:8568/app/codex-reader.html'
