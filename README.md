# zzz_scraper

to get post, run PostScraper.py, this will get all posts in to csv.

then use MinasScraper.py to get all Minas links and password


then use MinasDownloader.py to download the minas files.


## Safe incremental assets

The update job checks out Git LFS pointers (`lfs: false`, `GIT_LFS_SKIP_SMUDGE=1`)
and keeps LFS installed so newly added originals still use LFS. It does not fetch
historical originals. No existing assets or Git history are removed.

`metafiles/asset-state.json` records a SHA-256 fingerprint of each completed CSV
row, including source link/password and any revision fields. Its initial 60 entries
were seeded only for posts with assets already tracked in Git. Unchanged rows skip
all asset network access, even in a pointer-only checkout. New or changed rows fetch
one post ZIP, validate and extract into temporary staging, then merge new/changed
files while retaining other files. Identical files are left untouched. The completion
record is written atomically only after installation succeeds; failures are retried
and cause a nonzero batch exit so Actions cannot auto-push a failed batch.

Changes inside a remote share at the same URL cannot be discovered from the CSV
alone. Set `MINAS_REFRESH_POSTS` to newline-separated exact dated folder names to
refresh selected shares, or change a revision field in their CSV rows. A changed
post currently requires downloading its whole ZIP; file comparisons avoid rewriting
identical extracted originals. The existing Minas link scraper reuses cached links;
this change does not add upstream post/share polling.

Downloads enforce Content-Length when available and count streamed bytes even
without it. Browser downloads are canceled as soon as the URL is available, then
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
