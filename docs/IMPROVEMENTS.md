# DEIS improvement backlog

## How to read this

An audit of all four DEIS stages plus the operator experience, written as a prioritized
backlog. Item numbers are stable: they are referenced from commit messages and from the
sections below, so fixed items keep their number rather than being renumbered away.

Effort is rough: **S** = an afternoon, **M** = a few days, **L** = a project.

## Context

DEIS is a docker compose pipeline for investigating ransomware leak dumps:
**D**ownload over TOR → **E**xtract archives → **I**ngest into Elasticsearch via Tika →
**S**earch in Kibana and JupyterLab. It is meant to be usable by people who are not
developers, and it handles stolen data containing other people's personal information.

Two properties should drive prioritization, because they follow from what the tool is:

1. Silent data loss is the worst failure mode. Someone asking "does this leak contain my
   friend's personal data?" gets a wrong answer if a file was skipped and nobody noticed.
2. The data is toxic and the operator may be a target, so defaults matter more here than
   in a normal side project.

## Already fixed

| Item | What was wrong | Commit |
|---|---|---|
| 1 | Ingest wrote the "done" marker before Elasticsearch confirmed the document, so a crash mid-upload made a file look processed forever | `8e6bbdf` |
| 2 | `len(None)` on an unreadable file, and a narrow `except`, could kill an entire ingest run | `8e6bbdf` |
| 3 | Elasticsearch, Kibana, Gotenberg and JupyterLab published on `0.0.0.0`; Jupyter ran with authentication disabled | `92297dd` |
| 4 | Logstash crash-looped on the obsolete `http.host` setting and started by default despite being unused | `92297dd` |
| 5 | Only `http-proxy`/`https-proxy` were set, so any other scheme bypassed TOR silently | `5ce18cc` |
| 6 | One unreachable URL stalled the pipeline forever, and `tellStopped` read only the first 1000 entries | `5ce18cc` |
| 7 | `.env` held the passwords but was tracked by git | `92297dd` |
| 8 | `addurl.sh` built JSON by string interpolation, so a URL containing a quote failed silently | `5ce18cc` |
| 11 | Clearnet routing policy was accidental rather than decided | `5ce18cc` |
| 25 | Nothing reconciled files on disk against documents indexed | `8e6bbdf` |
| 26 | A run marked itself complete even when files had failed | `8e6bbdf` |

Item 11 was resolved as a deliberate decision, worth recording: **`.onion` goes through TOR
because nothing else resolves it, and everything else is fetched directly**, because the
operator is expected to be on a VPN and routing clearnet traffic through TOR only makes it
slower. `FORCE_TOR=true` restores the everything-through-TOR behaviour. The preflight TOR
leak test suggested in the original item 11 was not implemented and is still worth having;
see item 42.

## Live reconciliation

The numbers a healthy run should produce, from the current stack:

```text
397 files on disk  →  273 unique sha256  →  273 markers  →  273 documents
on disk but not in Elasticsearch:  0
in Elasticsearch but not on disk:  0
```

Far fewer documents than files is normal: the document id **is** the sha256, so identical
files collapse into one document. Here 151 hashes appear once, 122 appear more than once,
and 124 duplicate copies are folded in (151 + 122 = 273 unique; 273 + 124 = 397 files).

**Correction to an earlier version of this document.** It claimed two files were being
silently lost, based on a symlink count of 274 against 272 documents. That was wrong on two
counts. The two extra hashes belonged to `path.txt` and `done`, bookkeeping files that
`ingest.py` has deliberately skipped since commit `29474d9` (Jan 2024), and their symlinks
were orphans left from before that guard existed. The count also came from the wrong place:
there are **two separate sha256 symlink trees** — `ingest.py` writes its markers to
`extracted/sha256` on the host, while the `web` container has its own `deis_shasum` volume
built independently by `web/startup.sh`. Comparing the second one against Elasticsearch
compares two things that were never meant to match. No data had been lost. The underlying
ordering bug in item 1 was real and is fixed, but this was not evidence of it. The
duplicated symlink tree is itself worth cleaning up; see item 43.

## D — Download

### 9. `TORSERVNUM` does not drive the number of parallel circuits

How the download path actually works: v2ray listens on `127.0.0.1:16001` and randomly
balances across 50 outbounds, `tor-1` to `tor-50`. All 50 point at the **same** TOR SOCKS
proxy, `127.0.0.1:9050`, served by a single `tor` daemon; what differs is `sendThrough`,
which binds each outbound to its own loopback source address (`127.0.0.1` through
`127.0.0.50`). TOR isolates circuits by client address, so each outbound gets its own
circuit. The point is **parallel downloads over one TOR daemon**, not multiple proxies.
Confirmed on the running stack: four concurrent downloads went out over `tor-25`, `tor-33`,
`tor-34` and `tor-40`, with `netstat` showing exactly one listener on `9050`.

The defect is narrower than the mechanism: `TORSERVNUM` is set in `.env` and passed into the
`deis` and `downloader` containers, but nothing reads it. The count of 50 is hardcoded in
`downloader/conf/config.json`, so raising or lowering `TORSERVNUM` to tune parallelism does
nothing at all. Either generate the outbound list from the variable at container start, or
drop the variable and document 50 as fixed. *Effort: S. Impact: low, but it is a knob that
looks live and is not.*

### 44. The TOR configuration generator fails on every start

`downloader/run/run_aria2.sh` runs `creatorrc.py --speetor` to generate a tuned `torrc`, then
falls back to plain `tor --runasdaemon 1` if that fails. It always fails:

```text
Fetching server descriptors...
Traceback (most recent call last):
  File "/home/creatorrc/creatorrc.py", line 196, in <module>
    f = open("tor_config.txt", "w")
PermissionError: [Errno 13] Permission denied: 'tor_config.txt'
```

The script writes `tor_config.txt` into the current working directory, which the `aria2` user
cannot write to. `/conf/torrc` is therefore never created, and TOR starts with stock defaults
— its own log confirms it: `Configuration file "/etc/tor/torrc" not present, using reasonable
defaults`. So the "speetor" tuning this project deliberately pulls in has never once been
applied, and the `||` fallback hides the failure behind a working-looking container.

Fix is small: run the generator from a writable directory, or give it an absolute output
path, and stop swallowing the error. Worth pairing with item 10, since this is third-party
code fetched unpinned at build time and executed at every start. Note that fixing it will
change guard selection and therefore real download behaviour, so it should be a deliberate
change rather than a silent one. *Effort: S. Impact: medium.*

### 10. Unpinned third-party code fetched at image build

`downloader/Dockerfile` pulls `creatorrc.py` and `guard_country_resolver.py` from a
third-party GitHub repo with no pin or checksum and runs them at container startup. v2ray is
installed by a fetched script that verifies only against a digest from the same host, and
`unpack/install.sh` downloads 7-Zip 2301 with no checksum. Pin to commits, verify checksums,
or vendor the code. *Effort: M. Impact: medium-high.*

### 12. No integrity verification of downloads

`aria2.addUri` passes no per-download options, so aria2's `checksum` support is unused and
nothing verifies a hash afterwards. Where a leak site publishes hashes, accepting an optional
`url<TAB>sha256` format in `urls/*.txt` would catch truncated or tampered downloads before
they poison the extract stage. *Effort: M.*

### 13. The move step in `done.sh` is one-shot and unverified

Guarded by the `/files/extract` marker, so any file landing in `downloader/data` afterwards
is never moved. The `mv` has no collision handling and no success check. (Note the naming:
`deis/done.sh` moves the files, `deis/download.sh` polls aria2 — the two read as if they were
swapped.) *Effort: S.*

### 42. Preflight TOR leak test

Fetch `check.torproject.org` through the configured proxy chain before any download starts
and refuse to run if the answer is "not using TOR". With per-URL routing now in place this
should assert the intended behaviour rather than blanket TOR use: `.onion` proxied, clearnet
direct unless `FORCE_TOR` is set. For a non-expert operator this is the single most valuable
opsec check, because it fails loudly instead of leaking quietly. *Effort: M. Impact: high.*

## E — Extract

### 14. Nested extraction is two fixed passes, not recursion to a fixpoint

Pass 1 handles `/files/*`; pass 2 walks `/extracted/files` once. Anything three levels deep
(zip → zip → pst) is only picked up by accident of `find` traversal order while the same run
is still writing files, which is undocumented and non-deterministic. Loop until a pass
produces no new archives, with a depth cap. *Effort: M. Impact: high, this is silent recall
loss.*

### 15. Pass 2 only matches `zip|gz|7z|gzip`

`.rar`, `.tar`, `.tgz`, `.bz2` and `.xz` are excluded, so a `.rar` inside a `.zip` is never
extracted even though pass 1 handles `.rar` fine and 7-Zip supports all of them. Better
still, detect by content with `file --mime-type` rather than by extension, since leak dumps
are full of wrong and missing extensions. *Effort: S. Impact: high.*

### 16. `ZIP_PASSWORD` is silently ignored in pass 2

`zip_extract()` never passes `-p`, so a password-protected archive nested inside another
archive always fails, quietly. *Effort: S.*

### 17. "Wrong password", "corrupt" and "not an archive" are indistinguishable

All three collapse to `|| extracted=false`, a raw copy, and the log line
`"Not an archive, copying as-is"`, which is actively misleading for the password case.
7-Zip's exit codes distinguish them. Related: support a password list file rather than one
global password, since leak dumps routinely use several, and record which archives remain
encrypted so they can be revisited. *Effort: S. Impact: high.*

### 18. No guards on hostile archives

No zip-slip check on extracted paths, no expansion-ratio or output-size cap, no disk-space
check, no timeout on `7zz` or `readpst`. A decompression bomb fills the volume and a hung
archive stalls the serial pipeline indefinitely. `web/app.py` has careful path-traversal
defenses; the extract stage, which is the part actually handling adversary-supplied
archives, has none. *Effort: M. Impact: high.*

### 19. Extraction is fully serial

`ingest.py` uses a `ProcessPoolExecutor`, but `unpack/start.sh` processes one archive at a
time with no parallelism, no progress bar, no timestamps and no log file — just bare `echo`.
On a multi-hundred-GB dump this is the pipeline's wall-clock bottleneck, and the operator
sees almost nothing while it runs. *Effort: M. Impact: high.*

### 20. Fragile shell mechanics in `unpack/start.sh`

Config is read with substring matching (`grep "unpack=true" /deis.cfg`), which is not
section-aware and would false-positive on a key like `not_unpack=true`. Pass 1 iterates
without `-print0`/`read -d ''` while passes 2 and 3 use it, so a filename containing a
newline breaks only the first loop. Pass-2 failures leave empty sha256-named directories
behind. *Effort: S.*

### 21. More extractors

Roughly in order of real-world value for leak dumps:

- **Email beyond PST**: `.msg`, `.eml`, `.mbox`, `.ost`. Only `.pst` is handled today.
- **OCR for image-only documents**, via tesseract or Tika's tesseract integration. A scanned
  passport or invoice currently indexes with `content_length: 0` and is invisible to every
  content search. This is the largest recall gap in the tool.
- **Structured data as rows rather than blobs**: `.csv`, `.xlsx`, `.sql` dumps and SQLite
  files are where personal data actually lives in these leaks. Tika flattens them to text;
  parsing them into per-record documents would turn "is my friend in here?" into a precise
  query.
- **Disk and VM images**: `.vmdk`, `.vhdx`, `.E01`, raw `.dd`.
- **Mobile backups**, `.iso`/`.wim`, mail-server maildirs.
- **Encrypted archives**: at minimum list them so they are not forgotten; optionally attempt
  a wordlist.

## I — Ingest

### 22. Define an explicit index template instead of relying on dynamic mapping

The mapping is entirely dynamic today. `sha256` and `filename` should be `keyword` — a hash
analyzed as English prose is wasted work and tokenizes oddly. `attachment.content` should not
carry a `.keyword` multi-field. Set `index.mapping.total_fields.limit` to bound mapping
explosion from hostile documents, set `highlight.max_analyzed_offset` in the template so the
README's manual Dev Tools workaround disappears, and set `number_of_replicas: 0` for the
single-node default so the cluster is green rather than permanently yellow with an unassigned
replica. *Effort: M. Impact: high.*

### 23. Add ILM and rollover, or drop the `-000001` pretence

The index name implies rollover, but `ingest.py` writes to a hardcoded
`leakdata-index-000001` with no alias and no ILM policy. Writing through an alias would keep
large investigations manageable and make "one index set per case" natural. *Effort: M.*

### 24. Use the `_bulk` API

Every file is a separate `PUT /_doc/<sha>` from a worker process. Bulk batching is the
standard order-of-magnitude win, and it reduces the pressure that makes the retry paths
trigger in the first place. *Effort: M. Impact: high on large dumps.*

### 27. `use_sqlite=True` is broken in the container

`sqlite3.connect("db/file_hashes.db")` resolves to `/db/file_hashes.db` because the working
directory is `/`, and the `ingest` service mounts no such volume, so it fails immediately.
The database is only ever populated by the manual `bin/pathfix.py`, whose `DB_PATH` is a
different relative path. Either wire it up properly, agreeing on one path and mounting
`./db`, or remove the option. *Effort: S.*

### 28. `bin/pathfix.py` reads whole files into memory

`compute_sha256` calls `f.read()` on the entire file, which will OOM on any large archive or
disk image. `ingest.py` already streams in 4 KB blocks; reuse that. *Effort: S.*

### 29. Dead ingest pipeline and stale boilerplate

The `attachment` pipeline is created by `setup/entrypoint.sh` but never referenced — only
`cbor-attachment` is used. `kibana/config/kibana.yml` carries a full Fleet and Elastic Agent
configuration block for services that do not exist here, and `setup/export.ndjson` ships
dozens of unrelated stock `system-*` and `elastic_agent-*` dashboards that clutter the
saved-objects list. *Effort: S. Impact: medium, clarity.*

### 30. `README.md` references `./bin/ingest.sh`, which does not exist

The documented "only run ingest" path is broken. *Effort: S.*

### 41. Identical files can be indexed twice on first ingest

A consequence of the fix for item 1: the marker is now written only after Elasticsearch
confirms, so two workers hashing identical copies of a file that has never been ingested can
both upload it before either marker exists. It is harmless — the document id is the sha256,
so it is the same document written twice — but it wastes an upload and a Tika parse per
collision. Observed in testing: removing the marker for a file with four identical copies and
re-running produced three indexing operations for two removed documents. The fix is a
two-phase marker: claim it atomically with `O_EXCL` before uploading, promote it on success,
remove it on failure. That gets the crash-safety of item 1 without the duplicate work.
*Effort: S. Impact: low, efficiency only.*

## S — Search

### 31. PII and personnummer detection

The origin story of this project is "does this leak contain my friend's personal data", yet
the only way to answer it is free-text KQL. Detect Swedish personnummer and samordningsnummer
with checksum validation, plus emails, phone numbers, IBAN and card numbers, and national IDs
for other locales, either as an ingest-time enrichment or a post-pass. Index them as
structured fields so an analyst can filter on a value and get a definitive answer, and so a
dashboard can show which documents contain personal identifiers at all. *Effort: L. Impact:
very high — this is the feature that most directly serves the use case.*

### 32. Entity extraction and language detection

Names, organisations and locations as structured fields turn the corpus from a text blob into
something pivotable. The notebook's word cloud already hand-maintains English, Swedish and
Portuguese stopword lists; automatic language detection would replace that and enable
per-language analyzers, which materially improves recall on non-English dumps. *Effort: L.
Impact: high.*

### 33. Near-duplicate detection and clustering

Leak dumps are full of near-identical documents: mail threads, template letters, versioned
files. Exact sha256 dedup already exists; fuzzy clustering with SimHash or MinHash, or a
`fingerprint` analyzer on content, would cut the volume a person has to read. *Effort: M.*

### 34. Surface the failure states in the dashboard

Panels for files that failed extraction, archives that are still encrypted, zero-content
files (this one exists), and the ingest reconciliation counts. Someone needs to know what the
search results do **not** cover before concluding "not found". *Effort: S. Impact: high, it
directly counters false negatives.*

### 35. Notebook hardening

The Elasticsearch password is embedded in the connection URL string. The word cloud pulls the
entire corpus with `match_all` and `size=10000`, which is fine at a few hundred documents and
fatal at scale — use aggregations or `significant_text`. The index name is hardcoded, and
duplicates the one in `ingest.py`. *Effort: M.*

### 36. Result quality

Highlighted snippets rather than raw content, a saved search per detected entity type, and
export of a result set as CSV or JSON for reporting back to whoever asked. *Effort: M.*

## CLI — one command for non-technical operators

Today the documented path requires copying and editing `.env` and `deis.cfg`, running
`docker compose --profile setup up -d`, watching `docker logs deis-setup-1 -f`, running
`docker compose --profile deis up -d`, then `just venv` and `just progress` — and, when a
search fails, pasting two JSON blobs into Kibana's Dev Tools console. That is a lot of
surface for the intended audience. `bin/progress.py` is a good seed: it already uses `rich`
and infers state from the marker files.

A single `deis` command, built on stdlib `argparse` plus `rich` to keep the dependency
footprint small, wrapping the existing scripts rather than replacing them:

- `deis init` — interactive first run: generate real passwords into an untracked `.env`,
  create `deis.cfg`, check Docker's memory allocation against the 18 GB requirement, and fail
  early with a plain-language message instead of an Elasticsearch OOM ten minutes later.
- `deis doctor` — preflight and diagnosis: Docker reachable, memory sufficient, disk space
  against expected dump size, the TOR routing check from item 42, Elasticsearch and Kibana
  health, and a plain-English explanation of any crash-looping container. This one command
  would have surfaced four of the seven original P0 items on its own.
- `deis add-urls <file|url>` — validate schemes, deduplicate, report exactly what was queued.
- `deis run [--only download,extract,ingest]` — replaces the profile incantations.
- `deis status` — `progress.py` plus counts at each boundary, so the funnel is visible by
  default rather than on request.
- `deis search <term>` — terminal search for people who never open Kibana.
- `deis report` — what was found, and what could not be processed.
- `deis clean` / `deis reset` — wrap the `Justfile` targets behind a confirmation prompt,
  since they delete evidence.

Keep `just` for developer tasks such as linting and exporting requirements; the CLI is for
operators. *Effort: L. Impact: high — it is the difference between a pipeline you can run and
a tool you can hand to a friend.*

## Cross-cutting

### 37. No tests at all

There is no test suite, and CI runs only super-linter and osv-scanner. The best targets are
pure functions with real security weight and no infrastructure needs: `web/app.py`'s path
validation, which has already been the subject of CodeQL findings, the sha256 and dedup
logic, config parsing, and a zip-slip and bomb fixture set once item 18 exists. A compose
smoke test that ingests a tiny fixture dump and asserts the reconciliation counts end to end
would catch the whole class of problem this document opens with. *Effort: M. Impact: high.*

### 38. Make resource limits configurable

`ES_JAVA_OPTS: -Xms2g -Xmx16g` is hardcoded in `docker-compose.yml`, which forces the
README's 18 GB Docker requirement on everyone regardless of dump size. Drive it from `.env`
with a smaller default. *Effort: S.*

### 39. Structured logging across stages

`deis/download.sh` and `deis/urls.sh` write timestamped entries to
`logs/download_errors.log`; the extract stage and everything else echo to stdout with no
timestamps and no log file. One log directory, one format, per-file outcomes. This is the
substrate that items 17 and 34 and the CLI's `status` and `report` all depend on.
*Effort: M.*

### 40. Decide the fate of the log-ingest scaffolding

`evtx2json/`, `evtx/`, `json/`, `syslog/` and the filebeat extension are a half-built path
from closed issue #1. `syslog/` is mounted into filebeat but referenced by no input config,
and the `modules.d` glob points at a directory that does not exist. Either finish the wiring
or remove it, and document `evtx2json` as the manual side tool it currently is. *Effort: S.*

### 43. Two separate sha256 symlink trees

`ingest.py` writes its dedup markers to `extracted/sha256` on the host, while the `web`
container has its own `deis_shasum` volume, populated independently by `web/startup.sh`
rehashing every file at startup. Two sources of truth for the same mapping, kept in sync by
nothing, which is what made the miscount described under "Live reconciliation" possible.
Pick one: either mount the host directory into `web`, or have `web` treat the volume as a
cache it rebuilds only when empty. *Effort: S. Impact: medium.*

## Suggested sequencing

1. **Make losses visible** (34, 39, 17): failure dashboards and structured logging. The
   ingest side of this is done; extraction is still silent. Cheap, and it tells you whether
   everything else is working.
2. **Fix extraction correctness** (14, 15, 16, 18): recursion to a fixpoint, content-based
   detection, nested passwords, hostile-archive guards. This is where recall is quietly lost
   today, and it is the largest remaining correctness gap.
3. **Index quality and scale** (22, 23, 24): explicit template, bulk indexing, ILM.
4. **The CLI**, once the underlying states are reportable — it is a facade over the items
   above, and building it first would mean building it twice.
5. **Analytical power** (31, then OCR from 21, then 32, 33): PII detection first, because it
   is the question the tool exists to answer.

Housekeeping items (9, 13, 27, 28, 29, 30, 41, 43) are individually small and can be picked
up whenever the surrounding code is being touched anyway. Item 44 is small too but changes
how TOR picks guards, so treat it as a deliberate change rather than a cleanup.

## Verification approach

- Keep a small fixture dump in the repo — nested archives, a password-protected archive, a
  zip-slip entry, a decompression bomb, an image-only PDF, a corrupt file — and assert counts
  end to end.
- Reproduce each failure before fixing it. Kill the ingest container mid-run and confirm the
  file is retried; submit a `magnet:` URL and confirm it is rejected rather than leaked; add a
  never-completing URL and confirm the pipeline reports it as stalled rather than hanging.
- Re-run the reconciliation under "Live reconciliation" and require the numbers to agree,
  modulo known duplicates. The ingest summary now prints most of this at the end of every run.
