# Daily Paper Reader: local Codex fork

This fork adds a local paper inbox to the upstream reader. The upstream
DeepSeek/GitHub Actions pipeline remains available separately.

## Start

Requirements: Python 3.10+, the dependencies in `requirements.txt`, and a working
`codex login`. Codex uses the CLI's configured account and model. No DeepSeek key,
Supabase account, GitHub token in the browser, or hosted Codex credentials are
required for this local workflow.

```sh
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python src/codex_reader_server.py --port 8568
```

Open <http://127.0.0.1:8568/app/codex-reader.html>.

On macOS, `scripts/start_codex_reader.command` installs a user LaunchAgent and
opens the inbox. It starts again after login. `scripts/stop_codex_reader.command`
stops the service and removes automatic startup. The computer must be awake and
logged in for local processing; a missed daily run is caught up when the service
next runs after the configured local hour. Nothing is deployed to GitHub Pages.

## Workflow

- Paste up to 100 arXiv links/IDs, Hugging Face paper links, DOI links, public PDF
  links or paper landing pages. Choose quick or deep reading, then import.
- Manual imports enter the queue. Repeated normalized links reuse their inbox
  entry. Explicit arXiv versions are preserved separately; unversioned URLs mean
  the latest version at processing time. DOI/arXiv cross-provider identity
  matching is not claimed.
- Discovery uses the public Hugging Face Daily Papers feed and arXiv Atom API.
  Keywords match title/abstract; arXiv also applies a submission-date window.
  This is bounded keyword retrieval, not exhaustive search or semantic ranking.
  Empty keywords retain the HF feed and use `cs.AI` for arXiv.
- Settings control sources, keywords, daily hour, result limit and automatic
  processing quota (default 3 papers/day, quick reading). Set the quota to 0 to
  collect candidates only. Manual imports and explicit queue actions do not use
  the automatic discovery quota.
- One Codex task runs at a time. Pause stops new processing after the current
  paper. It does not stop discovery; disable daily discovery separately.
  Failed processing automatically retries once after 60 seconds, then remains
  failed with a local log and an explicit retry action.
- Finished notes open in the upstream reader in local guest mode automatically.
  The Codex inbox does not require its password/key setup.
  Existing browser chat still uses the upstream model configuration; this fork
  does not connect the chat box to Codex.

## CLI import

```sh
.venv/bin/python scripts/codex_reader.py import --file links.txt
.venv/bin/python scripts/codex_reader.py import --depth deep https://arxiv.org/abs/1706.03762
.venv/bin/python scripts/codex_reader.py status
.venv/bin/python scripts/codex_reader.py fetch
```

Imports can be submitted while the service is stopped; they persist until it
starts. The fetch command uses the running local server on port 8568.

## Evidence and storage

Queue, configuration, provenance, source PDFs and task logs live in
`.codex-reader/`. Native Markdown, full text, PDF copies and page snapshots live
in `docs/codex/`. Both directories are ignored by Git. Back up both directories
together with `docs/_sidebar.md`; stop the service first for a consistent SQLite
backup. Generated reading pages are not pushed to the public fork.

Codex runs read-only, with shell execution and agent spawning disabled. It
returns structured JSON; the publisher, not the model, writes the reading
library. Quotations must match their physical PDF page before a task completes.
Notes record the source hash, extraction date, reading depth and coverage.
Landing-page fallbacks and context truncation are labeled partial. Page images
are original PDF page screenshots, not claimed to be cropped figures.
Quoted evidence validation does not independently verify every model claim.
Code/data URLs are only paper-mentioned leads, not a reproduction audit.

The worker supplies up to 240,000 characters and 500 PDF pages. Download and
Codex execution are bounded. Scanned PDFs without extractable text fail with
an OCR message. Existing manual-note blocks and unknown frontmatter fields are
preserved when a task is regenerated.

`DPR_CODEX_BIN` can select the CLI executable; `DPR_CODEX_MODEL` can explicitly
override its configured model. The macOS service uses the persistent native
`~/.codex` login; `DPR_CODEX_HOME` selects another persistent configuration.
It does not copy a development session's temporary gateway credential.
Account rate limits and usage still apply.
Authentication stays in the user's existing Codex configuration. Do not add
it to GitHub Actions or commit it to this repository.

## Verification

```sh
.venv/bin/python -m pytest tests/test_codex_reader.py
node --check app/codex-reader.js
```

The inbox is bound to loopback, verifies Host/Origin and a same-origin request
token, and excludes private runtime/configuration directories from static
hosting. It is for one local user, not a public multi-user service.

Lucide icons: `app/vendor/lucide-0.468.0.min.js`, upstream
<https://unpkg.com/lucide@0.468.0/dist/umd/lucide.min.js> (ISC license).
Official Codex automation reference:
<https://developers.openai.com/codex/noninteractive/>.
