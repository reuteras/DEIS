# Architecture

DEIS is a `docker compose` stack. Stages coordinate through marker files under
`status/` rather than a message queue — each container polls for the marker
it needs and touches its own when done. `bin/deis.py` is a thin CLI wrapper
around `docker compose --profile ...` plus these marker files and a couple of
ES/Kibana HTTP calls.

## Services (`docker-compose.yml`)

| Service | Image/base | Profile(s) | Role |
|---|---|---|---|
| `deis` | custom, cron (`dcron`) | `deis` | Orchestrator: crontab runs `urls.sh` → `download.sh` → `done.sh` every minute. Submits queued URLs to aria2, polls aria2 for batch completion, moves finished downloads into `files/`. |
| `downloader` | custom, aria2 | `deis`, `download` | aria2c daemon (JSON-RPC on 6800), patched for TOR-friendly retry/timeout behavior. Fetches `.onion` URLs via the TOR proxy, clearnet URLs directly (unless `FORCE_TOR=true`). Writes into `downloader/data`. |
| `controller` | AriaNg (static, nginx) | `deis`, `download` | Web UI (port 8080) for the aria2 RPC endpoint — visibility/control over the download queue, no pipeline logic of its own. |
| `unpack` | custom, 7-Zip + readpst + tesseract | `deis`, `unpack` | Recursive archive extraction, PST/OST parsing, OCR sidecar generation. Reads `files/`, writes `extracted/`. |
| `ingest` | custom Python | `deis`, `ingest` | Walks `extracted/files`, indexes into Elasticsearch via the attachment (Tika) pipeline. |
| `elasticsearch` | elastic/elasticsearch | (always) | Document store + Tika-based attachment processor pipeline. Single node. |
| `kibana` | elastic/kibana | (always) | Search/browse UI against the ES index (port 5601). |
| `web` | custom FastAPI | (always) | Status/funnel dashboard and a file-viewer proxy (converts office docs via gotenberg for inline viewing) (port 8081). |
| `notebook` | `reuteras/container-notebook` | (always) | JupyterLab (port 8888, token auth) for ad-hoc analysis: ES queries, word clouds, etc. |
| `gotenberg` | gotenberg/gotenberg | (always) | LibreOffice/Chromium rendering backend used only by `web` to convert office docs/HTML to PDF for viewing. |
| `setup` | custom | `setup` | One-shot: creates ES roles/users/passwords, imports `setup/export.ndjson` (Kibana saved objects: index pattern, dashboards). |

`deis` and `setup` are meta-profiles: `deis` covers every stage-specific
profile so `deis run` (no `--only`) brings up the whole processing chain in
one `docker compose up`; each stage also has its own profile
(`download`/`unpack`/`ingest`/`setup`) so `deis run --only <stage>` can start
just that container.

## Networks

Three bridge networks isolate concerns:

+ `download` (deis/downloader/controller — the only containers that need aria2 RPC)
+ `elk` (elasticsearch/kibana/ingest/setup + notebook and web, which query ES)
+ `web` (web + notebook + gotenberg, for the file-viewer's conversion calls)

Only `elk`/`web`-network ports are published to `127.0.0.1`.

## Data flow / directories

```text
urls/, add-urls    ──▶  aria2 (downloader)  ──▶  downloader/data/
                                                        │ done.sh moves finished files
                                                        ▼
                                                      files/
                                                        │ unpack (7-Zip/readpst, recursive)
                                                        ▼
                                          ┌── extracted/files/<sha256>   (extracted content, deduped by hash)
                                          ├── extracted/sha256/          (symlink namespace web/ uses to serve by hash)
                                          └── extracted/archive/         (originals kept post-extraction, if *_archive=true)
                                                        │ ingest.py (Tika attachment pipeline)
                                                        ▼
                                          Elasticsearch index `leakdata-index-000001`
                                                        │
                                          Kibana / notebook / web  (search & review)
```

+ **`files/`** — raw, as-downloaded input to `unpack`. Emptied out as unpack
  consumes/moves originals (per `deis.cfg`'s `zip_remove`/`zip_archive` etc.);
  a `.gitignore` is the only tracked entry and is excluded from all counts.
+ **`extracted/files/`** — flat output of recursive extraction, one entry per
  *occurrence* (not deduplicated) named by sha256. Archives are unpacked into
  it recursively (nested archives re-processed up to `max_depth`, default 6);
  non-archive files from `files/` are copied straight in; `.msg`/`.eml`/`.mbox`
  are left alone for Tika; OCR text lands as `<name>.ocr.txt` sidecars next to
  images. This is what `ingest` walks.
+ **`extracted/sha256/`** — dedup namespace: one entry per unique content hash,
  used by `web` to serve/view a file by hash regardless of how many places it
  occurred.
+ **`extracted/archive/`** — original archives retained after extraction
  (only populated when `deis.cfg` has `*_archive=true`), collision-suffixed
  (`-dup2`, …) rather than overwritten.
+ **`downloader/data/`** — aria2's working/download directory; drained into
  `files/` by `deis`'s `done.sh` once a batch fully completes.
+ **`status/`** — marker files coordinating stages, all polled rather than
  pushed: `downloaded` (download batch done) → `unpack` (files moved, unpack
  may start) → `extract_done` (unpack finished, ingest may start) →
  `ingest_done`. Also holds diagnostic output (`still_encrypted.txt`,
  `still_corrupt.txt`, `still_unsafe.txt`, `extensions.txt`, `mime.txt`,
  `path.txt`) that `deis status`/`web` surface.
+ **`logs/`** — per-stage logs (`download_errors.log`, `unpack.log`,
  `ingest.log`).
+ **`passwords/`** — operator-supplied `*.txt` wordlists, one password per
  line, tried against every archive at every nesting level (in addition to
  `ZIP_PASSWORD` from `.env`).
+ **`db/file_hashes.db`** — sqlite, optional (`ingest.use_sqlite` in
  `deis.cfg`): maps sha256 back to an original filename for display, since
  `extracted/files` itself is keyed by hash, not name.
+ **`urls/`** — operator-supplied URL lists consumed by `deis add-urls` /
  `deis/urls.sh`.

## Pipeline coordination

No queue or event bus: every stage container polls a marker file in
`status/` on a loop (or via the `deis` container's crontab) and touches its
own marker when its precondition-driven work is done. This keeps each
container independently restartable and makes `deis status` a simple matter
of reading marker files plus counting files in `files/`/`extracted/files/`/
`extracted/sha256/` and querying ES's `_count`.

## Post-ingest enrichment

Run manually (not part of the automatic marker chain):

+ `deis pii-scan` — regex + checksum-validated extraction (Swedish
  personnummer/samordningsnummer, IBAN, card numbers, email, phone) into
  each document's `pii` field.
+ `deis dedupe-scan` — SimHash-based near-duplicate clustering into
  `duplicate_cluster`, distinct from ingest's exact-sha256 dedup.

Both operate directly against the Elasticsearch index via `bin/pii.py` /
`bin/simhash.py`, not on files on disk.
