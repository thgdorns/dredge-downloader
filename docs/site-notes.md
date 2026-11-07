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
- **A private video is not offline.** It answers `Private video. Login if you
  have permissions to watch` or `Video may need login to view`; it can open
  later, so it belongs in `skip.txt` (retry skipped when the account changes),
  not in `offline.txt`. It also answers the same 403 from every engine - one
  `FAILED`, not a reason to try three more engines. On 2026-11-06, 43
  `ecchi.iwara.tv` links in `offline.txt` were re-checked: 43 were
  private/login-gated and moved to `skip.txt`; `wy2rbfkoycrmr803` was a real 404
  and stayed offline.

## iwara.ai

- **gallery-dl only** (`engine=gdl`). yt-dlp's Iwara extractor matches
  `iwara.tv`/`ecchi.iwara.tv` only and answers `Unsupported URL` for this host -
  checked against yt-dlp 2026.11.04 and against master's `_VALID_URL`.
- **Folder names use the display name, not the handle.** gallery-dl exposes
  `user.name` as the handle (`user2938287`) and `user.nick` as the display name
  (`abc的二分`, `テーヘン`). The `iwara.directory` template in
  `config/gallery-dl.conf` is `{user[nick]|user[name]|username}`, so new
  downloads land under the display name; `ratzy` happens to have both the same.
  Checked 2026-11-06; existing handle-named folders were left in place.
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
  records `FAILED`, so a later run retries.
- **One quota answer parks the whole host in that pass.** The quota belongs to
  the IP, so the remaining MEGA links cannot succeed either; the pass records
  the failing link, then skips the host until `quota_block_minutes` (default
  60) have passed, saying so once. The parked links stay in queue.txt rather
  than being written to fail.txt one by one, and the pass idles instead of
  re-dispatching them until the window ends. `EBLOCKED` counts as the same
  thing. Partial data stays in
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
- **Short links are resolved before the rule lookup.** `config/shorteners.txt`
  lists goo.gl, bit.ly, ...; `resolve_short_url` follows the HTTP redirect first.
  `goo.gl/jkGHi0` becomes `drive.google.com/drive/folders/...`, so the drive rule
  selects gdown. The engines cannot do this themselves: gdown reads `jkGHi0` as
  a folder id and 404s, yt-dlp's generic pass errors, gallery-dl says
  `Unsupported URL`.
- gdown prints the filenames before fetching; `engines.run` gives child
  processes `PYTHONIOENCODING=utf-8`, so a Drive folder with Japanese names does
  not die on the Windows ANSI code page.
- Two refusals are *temporary*: `Cannot retrieve the public link` (not shared
  any more, or the per-file access limit) and the "too many users have viewed"
  quota. Both are a `FAILED` with a lane note, retried later.
- gdown rewrites the cookie file it is given, so it gets a throwaway copy of
  `config/cookies.txt`.
- **Anonymous first, cookies only as a fallback.** The engine is for public
  folders. Asking Drive as the logged-in account turns "public but not shared
  with me" into an empty listing: exit 0, `Download completed`, nothing on disk,
  and the pass records a bare `FAILED` with no reason (2026-11-07: 34 folders in
  one pass, one of them 1.16 GB). So the anonymous run is the default; the jar is
  used only when that fails (an error, or a folder listing with no files), which
  is the one case it helps with, a private link.

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

## Nicovideo - nicovideo.jp / nico.ms

- **yt-dlp is the only engine that has it.** gallery-dl has no niconico video
  extractor - only `seiga.nicovideo.jp` images (checked gallery-dl 1.32.19,
  2026-11-06). The rule is pinned `engine=yt-dlp`, so gallery-dl is not tried.
  yt-dlp covers `niconico`, `niconico:playlist`, `niconico:series`,
  `niconico:channel`, `niconico:live` and `NiconicoUser`.
- `/watch/<id>`, `/shorts/<id>` and `sp.nicovideo.jp` all match the extractor;
  `nico.ms/<id>` is the shortener for the same video, hence `also=nico.ms`.
  `id=[a-z]{2}\d+` folds both shapes into the same archive key, so pasting the
  short form after the long form is not a second download.
- **Descriptions.** `--write-description` (`yt=`) writes the post text as plain
  UTF-8 next to the video: `<uploader>/<title>.description` (checked with yt-dlp
  2026.11.04 on `sm9`). Playlist metadata is written by default
  (`--write-playlist-metafiles` is on), so a mylist or series also leaves its own
  `.description` through the `pl_description` template - same extension, no
  separate rule. `tools/url_scan.py` already reads `.description` by default, so
  there was nothing to add there.
- **Economy mode is gone - do not add a peak-hour block.** The time-of-day
  quality cap for free and logged-out users was abolished on 2024-03-01 15:00
  JST ([official notice](https://blog.nicovideo.jp/niconews/215661.html)).
  Since then free/logged-out users get up to 720p at any hour and premium gets
  1080p; the old windows (weekdays 18:00-26:00, weekends/holidays 12:00-26:00)
  no longer exist. nndownload's README still says "12 PM - 2 AM JST" and is
  stale.
- The API still lists a `qualityLevel: 0` rendition (`video-h264-360p-lowest`,
  labelled 低画質) beside the normal one (`qualityLevel: 1`), but it is an
  alternate track, not the one yt-dlp picks while a normal track is available.
  If a peak-hour mode ever returns, the tell is a chosen format id containing
  `-lowest`; that is the point to fail the download instead of accepting it.
- `native` is not set. Nicovideo serves HLS (m3u8) with separate video/audio, and
  `ytdlp.conf` already routes `dash,m3u8` to yt-dlp's own downloader
  (`--downloader aria2c --downloader dash,m3u8:native`). So aria2c never touches
  nicovideo - the lane log says `[hlsnative]`, and a live process check during a
  nicovideo batch showed only `yt-dlp.exe`, no `aria2c.exe`. The concurrency knob
  is `-N 4` (HLS fragments) from the same config.
- **`serial` keeps one nicovideo download at a time.** Without it, four lanes
  each run `-N 4`, so up to 16 fragment connections hit the CDN at once;
  nndownload explicitly warns that multiple download sessions on one connection
  get temporary blocks or throttling. Serial drops the ceiling to the four
  fragments of the single active video. If that ever proves too much, a
  per-host `yt=-N,2` is the next step; aria2c is not involved either way.

## n.sfw.video

- **Generic yt-dlp tier only.** There is no dedicated yt-dlp or gallery-dl
  extractor; the generic tier reads the page and exposes 480p/720p/1080p MP4
  plus the full description. There is no `engine=` pin, because a pin skips the
  generic tier and the dedicated tier has no extractor for this host.
- `--write-description` writes `<site>/<title>.description`, and `tpl=` puts
  every video in `n.sfw.video/`.
- **The library follows the default destination**, like every site without a
  `dest=`: `G:\Downloads\n.sfw.video`, or `D:\Downloads\n.sfw.video` when the
  drive fallback moves the default destination because G: is low. There is no
  site-specific destination, so nothing here has to be kept in sync when the
  fallback switches drives.
- **`-S res` is required.** The generic extractor marks the LQ rendition
  `quality=1`; yt-dlp's default `b` then picks LQ. Sorting by resolution picks
  1080p (480/720/1080 carry real heights).
- **One flat folder per drive for now; author subfolders are the planned
  layout.** `tpl` has no `%(uploader)s`, so a video and its `.description` sit
  side by side in `n.sfw.video/`, on `G:` and on `D:` alike. The author is not
  exposed to the engine: the page has a `block-user` block linking to
  `/members/<id>/` (for example `ratzy`), but the generic extractor leaves
  `uploader` empty. When a dedicated extractor exposes the author, add
  `%(uploader)s/` — the display name, not the handle — to the template and file
  the existing videos into those folders, inside each drive; never move files
  between drives to do it. `workarounds.json` → `nsfw-video-generic` watches
  for that extractor.

## SpankBang

- The extractor reports the video id as the title (`41aiv`), so files were named
  after the id. `parse-metadata` in `ytdlp.conf` copies the real title out of the
  URL slug (and turns `+` back into spaces).

## Pixiv - pixiv.net

- **Artwork pages are images, so gallery-dl is the only engine that can fetch
  them.** yt-dlp has no pixiv extractor: the generic tier answers
  `Unsupported URL: .../artworks/<id>`, which is why a failure used to appear
  with no reason at all. `sites.txt` pins `pixiv.net engine=gdl` so the attempt
  and its error are visible.
- **gallery-dl needs a refresh token; cookies alone are not enough.** Its docs
  say OAuth is *necessary* for pixiv. Without a token every link stops at
  `AuthenticationError: 'refresh-token' required`, even with a valid
  `config/cookies.txt` and a logged-in browser.
- **`oauth=pixiv` makes Dredge run the login flow itself.** When a link on that
  host comes back asking for a token, the pass stops the lane board, runs
  `gallery-dl oauth:pixiv` in the console (it prints a URL, then asks for the
  `code` from the browser's Network tab), and retries the same link. Once per
  site per pass, so a cancelled flow cannot loop: the retry is the second and
  last chance, after which the link is recorded as failed. gallery-dl writes the
  token to its own cache (`extractor.oauth.cache` defaults to true), so nothing
  is copied into `config/gallery-dl.conf` — a token there is optional and only
  needed if the cache is unwritable.
- **Filenames are `<title> <id>`, with a page suffix only for multi-page posts.**
  `extractor.pixiv.filename` in `config/gallery-dl.conf` is
  `"\fF {title} {id}{('_p' + str(num)) if count > 1 else ''}.{extension}"`.
  The `\fF ` prefix selects gallery-dl's f-string formatter, because the
  `?before/after/` format spec cannot embed `{num}` (its parts are literal text)
  and `num` is 0 for a single image. `count` is the post's page total, so a
  one-image post gets no suffix while a multi-page one gets `_p0`, `_p1`, … on
  every page. Verified on both shapes (1-image post, 5-page post).
- The `#ppixiv` fragment that the ppixiv userscript appends is harmless:
  `canon`/`keys` already drop it (so re-queueing with or without it is the same
  link), and gallery-dl matches the path. It is *not* stripped from `dl_url`
  because fragments are load-bearing elsewhere - a MEGA link's `#<key>` is the
  decryption key.
- Bookmark listings (`bookmark_new_illust.php?...`) are pages, not posts; they
  only work with the token too and expand into many artworks.

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
