# Watching the engines

Every workaround in this project exists for a reason that can expire. Two of
them are already upstream:

- creator pages are expanded with `yt-dlp --flat-playlist` because gallery-dl
  has no list-only mode ([mikf/gallery-dl#9859](https://github.com/mikf/gallery-dl/issues/9859));
- locale tube hosts are rewritten to the bare domain because the site
  extractors do not match `br.`/`it.`/`es.` prefixes
  ([yt-dlp/yt-dlp#17995](https://github.com/yt-dlp/yt-dlp/issues/17995)).

If either lands, the workaround should be deleted - not kept forever because
nobody remembered it was temporary.

`dredge-watch` checks the registry in
[`src/dredge/workarounds.json`](../src/dredge/workarounds.json) and answers one
question per entry: **is the reason still there?**

```powershell
python -m dredge.watch              # markdown report, exit 1 if action is due
python -m dredge.watch --json       # machine-readable
python -m dredge.watch --offline    # skip network checks
```

## Verdicts

| verdict | meaning | what to do |
| --- | --- | --- |
| `KEEP` | the reason still applies | nothing |
| `RETIRE` | the engine now does it / the issue is closed | delete the workaround and its registry entry, run the tests |
| `DUE` | a manual re-check has expired | re-measure, update `last_verified` or retire |
| `UNKNOWN` | engine not installed, network blocked, check failed | run it where the engines are installed |

## Check kinds

| kind | how it decides |
| --- | --- |
| `help_flag` | runs `<engine> --help` and looks for a flag (e.g. `--list`). Found -> `RETIRE`. |
| `extractor` | runs `yt-dlp --simulate --print "%(extractor)s" <url>`; a different extractor than `expect_not` -> `RETIRE`. |
| `issue` | reads the GitHub issue state; closed -> `RETIRE`. |
| `manual` | `DUE` once `recheck_after_days` have passed since `last_verified`. |

A `manual` check is the honest option for things that need a measurement (the
aria2c-vs-native timing) or a behaviour we cannot probe without credentials
(the `ST-*` cookie overflow). Those carry a date so they come back around
instead of being forgotten.

## Adding an entry

```json
{
  "id": "something-short",
  "title": "what the workaround does",
  "engine": "yt-dlp",
  "added": "2026-11-04",
  "where": ["src/dredge/worker.py:download"],
  "retire_when": "the engine does it itself",
  "issue": "https://github.com/.../issues/1",
  "checks": [
    {"kind": "issue", "url": "https://github.com/.../issues/1"}
  ]
}
```

Rules of thumb:

- one workaround per entry, with the file and function that implements it;
- `retire_when` must be falsifiable: "gallery-dl has a flag", not "gallery-dl
  gets better";
- prefer an automatic check; use `manual` only when there is nothing to probe;
- when an entry retires, delete the workaround **and** its `where` references,
  then update the docs that mention it.

## Upstream reports

`reports` in the registry lists the issues we filed. They are informational: the
weekly report shows open/closed for each, and a workaround whose `issue` closes
flips to `RETIRE` on its own. Reports without a workaround behind them (the
header-only HTML detection, the aria2c and cookie-size docs notes) are listed so
they do not get forgotten.

## Scheduled runs

`docs/engine-watch.workflow.yml` is the GitHub Actions workflow. Every Monday
it installs the latest engines, runs the audit and keeps **one open issue**
(`engine-watch: current report`) whose body is replaced with the fresh report -
so the version table and verdicts are always current without notifying anyone.

A comment is added only when the list of `RETIRE`/`DUE` items changes, so an
unfixed item does not spam the thread; the issue body stays the source of
truth between runs.

Installing it needs the `workflow` scope, which the login used for the rest of
this repository does not have:

```powershell
gh auth refresh -h github.com -s workflow
mkdir .github\workflows -Force
copy docs\engine-watch.workflow.yml .github\workflows\engine-watch.yml
git add .github && git commit -m "ci: weekly engine-watch" && git push
```

Until then, run it by hand after updating the engines - the audit prints the
same report:

```powershell
winget upgrade yt-dlp.yt-dlp mikf.gallery-dl
python -m dredge.watch
```
