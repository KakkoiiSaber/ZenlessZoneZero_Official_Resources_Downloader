# Metadata-only Actions and a NAS media downloader

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

1. Make the reviewed code available to the NAS and copy `.env.example` to an untracked `.env` beside the Compose files. Fill the verified runtime UID/primary GID and the two project paths. Do not add an administrator supplemental group by default.
2. Confirm the catalog URL is accessible and current. An uninitialized or failed metadata update can leave it unavailable; do not start blind repeated retries.
3. In Container Manager Project, use the code's `deploy/synology` directory and import **compose.first-run.yaml**. It is a complete single-file configuration: at most two transfer attempts, a 50 MiB run budget, no interval and restart disabled. Its relative build context must still point to the code root.
4. Only after explicit approval, build and run that sample. Check `data/state/last-sync.json`, the completed files and source associations. Larger files may be deferred by the sample budget; this does not justify removing all limits.
5. The viewer only checks read access. Runtime write access must be checked using the chosen downloader identity, without changing unrelated ACLs or overwriting user files.
6. Only after the sample succeeds and the user chooses to proceed, use `compose.yaml` for normal polling. This repository never connects to the NAS or enables a schedule on the user's behalf.

The Docker image copies only application code/dependencies, not `.git`, original assets, previews, CSV caches, personal `.env` files or repository configuration. The root filesystem and config mount are read-only; only data and temporary storage are writable. No ports, host networking, privileged mode or Docker socket are configured.

## Defaults and recovery

Normal configuration allows 128 transfers and 2 GiB received per run, up to 32 source attempts, a 256 MiB image or 1 GiB video, a 5 GiB free-space reserve and an 1800-second per-file limit. All transfers are staged and SHA-256 is computed while streaming. A failed file is never checkpointed as complete. Rebuilding the container resumes from persistent state; interrupted files are retried whole, without HTTP Range resume.

Identical bytes share one object while retaining source/path/post associations. Different resolutions/hashes remain separate. The first scan can still transfer identical bytes from distinct sources until their real hashes are known. Same-name replacements retain prior content history. A valid LFS placeholder is replaced only after matching downloaded hash/size; other local corruption fails closed.

Unchanged-size/timestamp replacements enter a bounded verification queue after seven days; the default is four checks per run. This is not a guarantee of discovery within seven days. With about 2000 simultaneously due paths and six-hour polling, a full queue can take about 125 days; duplicate source paths increase the queue. Configure `NAS_VERIFY_DAYS` and `NAS_RUN_VERIFY_FILES` according to bandwidth. A new remote path cannot be discovered until the metadata workflow produces a new listing.

No existing LFS originals are automatically migrated to the NAS. Expired source shares may leave historical originals available only in the existing GitHub archive. Existing files/history are never purged by this tool.

## Supported media and limits

Minas files: JPG/JPEG, PNG, GIF, WebP, BMP, AVIF, TIF/TIFF; MP4, WebM, MOV, MKV, M4V, AVI. Types are based on extensions, not codec validation. Video originals are downloaded/indexed without ffmpeg, transcoding or video thumbnails. Standalone video platforms and other cloud-drive providers are not implemented.

This is a downloader plus a JSON content index, not a NAS gallery website. New NAS files do not automatically update the existing public Pages gallery or inherit DSM web login. Do not expose NAS ports just to use the downloader.

Offline tests cover catalog stability/privacy, cumulative additions, nested media, source changes, multi-source dedup, replacement history, budgets, interrupted streams, pointer recovery and locking. Container builds, Linux/DSM behavior and live NAS transfers require a separately authorized sample on the actual device; do not interpret local tests as deployment verification.

Official Container Manager Project documentation: https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project?version=7
