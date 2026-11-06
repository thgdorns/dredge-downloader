r"""Render fail.txt and offline.txt as HTML, grouped by domain.

A thin wrapper around the installed ``dredge-report`` command, kept in tools/
so it sits next to the other record utilities.

Run it from anywhere; it switches to the dredge root that contains this
script, so the defaults and printed paths stay relative:

    py tools\record-report.py
    py tools\record-report.py --out reports\2026-11

``--root`` and ``--out`` are resolved from the dredge root. Output defaults to
``reports\fail.html`` and ``reports\offline.html``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dredge.report import main

ROOT = Path(__file__).resolve().parent.parent


def _run(argv: list[str]) -> int:
    """Run with the dredge root as the working directory."""
    os.chdir(ROOT)
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(_run(sys.argv[1:]))
