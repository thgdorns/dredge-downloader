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

git clone https://github.com/thgdorns/dredge-downloader.git
cd dredge
py -3.11 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
```

`megatools` and `mediafire_rs` are optional: install them into a folder and
point `megatools=` / `mediafire=` in `config/dredge.conf` at it.

A MEGA folder link is saved into a folder named after the online folder
(decrypted from the link's key, e.g. `VR`); links whose folders have the same
name merge into one destination folder.

## Codespaces (Linux without a VM)

Open the repository in a Codespace:

<https://codespaces.new/thgdorns/dredge-downloader>

The devcontainer installs the package, its test dependencies, `yt-dlp` and
`gallery-dl`. Then, in the Codespace terminal:

```bash
pytest
python -m dredge --version
```

The unit tests are cross-platform; the Linux CI job runs the same suite plus a
CLI smoke test. For real downloads, copy the example configs from the Quick
start below and point `dest_default` at a folder in the Codespace.

## Quick start

```powershell
copy config\sites.example.txt      config\sites.txt
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
| `--probe` / `--probe-files` | probe mode: list a copied page, queue its new item (or file) URLs, keep downloads paused for review |
| `--max-downloads N` | concurrent lanes (default 4) |
| `--no-capture` | do not watch the clipboard |
| `--no-update` / `--no-cookies` | skip the engine update / cookie refresh |
| `--strict-generic` | strict mode: a generic-extractor result only counts on hosts in `generic-sites.txt` |
| `--ignore-records` | ignore every record and engine archive (menu 1 -> 1) |
| `--refresh URL` | re-check one post for a newer revision (repeatable; menu 1 -> 9) |
| `--root DIR` | folder holding `config/`, `data/`, `logs/` (default: cwd) |

Press **M** while it runs to open the menu. Inside: records check on/off, retry
failed, retry skipped, skip failed, probe files, lane count, pause/resume,
strict mode, refresh post, and shortcuts to the record files. **Esc** goes back.

The lane count chosen in the menu (item 6) is written back to
`config/dredge.conf` (`max_downloads = N`), so the next start uses it;
`--max-downloads` overrides it for that run only.

Probe mode runs until **Esc** (or Ctrl+C) and leaves the queue paused so the
list can be pruned first; it ends by resuming the downloads. With a piped
stdin it drains `state/clip-inbox.txt` once and exits, which is what the tests
and scripted runs use.

Only **one instance per root** can run: the second one exits with code 3 and
names the running process, so two processes cannot race on `queue.txt` and
`fail.txt` (a pass rewrites the queue while a menu action moves failed links
into it - that race can empty both).

Each finished URL prints the same line the run log gets, with its position in
the queue, the lane, and the file names after the URL:

```
[14:02:11] 20/1277: lane 2: OK|gallery-dl  https://example.com/post/123  -> clip.mp4
[14:02:19] 21/1277: lane 1: FAILED  https://example.com/post/124
```

A failed URL ends with the file name it had reached when the engine got that
far (a partial download, for example); if it never named one, it stays URL-only.

On a terminal the lanes are a live block of one line each, redrawn several
times a second. A line moves through the URL, then the file with the
percentage and speed the engine reports, then the verdict:

```
lane 1: 20/1277  https://example.com/post/123
lane 2: 21/1277  other.mp4  18.0%  4.1 MiB/s
lane 3: idle
lane 4: idle
```

The per-URL lines still scroll above that block (and into the run log). A
long line is cut at the console width with `…`; the complete URL is in the
scrolled line. `--progress-every` only applies when the output is not a
terminal, where the old timed status lines are used instead.

The console keeps the PowerShell colours: cyan for the banner and menu titles,
green for `Finished` and successful menu actions, yellow for warnings and
paused states, red for errors, and grey for background lines (run log, archive
merges, lane status). Colour is only emitted on a terminal, so redirected
output and log files stay plain; `NO_COLOR=1` disables it and `FORCE_COLOR=1`
keeps it for a pipe (for example `... | less -R`).

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
5. A yt-dlp result only counts if a video/audio file is on disk - a page asset
   (`.js`, `.css`) or a preview image in the manifest is not a download. Size is
   not a signal: a short clip is kept. `min=` in `sites.txt` is the opt-in rule
   for sites that split a video into segments.
6. With **strict mode** on (menu 1 -> 8), a yt-dlp tier that falls back to the
   `generic` extractor only counts on hosts listed in `generic-sites.txt`;
   everything else fails with a message naming the host to add.
7. On success the URL, file names and sizes are appended to
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
`engine=`, `author=`, `list=`, `dest=`, `rewrite=`, `yt=`. The example file documents each one.

`yt=ARGS` passes extra yt-dlp arguments for a host (comma-separated), for
example `yt=--write-description`. The iwara rule uses that; the kemono,
pawchive, iwara and fantia sections of `gallery-dl.conf` enable the `metadata`
postprocessor, so the post or video description is saved next to the media as
a sidecar. Neither engine follows links found in a description — the sidecar is
there so you can review them. Sidecars are description records only; they are
never read for duplicate or update decisions.

`engine=yt-dlp` runs the yt-dlp tiers first and keeps gallery-dl as a fallback,
for image pages or URLs yt-dlp cannot take. `engine=gdl` stops the chain early.

### Updating an already-downloaded post

The TSV is the record of what a post downloaded; it is not used to prove a
post's files are current. The file-level state is gallery-dl's archive. For
kemono and pawchive, `gallery-dl.conf` sets:

```json
"archive-format": "{service}_{user}_{id}_{num}_{hash}"
```

so a changed attachment has a new archive key. `--refresh URL` (or menu 1 -> 9):

- bypasses the TSV duplicate check, but uses gallery-dl's normal archive;
- unchanged files keep their old key and are skipped;
- changed/new attachments have new keys and download;
- `skip: "enumerate"` writes a changed file as `name.1.ext` instead of
  overwriting it, and that name is appended to `downloads.tsv`;
- never reads local files or sidecars, so deleting files does not change the
  decision.

Old archive entries predate `{hash}`; the first refresh of an old post therefore
downloads its current files as `.1` once. Normal re-adds of a post already in
the TSV stay skipped; use `--refresh` for the archive check.

fantia's archive key already contains `{file_id}`, so a new file id is detected
natively. `--refresh --ignore-records` bypasses the archive and re-downloads with
`--no-skip` (overwrite), not the incremental check. Use `--refresh` while dredge
is stopped, or menu 1 -> 9 while it is running.

### What counts as a duplicate

- **A single post** is keyed by its canonical URL in `downloads.tsv`. The
  kemono/pawchive twins (`kemono.cr`, `kemono.su`, `kemono.party` and
  `pawchive.pw`) share one key, so re-adding either host is "already
  downloaded"; the row keeps the URL you queued.
- **The TSV is a record first**: it stores the URL and the file names/sizes that
  were written. The queue filter uses it as a secondary duplicate check. It is
  not used to decide whether local files still exist.
- **Gallery-dl's archive is the file-level check**: with the hash-bearing
  `archive-format`, unchanged files are skipped and changed/new files are
  fetched as `.1`.
- **A creator profile** is a listing, not a post: its URL is not archived.
  Re-adding it re-crawls and finds new posts; existing files are skipped by
  gallery-dl's archive.
- **Probe, post level** (`--probe`) queues only posts not already in the TSV,
  including mirror twins.
- **Probe, file level** (`--probe-files`) uses `gallery-dl -g`, which lists
  every file URL regardless of gallery-dl's archive. It is a review list, not a
  new-only list; only a file URL that Dredge itself downloaded and archived is
  skipped.
- **A revised post** is skipped by a normal re-add; `--refresh` is the
  gallery-dl-archive check and writes new/changed files as `.1`.

The `.json` and `.description` sidecars are for descriptions only; they are
never read for duplicate or update decisions.

`author=yt-dlp` makes gallery-dl use yt-dlp's uploader **display name** as the
creator folder. gallery-dl's own metadata only has the handle for some sites
(iwara, for example), which would otherwise name the folder `user29894` instead
of the author's name.

`config/dredge.conf` holds the machine-specific values (destinations, fallback
drives, templates, tool folders). See
[`config/dredge.conf.example`](config/dredge.conf.example).

### Destinations are named, and yours

`dredge.conf` declares roots by name; a site picks one with `dest=NAME`:

```ini
[dest:default]
path = E:\Downloads
layout = creator        ; one folder per uploader

[dest:flat]
path = E:\Downloads\misc
layout = flat           ; files go straight into the root
```

```text
# sites.txt
example.com     dest=flat
```

`destination_default` names the root for sites without `dest=`. Declare as many
destinations as you need - a second drive, music, books - and each can set its
own `tpl`. `dest=` is a routing decision, not a content classification: it says
where a download goes and which folder shape it gets, never what kind of site it
is.

Only destinations on the default destination's drive move to a fallback drive
when it is missing or below `dest_fallback_below_gb`; the drive letter changes,
the tree does not.

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
tests/               unit tests, no network and no clipboard required
config/*.example     configuration templates (real files are gitignored)
docs/                engine-watch (workaround retirement audit) and notes
legacy/              notes from the private PowerShell original
```

## Watching the engines

yt-dlp and gallery-dl change weekly, and some of the code here is a workaround
for a gap that upstream may close (creator-page listing, locale subdomains,
aria2c on DASH fragments, YouTube's tracking cookies). Those are recorded in
`src/dredge/workarounds.json` and audited by:

```powershell
python -m dredge.watch      # KEEP / RETIRE / DUE / UNKNOWN per workaround
```

`RETIRE` means the reason is gone - delete the workaround, not just the entry.
`docs/engine-watch.md` explains the verdicts and the weekly GitHub Action
(`docs/engine-watch.workflow.yml`).

## Status

- [x] queue, archive, fail/offline/skip records
- [x] URL canonicalisation, `id=` identity, mirrors
- [x] `sites.txt` rules, folder templates, `list=` fast expansion
- [x] worker tier chain, junk guard, auto-rename, file-host staging
- [x] lane pool, serial hosts, destination fallback, space guard
- [x] console menu, records check, retry/skip failed
- [x] content-aware `--refresh` for updated posts
- [x] clipboard capture without CopyQ
- [x] probe mode (`--probe`, `--probe-files`, menu 1 -> 5)
- [ ] Linux support beyond the pure-Python parts

## License

MIT - see [LICENSE](LICENSE). The engines listed above keep their own licenses;
dredge only calls their command-line interfaces.
