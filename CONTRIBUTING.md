# Contributing

Thanks for helping. A few ground rules keep the project safe and reviewable.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
ruff check .
pytest
```

Run `ruff check .` and `pytest` before pushing: the suite is offline, so it
takes a fraction of a second.

## What to contribute

- **Bug reports**: the URL shape (redact the id if needed), the engine
  (yt-dlp/gallery-dl/...), its output and what dredge recorded. A failing test
  is even better.
- **New site knowledge**: a `sites.txt` example line plus a test for the URL
  shape; never hard-code a host in Python.
- **Porting**: the PowerShell implementation in `legacy/` is the reference.
  Port pure logic first (URLs, rules, records, archive) with golden tests
  comparing both implementations.

## Rules

- No secrets: cookies, `netrc`, tokens, personal URLs and logs never go into a
  commit. `.gitignore` covers the usual paths, but check `git diff` first.
- No downloaded media in the repository.
- Keep functions pure and testable where possible; I/O at the edges.
- Match the existing style: type hints, `from __future__ import annotations`,
  no new runtime dependencies without a reason.
