# zzz_scraper

to get post, run PostScraper.py, this will get all posts in to csv.

then use MinasScraper.py to get all Minas links and password


then use MinasDownloader.py to download the minas files.


## Safe incremental assets

The update job checks out Git LFS pointers (`lfs: false`, `GIT_LFS_SKIP_SMUDGE=1`)
and keeps LFS installed so newly added originals still use LFS. It does not fetch
historical originals. No unique image contents or Git history are removed; exact duplicate paths are
handled by the recoverable deduplication step below.

`metafiles/asset-state.json` records a SHA-256 fingerprint of each completed CSV
row for categories other than `影像档案记录分享`, including source link/password and any revision fields. Its initial 60 entries
were seeded only for posts with assets already tracked in Git. Unchanged rows skip
all asset network access, even in a pointer-only checkout. New or changed rows fetch
one post ZIP, validate and extract into temporary staging, then merge new/changed
files while retaining other files. Identical files are left untouched. The completion
record is written atomically only after installation succeeds; failures are retried
and cause a nonzero batch exit. The workflow retains verified completed progress even when other rows fail; failed rows remain eligible for retry (see recovery below).

For those other categories, changes inside a remote share at the same URL cannot
be discovered from the CSV alone. Set `MINAS_REFRESH_POSTS` to newline-separated exact dated folder names to
refresh selected shares, or change a revision field in their CSV rows. A changed
post currently requires downloading its whole ZIP; file comparisons avoid rewriting
identical extracted originals. The existing Minas link scraper reuses cached links;
this does not add upstream post polling; the category-specific share scans below
are independent of that scraper cache.

Downloads enforce Content-Length when available and count streamed bytes even
without it. A streaming deadline (`MINAS_DOWNLOAD_MAX_SECONDS`, default 300)
is checked between chunks, with at most one additional read timeout. Browser downloads are canceled as soon as the URL is available, then
retried through a bounded authenticated stream. ZIPs are checked for traversal,
symlinks, duplicate paths, file count, per-file size and total extracted size before
installation. Extraction checks free disk space as it writes. Temporary work is
cleaned on ordinary failures/cancellation; process termination may leave `.minas-*`
work directories, but does not mark the post complete. Installation rolls back
ordinary IO failures; after a hard process kill, the next run retries the whole row.

Environment settings (positive integers, bytes unless noted):

| Setting | Default |
| --- | --- |
| `MINAS_MAX_ARCHIVE_BYTES` | 536870912 (512 MiB) |
| `MINAS_MAX_EXTRACTED_BYTES` | 2147483648 (2 GiB) |
| `MINAS_MAX_FILE_BYTES` | 268435456 (256 MiB) |
| `MINAS_MAX_FILES` | 5000 entries |
| `MINAS_MIN_FREE_BYTES` | 1073741824 (1 GiB reserve) |
| `MINAS_ROW_TIMEOUT` | 420 seconds (browser/async row timeout) |
| `MINAS_STATE_FILE` | `metafiles/asset-state.json` |
| `GALLERY_MAX_BYTES` | 100000 |
| `GALLERY_MAX_EDGE` | 1600 pixels |
| `GALLERY_MAX_PIXELS` | 50000000 source pixels |

The HTTP transfer also has 30-second connect/read timeouts; synchronous streaming
and extraction finish under their byte/disk limits rather than the async row timer.
The gallery hashes original content and recognizes LFS SHA-256 pointers instead of
using checkout mtimes. Missing/pointer originals retain published preview metadata
and existing cache entries. New pointers without previews are skipped. Legacy
caches migrate lazily without decoding pointer text. New previews preserve aspect
ratio, cap the longest edge, and shrink further until the byte limit is met. Existing
previews are retained until a real original is available; the new size limits cannot
retroactively resize previews whose originals are absent. Corrupt JSON state stops
the build instead of silently replacing it. Preview failures also fail the build while
retaining previously published entries.

Run local regression tests without any asset/network downloads:

```sh
python -m unittest discover -s tests -v
```

On Windows, some historical asset paths contain characters such as `|` and cannot
be checked out. Use a sparse checkout excluding `downloads/`, `docs/thumbs/` and
`posts/` for code-only development; the original tracked files remain in Git. Never
stage deletions caused by an incomplete checkout. Actions runs on Linux.


## Mutable image-archive shares

`影像档案记录分享` now uses the share page's observed file-listing API and
individual-file download route, without a browser or ZIP. Every eligible category
row participates on every batch run, even if its CSV fingerprint/share URL is unchanged.
Repeated references to the same normalized share are scanned once per run. Different
share tokens remain separate sources; identical previous snapshots do not establish
permanent equivalence. The remaining categories keep the bounded ZIP behavior above.

The scanner traverses every nested folder, validates paths and response shapes,
and records source path, size, listing `last_modified`, opaque HEAD/GET ETag, actual
SHA-256, and verification/check timestamps in `metafiles/minas-files.json`.
The observed endpoint returns `dirent_list` without pagination. A future response
announcing more pages fails explicitly rather than silently accepting an incomplete
scan. Per-file HEAD is metadata-only; it does not depend on HTTP 304 support.
ETags are not SHA-256, and download-response `Last-Modified` is deliberately ignored.
Metadata or validator changes trigger an individual bounded download. SHA-256 is
computed from received bytes, and a file checkpoint is written atomically only
after byte counts, limits and installation succeed. Partial downloads never become
completed records. Source/scan errors fail the batch and block the workflow's push.

Because unchanged size, timestamps and ETags are imperfect evidence, unchanged files
become eligible for full byte verification after seven days. At most four such
verifications and 32 total file downloads occur per run, within a shared 512 MiB
stream budget. Deferred entries retain their previous checkpoint and are retried;
this is successful partial progress, not a completed source snapshot. Older checks
and least-recently-transferred sources are prioritized. The refresh interval is an
eligibility threshold: actual verification can be later when the bounded queue is
large, so increase budgets or reduce the interval according to acceptable staleness.
New paths are discovered by listing on every run, independently of this interval.
The byte budget includes bytes received by failed downloads. A single listed file
larger than the run budget requires an explicit configuration increase.

The seed index contains 10 posts, seven shares, 38 content hashes and 357 historical
paths derived solely from committed LFS pointers. On its first scan, a seeded file
whose size matches and whose listing timestamp predates the repository snapshot
establishes a metadata/ETag baseline without hydrating LFS. Newer, unknown-date or
changed-size entries require download. This bootstrap cannot prove that same-size
remote contents retained old metadata; periodic verification covers that limitation
within the configured queue/budgets. No live files were downloaded to prepare the
seed index. Missing remote entries do not delete historical records or originals.

New content is stored under `downloads/_minas-content/<sha256>.<extension>` and
continues to use the existing `downloads/**` LFS rule. Same-name replacements retain
the previous content file and its provenance. Every unique original and Git
history are preserved; exact duplicate copies may be remapped and removed. Already-known content reuses its existing preview when one
is locally present; otherwise received bytes can supply one content-addressed local
copy for preview generation. No historical LFS objects are fetched automatically.
Deduplication does not claim additional physical LFS storage savings for identical
OIDs; it avoids repeated gallery work and, through individual selection, unnecessary
whole-ZIP transfers.

The gallery groups known identical SHA-256 content into one card and reuses one
preview. Metadata retains every original path under `associations` and all known
post/source/remote-path relationships under `posts`, explicitly marked
`included-in-share`; inclusion does not mean an image was featured by each post.
Different resolutions/content hashes remain separate. Byte-identical duplicate preview files
may be removed after references are remapped; different preview bytes are retained.

Additional settings (positive integers unless a path):

| Setting | Default |
| --- | --- |
| `MINAS_FILE_STATE_FILE` | `metafiles/minas-files.json` |
| `MINAS_FILE_VERIFY_DAYS` | 7 |
| `MINAS_FILE_RUN_VERIFY_FILES` | 4 |
| `MINAS_FILE_RUN_MAX_DOWNLOADS` | 32 |
| `MINAS_FILE_RUN_MAX_BYTES` | 536870912 (512 MiB) |
| `MINAS_FILE_MAX_DIRS` | 256 per share |
| `MINAS_FILE_MAX_DEPTH` | 32 path components |
| `MINAS_FILE_MAX_LISTING_BYTES` | 2097152 per response |
| `MINAS_FILE_MAX_METADATA_REQUESTS` | 10000 per run |
| `MINAS_DOWNLOAD_MAX_SECONDS` | 300 per streamed download |

`MINAS_MAX_FILES`, `MINAS_MAX_FILE_BYTES` and the free-space reserve also apply.
The workflow exposes refresh/download budgets through matching GitHub repository
variables. The workflow is manual-only (`workflow_dispatch`); it contains no schedule
or push trigger. Enabling this workflow for an explicitly approved update therefore
does not resume recurring downloads. The Actions job has a 45-minute
timeout. Access codes remain in the existing CSV and are never copied to the new index.


## Gallery versions and search

The preview page uses broad content categories and searchable tags as its primary
controls, with an optional explicit-version selector under More filters and a reset
to all images. Filters compose; filtered cards retain
correct detail-page links, and returning from a detail preserves the filters.
Versions come from explicit `1.2版本` or `v1.2` labels in asset paths or source
metadata. Dates alone never imply a version. For cumulative image archives, the
image's own version subfolder wins: the enclosing post title is excluded. A shared
image can belong to multiple explicit versions, and unknowns are labeled `未分类`.
The metadata also exposes categories, version memberships and provenance; the
detail page lists associated posts without claiming every included image was featured.
Existing image previews are reused. In sparse checkouts, the gallery can read tiny
committed LFS pointer blobs through Git to identify missing originals; it never
runs LFS fetch/smudge. Browser tests use only local synthetic images;
optional `GALLERY_SCREENSHOT_DIR` saves desktop/mobile evidence outside the repo.


## Recoverable exact-image deduplication

`python tools/deduplicate_images.py` prints a read-only plan. `--apply` applies
current-tree deletions, remaps the source index, and writes
`metafiles/image-aliases.json`; run the gallery builder afterward to update preview
references/cache. Actions performs this after downloads and before gallery generation.
The tool reads validated LFS SHA-256 pointers rather than hydrating originals, hashes
ordinary files/previews, and retains one deterministic image per content hash
(preferring already materialized bytes, then lexical path order). Different hashes,
resolutions, video, and unrelated files are excluded from duplicate removal.

For the existing snapshot, 1,048 original images become 726 unique images: logical
by-path original bytes fall from 3,361,887,908 to 2,548,034,385. The 726 exact preview
contents occupy 104,068,837 bytes instead of 144,560,616 across 1,048 paths. The MP4
remains untouched. These ordinary tracked deletions are recoverable from earlier Git
commits. They do not rewrite history, prune LFS objects, reduce already deduplicated
physical LFS object storage, or erase historical quota usage.

The alias map preserves old original/preview path mappings; source records retain
historical paths and all post/source/version relationships. Current gallery image
and cache references point to surviving files. Original-download links use the retained
path through GitHub media on `main` (`GALLERY_ORIGINAL_BASE` can override this base),
rather than attempting to serve LFS pointer text from Pages. The detail page separates
preview viewing from original downloading; it never starts an original download itself. New detail links include content IDs;
`legacy_indices` resolves the previously published numeric detail links to surviving
cards. External URLs pointing directly at a removed duplicate file can no longer
serve that file on static hosting; use the canonical path from the alias map.
No redirect files pretending to be images are created.

On a sparse Windows development checkout, `--apply --stage-sparse` explicitly stages
only planned verified image deletions, including paths Windows cannot materialize.
Normal Actions runs use `--apply` and stage their resulting changes in the existing
commit step. Never use history rewriting, LFS prune, or remote object deletion for
this cleanup. Repeated runs leave canonical copies and associations in place rather
than recreating redundant image files.


## Content categories and searchable tags

The primary categories are `角色图片`, `月历`, `活动壁纸`, `测试期素材`, `表情包`,
`影像档案`, and `未分类`. Deduplicated images keep all applicable categories/tags
from their retained historical paths and post/source associations. The keyword field
matches tags, character aliases, titles, paths, and associated post titles; multiple
space-separated keywords compose with AND. Bare version keywords such as `1.7`
match explicit/associated version tags rather than coincidental posting dates.
Category and keyword filters compose. Existing content-ID/numeric detail links,
original downloads, reset behavior, and optional explicit-version URLs remain supported.

`tags` contains typed records with `kind`, `value`, `label`, `confidence`, and
`evidence`. Tags cover characters, literal themes, PV/EP/MV source types, explicitly
labeled versions and calendar year/month. Evidence retains the original path/post
or an official announcement URL. Per-image source subfolders take priority over mixed
post captions (e.g. a role-showcase image is not automatically labeled PV just because
the enclosing post bundles both). When only a post-level source caption is available,
source-type chips say `来源帖` and carry contextual rather than asset-level evidence. Calendar filenames take precedence over a mismatched
enclosing post month; dates alone never create version labels. Cumulative archives
use an image's own subfolder/source path instead of applying an enclosing post's
character or version to the whole collection.

Only the character groups already checked against official announcements get
character-debut association tags: 伊德海莉/狛野真斗 (2.3), 席德/奥菲丝与鬼火 (2.2),
薇薇安/雨果 (1.7), 零号·安比/扳机 (1.6), 耀嘉音/伊芙琳 (1.5). For generic character art these remain
`associated_version`, `confidence: inferred`, `relation: character_debut`, visibly
labeled `关联版本 1.7 · 将眼泪与过往一同埋葬 · 推断`. They do not assert
an image's first upload version or add later rerun appearances. Other characters
remain searchable without an unverified debut mapping.

The bounded rule `official-wallpaper-debut-v1` in `tools/gallery_tag_rules.py`
recognizes the repository's official-source wallpaper title convention:
`壁纸分享 | <character>角色PV&角色展示壁纸` (also `丨`, quoted aliases,
role-only variants, and the observed mixed `角色展示&EP`/`角色展示&MV` forms).
It requires a matching Miyoushe article association and the same title in that
image's historical path. A `主题壁纸` post needs a matching named or bare
`角色展示`/`角色PV` image subfolder. Resolution suffixes are accepted. A role-only
bundle may use its title when its image paths have no finer source label.
A mixed EP/MV bundle only promotes an unambiguous role subfolder: its music or
unknown subfolders retain inferred tags. A character in arbitrary text, a bare
`PV`, EP/MV alone, rerun/review/fan-edit titles, and enclosing cumulative-archive
titles are insufficient. No posting dates, bodies or new network requests are used.

This rule combines the observed official title/directory convention with the
verified character debut map. It emits `confidence: rule_supported`,
`relation: debut_content`, e.g. `版本 1.7 · 将眼泪与过往一同埋葬`, without
a generic inference badge. Evidence retains the rule ID, exact title/path,
source post and official character-version announcement; the detail page explains
that this is a debut-content association rather than an asserted upload date.
Direct asset-version evidence still takes precedence, and different version
associations aggregate into separate tags without losing their provenance.

Actual positive corpus examples include [薇薇安's role-only bundle](https://www.miyoushe.com/zzz/article/65001819),
[扳机's role-only bundle](https://www.miyoushe.com/zzz/article/64380445), and
[伊芙琳's mixed showcase/EP bundle](https://www.miyoushe.com/zzz/article/62318049).
Only its role subfolders qualify. These explicit patterns are deliberately
maintainable and conservative; new title forms need reviewed examples and tests.

The numeric version and verified official version name share one chip; an identical
standalone theme chip is removed. `VERSION_NAMES` records official announcement
URLs for 1.1–1.7 and 2.0–2.3. Unverified numbers retain a numeric-only chip, and
unclassified images remain `未分类`. Either the number or name is searchable,
numeric ordering remains numeric (2.10 before 2.9), and the optional version selector
includes direct and rule-supported versions, excluding purely inferred membership.

The current 726 unique images keep the same categories, original paths, previews,
and associations: 606 character images, 46 calendars, 15 event wallpapers, five
test-period images, 16 emoticons, and 38 image-archive images. Of the 359 existing
character-debut associations, 145 now have rule-supported content evidence and
214 remain inferred; 38 images retain direct version evidence. One further direct
version tag gains rule provenance without creating a second chip. There are 1,781
tags after combining the 38 duplicate version-name themes. The detail page retains
all evidence; inferred chips keep a distinct dashed appearance.

When MinasScraper already fetches a new public post, it additively records a small
`metafiles/post-evidence.json` sidecar (`MINAS_POST_EVIDENCE_FILE` overrides the path).
It stores at most five bounded version-mention excerpts and 20 source topic names,
not full post bodies, access codes or share-download URLs. Sensitive/link-containing
lines are excluded. Body mentions have `confidence: unreviewed` and post scope;
they are not automatically promoted to asset version tags. Evidence capture does
not add requests or re-fetch cached posts, and an evidence-write failure leaves
normal link extraction intact. This patch performs no live body backfill.


## ZIP handoff, partial failures and Pages publication

The ZIP browser flow intercepts the first same-origin `/seafhttp/zip/…` request
before Chromium transfers any bytes. It then performs exactly one bounded streaming
GET with the authenticated cookies and request context. It never starts/cancels a
browser transfer and replays its URL. A bounded validation against the December
2025 two-image calendar share reproduced the old flow's HTTP 403; the new flow
successfully extracted both images (6,390,439 bytes) with an 8 MiB archive cap.
This verifies the handoff fix, without claiming a particular server-side token
implementation or guaranteeing every remote share remains available.

`metafiles/last-update.json` records completed/unchanged/missing/failed ZIP rows,
file-transfer attempts/completions/bytes/deferred records, and sanitized error types
and HTTP codes. It never stores download tokens, passwords or exception URLs.
File-level completion counts advance only after a durable per-file state checkpoint.
Successful rows retain their fingerprints when a later row fails; a retry skips them.
Deferred source-file records are not unique-image counts. Logs flush immediately and
include periodic listing progress. Existing size/disk/refresh/transfer budgets remain.

The manual-only workflow checks out current `main` with LFS smudging disabled and
checks its base before starting. The update step has a 35-minute timeout inside the
45-minute job, reserving time to validate and retain output. A failed update step
continues to deduplication and gallery validation. Valid completed output is committed
and normally pushed to `main`; the run still ends in failure to make incomplete work
visible. Failed or unfinished rows are never marked complete.

If gallery validation fails, metadata is malformed/truncated, or `main` has advanced,
`tools/checkpoint_update.py` retains the scoped output on
`asset-recovery/<run-id>-<attempt>` instead. It never force-pushes or silently merges
JSON conflicts. Only downloads, metadata and generated previews/cache are staged;
pre-existing staged work is rejected and task-owned staging directories stay ignored.
A network error on push is checked against the remote commit before recovery, avoiding
blind duplicate publication. Recovery-only output is not deployed. A hard runner loss
before checkpoint publication cannot be made durable by later steps; an overall job
cancellation or LFS/remote-storage refusal may also prevent a recovery push. The workflow
reports those failures rather than claiming a checkpoint exists.

GitHub token pushes do not automatically build branch-published Pages. The workflow
therefore uses its short-lived `pages: write` token permission (alongside existing
`contents: write`) to request the existing Pages build explicitly. No account settings,
long-lived credentials or OIDC permission are added. `tools/publish_pages.py` verifies
current main and the exact built commit, observes an existing matching pending build
rather than duplicating it, and fails on rejection, build error or timeout. The final
summary shows checkpoint/recovery references and net unique-image counts. No scheduled
or push-triggered asset updates are enabled.
