# Metadata-only Actions and a NAS media downloader

## Wiki-first resource index

The metadata pipeline now uses the public ZZZ Wiki's observed category tree and
channel/detail JSON interfaces. It indexes the four media categories (character
video, game video, wallpaper collections, affinity wallpapers) and the agent
roster. A bounded first pass does not mean all entries have been inspected: the
site and `resource-status.json` report indexed, checked, directly resolved and
deferred counts separately. The initial snapshot has 360 indexed works, four checked
details, two directly resolved works and nine media references; 356 details remain
unscanned. `Link` entries remain links to their original posts;
external video platforms are not silently treated as downloadable MP4 files.

`metafiles/resource-catalog.json` is the v2 NAS manifest; the v1 Minas catalog is
retained. Existing source/path IDs, gallery identities, preview/download links,
originals and all historical media stay intact. Wiki detail revision strings are
editorial revisions, not game versions or trusted file modification dates.

The generated knowledge file records names, full names, aliases, distinct forms,
factions and source URLs. New roster entries can be learned without a Python
name-list edit. Announced/indexed is not assumed to mean released. Numeric version
names come from explicit official announcement titles; character debut mappings
are retained only where reviewed release evidence exists. Missing debut evidence
stays unknown. Merely seeing a character, EP or MV does not assign an asset's
release version. Short aliases require structured context and longer overlapping
forms take precedence.

Official-site matching requires an exact normalized work title and the same
language. It does not merge different PV/EP/MV works, numeric versions or character
forms. Multiple matches are marked for review. Each work retains Wiki, matched
official and original-post links; published image/video references retain their
per-image section/caption evidence. Collection-wide character keywords are not
copied to every image. Old sources/media survive index failures and source edits.

Only explicit persistent references on the reviewed official CDN host allowlist
are downloadable. Metadata collection never requests media URLs. The NAS validates
each redirect target before sending a request. Signed/token queries, credentials,
unexpected hosts and media-transfer routes in the Wiki client are rejected.
Content SHA-256 is calculated only after streaming a file; CDN filenames, Wiki
revisions and ETags are never treated as content hashes.

Quality is reported as a published source with original quality unverified.
Variants are grouped only when both observed URLs address the same CDN object
with an OSS image transform. An observed untransformed reference is preferred,
without claiming that it is the highest-resolution historical original. All
variant references remain; distinct files and Minas resolutions remain separate.

The default manual metadata run checks at most 12 due details and preserves the
legacy index. An optional `refresh_legacy` input refreshes old post/Minas metadata,
never originals. Failed details retain the last good data and are retried within
future budgets. `python tools/collect_resources.py --offline --max-details 0`
reclassifies from the existing caches without any requests. Newly captured public
post evidence is sanitized and persisted rather than discarded with temporary
share-code files.

The website has an additional `resources.html` directory and retains the existing
image/detail pages. Cached previews are reused during reclassification. The
metadata workflow checkpoints only public metadata and generated index JSON; it
does not download files, upload LFS or change account permissions. Pages publication
uses the repository's existing publication mechanism separately: a GITHUB_TOKEN
metadata push alone is not a promise of an automatic Pages build.

## Runtime changes and rollback

The new image reads both v1 and v2 manifests and keeps state format version 1.
Official CDN files are attempted ahead of preserved Minas sources. A first sample
uses images only, at most two transfer attempts and 50 MiB, no interval, restart
disabled and a 600-second cooperative run budget. Normal mode retains the existing
file/byte/disk limits and allows images and videos. Requests and received blocks
check the run deadline; an in-progress blocking network/DNS operation can delay
the next deadline check.

Startup, catalog loading, source-code lookup, authentication, download progress,
completion and sanitized failures are printed immediately. Progress is on stderr
and the final JSON summary on stdout; Docker logs show both. No raw response,
password, URL path, cookie or exception text is logged. A public-post response such
as retcode 1034 with null data produces an actionable source-lookup error, not an
AttributeError. This guard does not obtain a missing access code: exact-share
private mappings or a usable official source are still needed.

Size, transfer-count, media-type, verification and time-budget limits are deferred
work, not failures. Interrupted transfers never mark an item complete, temporary
files are removed and durable successes remain. Content duplicates share one
object while all item/source associations remain. First encounters at different
URLs can still transfer equal bytes before their hashes are known.

For rollback, stop the worker, retain the same data/config directories and runtime
identity, then restore a known image reference. If rolling back to the pre-v2 image,
also restore its v1 `metafiles/catalog.json` manifest URL. Do not delete state,
objects, volumes or historical images. The old image ignores official-only IDs
that are not in its v1 catalog; the newer state fields are additive. Actual DSM
ACLs, hardware behavior and real NAS transfers still require a user-run sample.

GitHub updates public post/source metadata. A user-operated NAS downloads images and videos from the source shares. Existing LFS originals, Git history and the static gallery are preserved; this workflow does not rebuild or publish that gallery.

## Before deploying

Confirm your DSM/Container Manager version, a non-root runtime UID and primary GID, and two dedicated project directories. Use the generic `.env.example` as a template; keep actual account numbers, private host paths and access-code mappings in an untracked `.env` / private configuration file. No NAS login password or GitHub PAT is required for this public catalog.

The person viewing media may be read-only and need not be the downloader's file identity. Do not grant write access to a viewer just to run the downloader. An administrator can manage Container Manager while the container runs under a non-zero UID; no privileged mode, Docker socket or whole-home/shared-volume mounts are used. File ACLs and future website authentication are separate concerns.

Only mount:

- Your dedicated data directory as `/data` (runtime identity needs read/write).
- Your dedicated private config directory as `/config:ro` (runtime identity needs read access).

Layout inside those directories:

```text
config/share-codes.json
data/objects/<prefix>/<sha256>.<extension>
data/state/sync.json
data/state/catalog.json
data/state/last-sync.json
data/sync.lock
```

Do not overwrite existing directories belonging to another application. No remote deletion is mirrored. Back up the objects and state together; deleting a container must not delete its bind-mounted data.

## Metadata workflow

`Metadata Update` is manual-only. It checks official posts and recursively lists each distinct Minas share, even if its URL is unchanged. It publishes `metafiles/catalog.json` and `catalog-status.json`, plus public post indexes. It does not download original media, upload LFS objects, rebuild previews or deploy Pages.

The catalog contains stable source/path IDs, media type, sizes, listing timestamps, post associations, categories and evidence-backed version/tags. Explicit versions in cumulative subfolders take precedence over an enclosing post. Unverified names/versions remain unknown or inferred with provenance. Listings have no trusted content hash; an ETag is not treated as SHA-256.

Access codes discovered from public posts are used only in temporary files/memory and never copied to the new catalog or logs. Current tracked CSV code fields are empty and the legacy CLI has no hardcoded password default. Older Git history may contain previously committed values: history has deliberately not been rewritten.

A partial listing failure preserves prior catalog entries. A complete listing that no longer contains a path marks that path unavailable, without deleting local originals or associations. Metadata commits are scoped to public indexes/catalogs; concurrent main changes retain the output on a recovery branch rather than overwrite main.

## Private access-code file

Copy `share-codes.example.json` into your private config directory as `share-codes.json`:

```json
{"version":1,"shares":{}}
```

The worker first uses an explicit private mapping, otherwise inspects up to eight matching official public posts. A code is accepted only for its exact share; ambiguous multi-share posts need manual configuration. If required, add a mapping from the catalog source ID to that share's actual code. Do not publish the populated file.

## First sample, after approval

1. Copy the Compose files and `.env.example` to a dedicated project directory on the NAS; rename the latter to an untracked `.env`. No source checkout or ZIP of application code is needed. Fill the verified runtime UID/primary GID and the two project paths. Set `NAS_IMAGE` to the exact published image digest from a successful `Publish NAS Image` run. Do not add an administrator supplemental group by default.
2. Confirm the catalog URL is accessible and current. An uninitialized or failed metadata update can leave it unavailable; do not start blind repeated retries.
3. In Container Manager Project, use that dedicated project directory and import **compose.first-run.yaml**. It pulls the pinned Linux amd64 image without building on the NAS. It is a complete single-file configuration: at most two transfer attempts, a 50 MiB run budget, no interval and restart disabled.
4. Only after explicit approval, run that sample. Check `data/state/last-sync.json`, the completed files and source associations. Larger files may be deferred by the sample budget; this does not justify removing all limits.
5. The viewer only checks read access. Runtime write access must be checked using the chosen downloader identity, without changing unrelated ACLs or overwriting user files.
6. Only after the sample succeeds and the user chooses to proceed, use `compose.yaml` for normal polling. This repository never connects to the NAS or enables a schedule on the user's behalf.

The Docker image copies only application code/dependencies, not `.git`, original assets, previews, CSV caches, personal `.env` files or repository configuration. The root filesystem and config mount are read-only; only data and temporary storage are writable. No ports, host networking, privileged mode or Docker socket are configured.

## Program image publication and manual upgrades

`Publish NAS Image` is a separate **manual-only** GitHub workflow, restricted to `main`. It runs offline regressions, prepares an allowlisted program-only build context, builds Linux amd64, and tests the actual non-root container with networking disabled and an empty local catalog. It publishes to `ghcr.io/kakkoiisaber/zzz-scraper-nas` using only that job's short-lived `GITHUB_TOKEN` (`contents: read`, `packages: write`). It never runs metadata collection or transfers original media. Image publication does not deploy anything to the NAS.

Each run uses a unique `sha-<full-commit>-<run-id>-<attempt>` tag and reports the digest. There is no moving `latest` tag, push-triggered build, automatic NAS updater or new credential. Prefer `ghcr.io/kakkoiisaber/zzz-scraper-nas@sha256:<published-digest>` in private `NAS_IMAGE`. Keep the previous reference for rollback. A tag includes a full source commit and a unique run/attempt; no existing release tag is intentionally overwritten.

New GHCR packages default to private even for public source repositories. The workflow checks anonymous access after publication and clearly reports if it is unavailable. Before credential-free NAS use, an owner must explicitly approve and set **only this package's** visibility to Public in its package settings. Do not add a PAT, broaden account permissions or claim the image is publicly usable until anonymous manifest access has been verified. Official guidance: https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry

To upgrade after reviewing a release, stop the worker and let an active transfer exit, retain the previous image reference, change only `NAS_IMAGE` in the private `.env`, then use Container Manager's pull/recreate operation (or `docker compose pull` followed by `docker compose up -d`). Retain the same config/data mounts and account IDs. Do not select volume deletion, overwrite private config, reset state or start a second worker. To roll back, restore the previous image reference and recreate using the same persistent directories. This image-only release does not change the state schema; future releases must document any incompatible migration before promising rollback.

Ordinary catalog changes require no image upgrade: the worker reads the configured catalog on its next normal poll. New remote paths still require a metadata workflow update first. Publication of an image does not repair existing source-listing errors in a catalog.

## Transfer defaults and recovery

Normal configuration allows 128 transfers and 2 GiB received per run, up to 32 source attempts, a 256 MiB image or 1 GiB video, a 5 GiB free-space reserve and an 1800-second per-file limit. All transfers are staged and SHA-256 is computed while streaming. A failed file is never checkpointed as complete. Rebuilding the container resumes from persistent state; interrupted files are retried whole, without HTTP Range resume.

Identical bytes share one object while retaining source/path/post associations. Different resolutions/hashes remain separate. The first scan can still transfer identical bytes from distinct sources until their real hashes are known. Same-name replacements retain prior content history. A valid LFS placeholder is replaced only after matching downloaded hash/size; other local corruption fails closed.

Unchanged-size/timestamp replacements enter a bounded verification queue after seven days; the default is four checks per run. This is not a guarantee of discovery within seven days. With about 2000 simultaneously due paths and six-hour polling, a full queue can take about 125 days; duplicate source paths increase the queue. Configure `NAS_VERIFY_DAYS` and `NAS_RUN_VERIFY_FILES` according to bandwidth. A new remote path cannot be discovered until the metadata workflow produces a new listing.

No existing LFS originals are automatically migrated to the NAS. Expired source shares may leave historical originals available only in the existing GitHub archive. Existing files/history are never purged by this tool.

## Supported media and limits

Minas files: JPG/JPEG, PNG, GIF, WebP, BMP, AVIF, TIF/TIFF; MP4, WebM, MOV, MKV, M4V, AVI. Types are based on extensions, not codec validation. Video originals are downloaded/indexed without ffmpeg, transcoding or video thumbnails. Standalone video platforms and other cloud-drive providers are not implemented.

This is a downloader plus a JSON content index, not a NAS gallery website. New NAS files do not automatically update the existing public Pages gallery or inherit DSM web login. Do not expose NAS ports just to use the downloader.

Offline tests cover catalog stability/privacy, cumulative additions, nested media, source changes, multi-source dedup, replacement history, budgets, interrupted streams, pointer recovery, locking and program-only build boundaries. Each image publication additionally tests its built Linux container offline. DSM integration, actual bind-mount ACLs and live NAS transfers remain unverified until a separately authorized sample on the device; CI tests do not confirm NAS deployment.

Official Container Manager Project documentation: https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project?version=7
