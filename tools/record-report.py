r"""Render fail.txt and offline.txt as HTML, grouped by domain.

A thin wrapper around the installed ``dredge-report`` command, kept in tools/
so it sits next to the other record utilities.

    py tools\record-report.py --root D:\Warez\dredge
    cd D:\Warez\dredge
    py tools\record-report.py

Output defaults to ``<root>\reports\fail.html`` and ``offline.html``; use
``--out <folder>`` to write them elsewhere.
"""

from __future__ import annotations

import sys

from dredge.report import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
