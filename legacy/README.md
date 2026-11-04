# legacy - the PowerShell original

Dredge started as a single PowerShell 5.1 script (`dredge.ps1`, ~3,000 lines)
that ran unattended on one Windows machine for months. The Python package in
`src/` is a port of it.

The script itself is **not published here**: it is full of machine-specific
defaults, a personal library layout and operational notes about individual
sites, which is exactly the kind of thing this repository keeps out. What it
did teach the port is recorded in the code comments instead:

- engine output loses non-ASCII characters, so the real path must come from
  `--print-to-file` (UTF-8), never from stdout;
- a download archive keyed by URL alone cannot notice that a file was deleted;
  the junk guard has to remove the entry it deletes as well;
- a finished row must be appended to a pending file and merged in batches -
  rewriting a 25 MB TSV for every download is minutes of pure I/O;
- staged URLs must go to `state/queue-requeue.txt`, not straight into
  `queue.txt`, because a pass rewrites that file when it ends;
- a drive fallback that only changes the drive letter keeps two libraries
  mergeable, and a missing drive is a normal condition, not an error.

Those behaviours are covered by the tests in `tests/`, so a change that breaks
one of them fails before it reaches an engine.
