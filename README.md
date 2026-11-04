# dredge

A queue-and-archive **download orchestrator** for Windows: you paste links, it
decides what to fetch, with which engine, into which folder - and writes down
what it did so nothing is downloaded twice.

It does not download anything by itself. The heavy lifting is done by:

| Engine | Used for | Link |
| --- | --- | --- |
| **yt-dlp** | video sites, creator pages, playlists | [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp) |
| **gallery-dl** | image boards, galleries, fanbox/kemono-style posts | [mikf/gallery-dl](https://github.com/mikf/gallery-dl) |
| **megatools** | MEGA public folders (optional) | [megous/megatools](https://github.com/megous/megatools) |
| **mediafire_rs** | MediaFire folders and files (optional) | [NicKoehler/mediafire_rs](https://github.com/NicKoehler/mediafire_rs) |
| **ffmpeg** | merging video+audio streams | [FFmpeg](https://ffmpeg.org/) |
| **Node.js** | yt-dlp's JavaScript challenges | [nodejs.org](https://nodejs.org/) |

> **This is an amateur, vibe-coded project.**
> I wrote it for my own archive, mostly by describing what I wanted and letting
> an AI write the code, then testing it against real downloads. It is used
> daily on one Windows machine. There is no support, no roadmap promise, and
> the code is honest rather than clever. Expect rough edges, and read a diff
> before you trust it with a 4 TB library. If you want a polished downloader,
> use the engines above directly - they are excellent.

## Why another downloader?

Because the engines are great at *downloading* and bad at *bookkeeping*. Dredge
adds the boring parts:

- **a queue** (`data/queue.txt`) that survives restarts and waits for links;
- **an archive** (`data/downloads.tsv`) of every URL and the files it produced;
- **separate records** for failures (`fail.txt`), dead posts (`offline.txt`) and
  deliberate skips (`skip.txt`) - a link you skip is never retried, ever;
- **per-site knowledge in a text file** (`config/sites.txt`), never in code:
  folder templates, identity rules, extra engine options, which hosts are
  serial, which need a mirror;
- **creator pages expand fast**: a profile or playlist is listed with
  `yt-dlp --flat-playlist` in seconds and its items are queued for the next
  pass, instead of crawling the page one item at a time;
- **unattended**: with an empty queue it waits, watches the clipboard, and
  starts a new pass by itself.

Nothing here names a site. Everything site-specific is a line in
`config/sites.txt`.

## Install

Needs Python 3.11+ and the engines you actually use.

```powershell
winget install yt-dlp.yt-dlp
winget install mikf.gallery-dl
winget install Gyan.FFmpeg
winget install OpenJS.NodeJS

git clone https://github.com/<you>/dredge.git
cd dredge
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

`megatools` and `mediafire_rs` are optional: install them into a folder and
point `megatools=` / `mediafire=` in `config/dredge.conf` at it.

## Quick start

```powershell
copy config\sites.example.txt      config\sites.txt
copy config\pornsites.example.txt  config\pornsites.txt
copy config\mirrors.example.txt    config\mirrors.txt
copy config\filehosts.example.txt  config\filehosts.txt
copy config\generic-sites.example.txt config\generic-sites.txt
copy config\ytdlp.conf.example     config\ytdlp.conf
copy config\ytdlp-generic.conf.example config\ytdlp-generic.conf
copy config\gallery-dl.conf.example config\gallery-dl.conf

# put one URL per line into data\queue.txt, then:
.venv\Scripts\dredge --once --dry-run   # what would happen
.venv\Scripts\dredge                    # stay running, wait for links
```

`data\queue.txt` and `data\downloads.tsv` are created on first start.

## Command line

| Flag | What it does |
| --- | --- |
| *(none)* | stay running: download the queue, then wait for new links |
| `--once` | take the queue once and exit |
| `--dry-run` | `--simulate` only: asks the engines, writes no records |
| `--menu` | open the console menu at startup |
| `--retry-failed` | try URLs already in `fail.txt` / `offline.txt` again |
| `--max-downloads N` | concurrent lanes (default 4) |
| `--no-capture` | do not watch the clipboard |
| `--no-update` / `--no-cookies` | skip the engine update / cookie refresh |
| `--strict-generic` | only let the generic yt-dlp tier run on hosts in `generic-sites.txt` |
| `--ignore-records` | ignore every record and engine archive (menu 1 -> 1) |
| `--root DIR` | folder holding `config/`, `data/`, `logs/` (default: cwd) |

Press **M** while it runs to open the menu. Inside: records check on/off, retry
failed, skip failed, lane count, pause/resume, and shortcuts to the record
files. **Esc** goes back.

## How a URL is handled

1. Tracking arguments are stripped and the URL is canonicalised
   (`example.com/post/123`), which is the archive key.
2. If it is already in `downloads.tsv`, `skip.txt`, `fail.txt` or
   `offline.txt`, it is dropped and counted - unless records check is off.
3. If a site rule says the URL is a creator page, yt-dlp lists it flat and the
   item URLs are staged for the next pass. Duplicates are dropped there, so a
   595-item profile costs one request, not 595.
4. Otherwise the tier chain runs: **gallery-dl** first, then **yt-dlp** with its
   dedicated extractor, then yt-dlp again with the native downloader, then the
   all-extractor pass. The next tier only runs when the previous one failed or
   produced nothing. `engine=gdl` in `sites.txt` stops the chain early.
5. Files smaller than `min=` (or than a video-sized threshold) are treated as
   error pages and deleted, and the URL is recorded as failed.
6. On success the URL, file names and sizes are appended to
   `data/state/fold-pending.tsv` and merged into `downloads.tsv` in batches -
   the 25 MB archive is never rewritten for a single download.

## Configuration

`config/sites.txt` is the interesting file. One line per host, every key
optional:

```
example.com   keep=all min=0 id=[0-9]{6,} native list=^/(?:profile/[^/]+|videos)(?:/|$)
```

`keep=`, `min=`, `id=`, `also=`, `serial`, `native`, `probe=`, `gdl=`, `key=`,
`playlist=`, `postid=`, `posturl=`, `itemurl=`, `tpl=`, `folder=`, `cookiedrop=`,
`engine=`, `list=`. The example file documents each one.

`config/dredge.conf` holds the few machine-specific values (destinations,
fallback drives, templates, tool folders). See
[`config/dredge.conf.example`](config/dredge.conf.example).

## Cookies and login

Many sites need an account. Dredge asks **gallery-dl** to export the cookies of
a browser you are already logged into, and passes the resulting
`config/cookies.txt` to every engine:

```ini
# config/dredge.conf
cookies_browser = floorp     ; firefox, edge, chrome, ... ('' = never refresh)
```

Firefox-family browsers can be read while open; Chromium-based ones lock their
cookie database, so close them first. If the export fails, the existing
`cookies.txt` is kept untouched. Sites that choke on too many cookies (YouTube
is the classic) list the ones to drop: `cookiedrop=ST-*` in `sites.txt`.

## Privacy

The repository contains **code and example configs only**. `.gitignore` keeps
out `config/cookies.txt`, `config/netrc`, the real config files, `data/` and
`logs/` - those hold session cookies and personal URLs. Never paste a cookie
into an issue.

## Layout

```
src/dredge/          the package: cli, worker, orchestrator, plan, archive,
                     records, urls, sites, destinations, engines, menu, tui
tests/               64 tests, no network and no clipboard required
config/*.example     configuration templates (real files are gitignored)
legacy/              notes from the private PowerShell original
```

## Status

- [x] queue, archive, fail/offline/skip records
- [x] URL canonicalisation, `id=` identity, mirrors
- [x] `sites.txt` rules, folder templates, `list=` fast expansion
- [x] worker tier chain, junk guard, auto-rename, file-host staging
- [x] lane pool, serial hosts, destination fallback, space guard
- [x] console menu, records check, retry/skip failed
- [x] clipboard capture without CopyQ
- [ ] probe mode (`--probe`, `--probe-files`) - only in the private original
- [ ] Linux support beyond the pure-Python parts

## License

MIT - see [LICENSE](LICENSE). The engines listed above keep their own licenses;
dredge only calls their command-line interfaces.
