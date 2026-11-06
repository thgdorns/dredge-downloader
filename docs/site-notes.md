# Site notes

`config/sites.txt` says *what* to do per site. This page says *why* - the site
behaviour each rule works around, so a rule can be judged (and deleted) without
re-discovering the evidence. Dates and versions are where the claim was last
checked.

Rule keys referenced here: `engine=` (the only engine for the host, no
cascade), `native` (yt-dlp's own downloader instead of aria2c), `list=` (the
URL lists flat with `yt-dlp --flat-playlist`), `yt=` (extra yt-dlp arguments),
`tpl=` (output template), `gdl=` (extra gallery-dl options), `folder=` (the
regex that names a collection folder), `rewrite=` (URL shape fixed before the
engines see it).

## Kemono family - kemono.cr / kemono.su / kemono.party / coomer.* / pawchive.pw

- **Partial mirrors.** pawchive.pw carries the original files; the kemono shards
  (`n1`-`n4.kemono.cr`) stopped answering TCP 443, and `img.kemono.cr` only
  serves thumbnails. So a kemono link is fetched from pawchive and the twin is
  never probed (`mirrors.txt`, `probe=` on coomer for its own shards).
- **A post can exist while its file does not.** The API returns the post (title,
  description, preview) and gallery-dl then warns
  `Incomplete/Missing file import ('pending')`; the file URL answers 404 until
  the mirror imports it. That is *temporary* - it must land in `fail.txt`, never
  in `offline.txt` (the "pending import" phrase is part of `RATE_RE`).
- **The archive key is the file, not the post**: `patreon_<user>_<post>_<n>`.
  Post-level rows come from `postid=`/`posturl=` splitting a crawl into one row
  per post, which is what makes a re-queue of a creator page add only new posts.
- **kemono.cr answers 403** to a plain HTTP client (Cloudflare); gallery-dl with
  the exported cookies and its impersonation gets through.
- Creator pages expand into per-post URLs and are crawled one at a time
  (`serial`), because the mirror rate-limits parallel crawls.

## iwara.tv

- **Pinned to yt-dlp** (`engine=yt-dlp`). gallery-dl's iwara extractor exists but
  yt-dlp's format list is better (see below), and the pin also stops the
  gallery-dl → yt-dlp chain for private videos, where every engine gets the same
  403.
- **The CDN drops connections under parallel load.** `hime.iwara.tv` /
  `mikoto.iwara.tv` answer in 200-400 ms when probed alone, but with 4 lanes x 16
  aria2c connections, single connections time out and aria2c exits 1. The rule
  therefore uses `native`: yt-dlp's own downloader, one connection per file.
  (`-x4` via `yt=` was tried first and still churned.)
- Formats are `preview`, `360`, `540`, `Source`; `Source` is the highest and the
  same file the CDN serves to the browser. No audio-only streams, so
  `-f b` and `-f bv*+ba` pick the same thing.
- `--write-description` (`yt=`) keeps the post text next to the video; that text
  is often where the Mega/Drive links for the full version live.
- Profile and playlist pages are `list=`-expanded (seconds) instead of crawled.
- A private video answers the same 403 from every engine - a `FAILED`, not a
  reason to try three more engines.

## iwara.ai

- **gallery-dl only** (`engine=gdl`). yt-dlp's Iwara extractor matches
  `iwara.tv`/`ecchi.iwara.tv` only and answers `Unsupported URL` for this host -
  checked against yt-dlp 2026.11.04 and against master's `_VALID_URL`.
- **It is not a mirror of iwara.tv**: the same video id on iwara.tv makes the API
  answer `errors.differentSite`. A rewrite in `mirrors.txt` would break every
  link.
- Same backend and the same `acheron`/`mikoto`/`hime` CDN as iwara.tv, so the
  same 403/private behaviour applies to individual videos.
- `workarounds.json` → `yt-iwara-ai` watches for yt-dlp learning the host; the
  day it does, the pin becomes `engine=yt-dlp`.

## ecchi.iwara.tv

- A **separate catalogue**, not a mirror of iwara.tv: video pages are
  `/videos/<id>/<slug>` (plural) and gallery-dl has no extractor for it.
- `rewrite=` turns it into `iwara.tv/video/<id>` so yt-dlp's extractor handles
  it, and *no* `engine=` pin is set on purpose - the rewrite is a URL fix, not an
  engine choice.

## MEGA - mega.nz / mega.io

- `engine=megadl` (megatools). Public links need no login.
- A `/file/<id>#<key>` link is one file and stays flat in the destination.
  A `/folder/<id>#<key>` link names nothing in the URL, so the folder's own name
  is decrypted from the link's key (the id is the fallback) and used as the
  destination folder.
- **Transfer quota is per IP and resets**: megatools sleeps for minutes per
  chunk after a 509, which would hold a lane. The worker stops the process and
  records `FAILED`, so a later run retries. Partial data stays in
  `.megatmp.<id>` files and resumes.

## MediaFire - mediafire.com

- `engine=mdrs` (mediafire_rs). `/folder/<key>/<Folder_Name>` carries the
  collection name (`folder=` regex); `/file/<key>` is a single file and stays
  flat. Without the regex the generic "a path segment that reads as a name" rule
  skips names that look like filenames (`My.Album.Name` would lose its folder).

## Google Drive - drive.google.com / docs.google.com

- `engine=gdown`. `/file/d/<id>`, `/open?id=<id>` and `?id=<id>` all work;
  `-c` resumes a partial file and skips a finished one.
- A folder link writes into `<dest>/<folder id>`: the folder name is not in the
  link (same fallback MEGA uses).
- Two refusals are *temporary*: `Cannot retrieve the public link` (not shared
  any more, or the per-file access limit) and the "too many users have viewed"
  quota. Both are a `FAILED` with a lane note, retried later.
- gdown rewrites the cookie file it is given, so it gets a throwaway copy of
  `config/cookies.txt`.

## YouTube - youtube.com / youtu.be

- `native`: aria2c opens `-x` connections *per DASH fragment*, so a 900 MB video
  turns into thousands of connections and is slower than yt-dlp's downloader.
- `key=v` is the identity: the video id lives in the query, and without it
  `?t=30&list=...` variants looked like new videos. `youtu.be/<id>` is the same
  video under another name (`also=`).
- YouTube's click-tracking cookies (`ST-*`) are dropped on export
  (`cookiedrop=`).
- JS challenges need node (or deno); without one, some formats are missing.
- `--write-description` (`yt=`) stores the description next to the video.

## SpankBang

- The extractor reports the video id as the title (`41aiv`), so files were named
  after the id. `parse-metadata` in `ytdlp.conf` copies the real title out of the
  URL slug (and turns `+` back into spaces).

## The generic tier (last yt-dlp pass)

- Not a default: only hosts listed in `generic-sites.txt` may reach it
  (`strict`), because a generic pass happily saves the page itself.
- `-f b/bv*+ba`: prefer a pre-merged file, merge best video+audio as fallback
  (some pages have no pre-merged format at all).

## Where this knowledge can be shared

- **Here.** `config/sites.txt` carries the rules and their comments; this page
  carries the reasons; `src/dredge/workarounds.json` carries the ones that
  should expire and is audited by `python -m dredge.watch`.
- **gallery-dl.** Its *Supported Sites* list is generated from the extractors,
  so a host only appears there once an extractor exists (a PR to
  `gallery_dl/extractor/`). Their wiki has a **Custom Site Support** page for
  sites that are clones of an existing extractor - a JSON `extractor` block -
  which is the cheap way to publish "this site behaves exactly like that one".
  Site *behaviour* (partial mirrors, 403s, pending imports) belongs in an issue.
- **yt-dlp.** Same shape: a wiki *Supported sites* page, and the sanctioned way
  to publish support for a host upstream does not cover (iwara.ai) is a
  **plugin** package - code under `yt_dlp_plugins/extractor/`, published with
  the `yt-dlp-plugins` topic - rather than a patch nobody asked for.
- Neither project documents "this mirror is incomplete" anywhere central; those
  findings belong in an issue comment (with the URL, the message, the date) and
  in a page like this one.
