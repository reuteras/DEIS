# DEIS improvement backlog

## How to read this

An audit of all four DEIS stages plus the operator experience, written as a prioritized
backlog. Item numbers are stable: they are referenced from commit messages and from the
sections below, so fixed items keep their number rather than being renumbered away.

Open items come first, grouped by stage (D/E/I/S), then the CLI proposal and cross-cutting
items, then suggested sequencing and how to verify a fix. Everything already fixed - full
write-ups, not just a one-line summary - is collected at the end, after a quick-scan table.

Effort is rough: **S** = an afternoon, **M** = a few days, **L** = a project.

## Context

DEIS is a docker compose pipeline for investigating ransomware leak dumps:
**D**ownload over TOR → **E**xtract archives → **I**ngest into Elasticsearch via Tika →
**S**earch in Kibana and JupyterLab. It is meant to be usable by people who are not
developers, and it handles stolen data containing other people's personal information.

Two properties should drive prioritization, because they follow from what the tool is:

1. Silent data loss is the worst failure mode. Someone asking "does this leak contain my friend's personal data?" gets a wrong answer if a file was skipped and nobody noticed.
2. The data is toxic and the operator may be a target, so defaults matter more here than in a normal side project.

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
duplicated symlink tree was itself cleaned up; see item 43 in "Already fixed".

## Open items

### D — Download

#### 10. Unpinned third-party code fetched at image build (partly fixed)

`creatorrc.py` and `guard_country_resolver.py` are now vendored into `downloader/creatorrc/`
rather than fetched over HTTPS from GitHub at every build, with source commit, license and
sha256 recorded in `downloader/creatorrc/VENDORED.md`. `unpack/install.sh` still fetches
7-Zip fresh at build time — a real binary, not practical to vendor into git — but the
download is now checked against a sha256 recorded in `unpack/VENDORED.md`, and the build
fails rather than continuing on a mismatch; verified with a deliberately wrong hash. 7-Zip
was also bumped from 23.01 to 26.03 in the same change.

Still open: v2ray itself is installed by `downloader/install-release.sh`, a fetched script
that verifies its download only against a digest pulled from the same host it downloaded
from — that has not been touched. *Effort: M.*

#### 42. Preflight TOR leak test

Fetch `check.torproject.org` through the configured proxy chain before any download starts
and refuse to run if the answer is "not using TOR". With per-URL routing now in place this
should assert the intended behaviour rather than blanket TOR use: `.onion` proxied, clearnet
direct unless `FORCE_TOR` is set. For a non-expert operator this is the single most valuable
opsec check, because it fails loudly instead of leaking quietly. *Effort: M. Impact: high.*

### E — Extract

#### 21. More extractors (OCR done - see "Already fixed"; the rest is still open)

Roughly in order of real-world value for leak dumps:

- **Email beyond PST**: `.msg`, `.eml`, `.mbox`, `.ost`. Only `.pst` is handled today.
- **Structured data as rows rather than blobs**: `.csv`, `.xlsx`, `.sql` dumps and SQLite
  files are where personal data actually lives in these leaks. Tika flattens them to text;
  parsing them into per-record documents would turn "is my friend in here?" into a precise
  query.
- **Disk and VM images**: `.vmdk`, `.vhdx`, `.E01`, raw `.dd`.
- **Mobile backups**, `.iso`/`.wim`, mail-server maildirs.
- **Encrypted archives**: at minimum list them so they are not forgotten; optionally attempt
  a wordlist.

### S — Search

#### 32. Entity extraction - names, organisations, locations (language detection is done; see "Already fixed")

Names, organisations and locations as structured fields would turn the corpus from a text blob
into something pivotable - real named-entity recognition (NER), not the regex/heuristic
approach items 18/31 use for archive safety and PII, since there is no checksum or fixed shape
to validate a person's name against. Deliberately not attempted here: doing this properly
needs a real NLP model (spaCy's per-language models are hundreds of MB each; Elasticsearch's
own inference API needs a hosted model uploaded via Eland, a separate ML toolchain). That is a
genuine new-dependency decision this project's minimize-dependencies posture says is worth
raising explicitly rather than picking unilaterally - and a low-quality regex-based
"NER" (e.g. flagging every capitalized phrase as an entity) would likely be worse than nothing,
cluttering search results with noise on a tool whose whole premise is trustworthy answers.
*Effort: L. Impact: high, but blocked on a dependency decision.*

#### 33. Near-duplicate detection and clustering

Leak dumps are full of near-identical documents: mail threads, template letters, versioned
files. Exact sha256 dedup already exists; fuzzy clustering with SimHash or MinHash, or a
`fingerprint` analyzer on content, would cut the volume a person has to read. *Effort: M.*

#### 36. Result quality

Highlighted snippets rather than raw content, a saved search per detected entity type, and
export of a result set as CSV or JSON for reporting back to whoever asked. *Effort: M.*

### Cross-cutting

#### 40. Decide the fate of the log-ingest scaffolding

`evtx2json/`, `evtx/`, `json/`, `syslog/` and the filebeat extension are a half-built path
from closed issue #1. `syslog/` is mounted into filebeat but referenced by no input config,
and the `modules.d` glob points at a directory that does not exist. Either finish the wiring
or remove it, and document `evtx2json` as the manual side tool it currently is. *Effort: S.*

## Suggested sequencing

1. **Analytical power, continued** (33, near-duplicate detection). PII detection (31), OCR
    (the highest-value part of 21), and language detection (the tractable half of 32) are
    done; see "Already fixed". The CLI (originally step 1) is also done. Entity extraction
    (the rest of 32) is blocked on a dependency decision (spaCy vs. Elasticsearch's inference
    API), not effort. The rest of 21 (email formats beyond PST, structured data as rows,
    disk/VM images, mobile backups, encrypted-archive listing) is still open, lower priority.

Housekeeping items (10's v2ray remainder, 42's preflight leak test, 40's log-ingest
scaffolding, 36's result-quality polish) are individually small and can be picked up whenever
the surrounding code is being touched anyway.

## Verification approach

- Keep a small fixture dump in the repo — nested archives, a password-protected archive, a
  zip-slip entry, a decompression bomb, an image-only PDF, a corrupt file — and assert counts
  end to end.
- Reproduce each failure before fixing it. Kill the ingest container mid-run and confirm the
  file is retried; submit a `magnet:` URL and confirm it is rejected rather than leaked; add a
  never-completing URL and confirm the pipeline reports it as stalled rather than hanging.
- Re-run the reconciliation under "Live reconciliation" and require the numbers to agree,
  modulo known duplicates. The ingest summary now prints most of this at the end of every run.

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
| 9 | `TORSERVNUM` was set but read by nothing, so it never changed the number of TOR circuits | `5e5232c` |
| 11 | Clearnet routing policy was accidental rather than decided | `5ce18cc` |
| 13 | The move from `downloader/data` to `files` had no collision handling, no success check, and never retried | `95fd325` |
| 14 | Extraction was two fixed passes, so archives nested three or more levels deep were found only by accident | `996f479` |
| 15 | Archive detection was a fixed extension list, so a `.rar` inside a `.zip` was never extracted | `996f479` |
| 16 | `ZIP_PASSWORD` was silently ignored on nested archives | `996f479` |
| 17 | Wrong password, corrupt, and not-an-archive were indistinguishable and all logged the same misleading message | `996f479` |
| 18 | No zip-slip/decompression-bomb/disk-space guard and no timeout on `7zz`/`readpst` | 44ff308 |
| 25 | Nothing reconciled files on disk against documents indexed, and the counts were never Kibana-visible | 44ff308 |
| 26 | A run marked itself complete even when files had failed | `8e6bbdf` |
| 19 | `unpack/start.sh` extracted one archive at a time regardless of available CPU cores | `010903c` |
| 20 | Config reading in `unpack/start.sh` used substring matching instead of being section-aware | `010903c` |
| 22 | The index mapping was entirely dynamic; `sha256`/`filename` were analyzed as text and the cluster was permanently `yellow` | `c8b0f59` |
| 24 | Every ingested file was a separate `PUT /_doc`, not batched via `_bulk` | `c8b0f59` |
| 27 | `use_sqlite=True` failed immediately in the container, and its own database's own producer used a different path | `c8b0f59` |
| 28 | `bin/pathfix.py` read whole files into memory instead of streaming | `c8b0f59` |
| 29 (partly) | The unused `attachment` ingest pipeline was created alongside the one actually used | `c8b0f59` |
| 30 | README's "only run ingest" command referenced a script that does not exist | `c8b0f59` |
| 43 | `web` and `ingest.py` kept two independent, unsynchronized sha256 symlink trees | `c8b0f59` |
| 34 | No Kibana-visible signal existed for "still encrypted"/"corrupt"/"unsafe", only plain files on disk | `a6fef83` |
| 35 | Password in the connection URL, hardcoded/duplicated index name, and a full-corpus pull for the word cloud | `a6fef83` |
| 38 | `ES_JAVA_OPTS` was hardcoded, forcing the 18 GB Docker requirement on everyone regardless of dump size | `3f3cf9b` |
| 39 (ingest) | `ingest.py` had no per-file log, only an end-of-run summary | `3f3cf9b` |
| 41 | Multiple copies of a never-before-seen file could all be uploaded and Tika-parsed before any marker existed | `f228de5` |
| 44 | `creatorrc.py` failed on every start, so TOR ran on stock defaults and the guard tuning was never applied | `014be0f` |
| 37 | No test suite and CI ran only super-linter/osv-scanner; found and fixed a `re.match` gap in `web/app.py` and two CodeQL findings (embedded-credential URLs) in `ingest.py` along the way | `931e508` |
| CLI | Running DEIS meant memorizing docker compose profile incantations and checking four marker-file directories by hand | 065c714 |
| 31 | The only way to answer "does this leak contain my friend's personal data" was free-text KQL | 23ac2e8 |
| 21 (OCR only) | A scanned passport or invoice saved as a plain image indexed with no searchable text at all | 23ac2e8 |
| 32 (language detection only) | The notebook hand-applied both stopword lists at once instead of detecting which language a document is actually in | 23ac2e8 |

Item 10 is only partly fixed — `creatorrc.py` and `guard_country_resolver.py` are vendored
and 7-Zip is checksummed (`c80f15c`), but the v2ray installer is still fetched unpinned. See
item 10 above for what remains.

Item 11 was resolved as a deliberate decision, worth recording: **`.onion` goes through TOR
because nothing else resolves it, and everything else is fetched directly**, because the
operator is expected to be on a VPN and routing clearnet traffic through TOR only makes it
slower. `FORCE_TOR=true` restores the everything-through-TOR behaviour. The preflight TOR
leak test suggested in the original item 11 was not implemented and is still worth having;
see item 42 above.

### D — Download (fixed)

#### 9. `TORSERVNUM` did not drive the number of parallel circuits (fixed)

Kept because it explains the mechanism. v2ray listens on `127.0.0.1:16001` and randomly
balances across N outbounds, `tor-1` to `tor-N`. All of them point at the **same** TOR SOCKS
proxy, `127.0.0.1:9050`, served by a single `tor` daemon; what differs is `sendThrough`,
which binds each outbound to its own loopback source address (`127.0.0.1` through
`127.0.0.N`). TOR isolates circuits by client address, so each outbound gets its own
circuit. The point is **parallel downloads over one TOR daemon**, not multiple proxies.
Confirmed on the running stack: four concurrent downloads went out over `tor-25`, `tor-33`,
`tor-34` and `tor-40`, with `netstat` showing exactly one listener on `9050`.

`downloader/conf/config.json` used to hardcode 50 such outbounds, so `TORSERVNUM` was set in
`.env` and passed into two containers but read by nothing. It is now generated by
`downloader/run/generate_v2ray_config.py` from `TORSERVNUM` on every start (no network
needed, so this is cheap), range-checked to 1-250 with a fallback to 50 on anything invalid.
Verified: `TORSERVNUM=7` produces exactly 7 outbounds, and the default output is
semantically identical to the file it replaced.

#### 44. The TOR configuration generator failed on every start (fixed)

Kept here because it explains how TOR is now configured. `creatorrc.py --speetor` writes
`tor_config.txt` into the current directory, and the image's WORKDIR is not writable by the
`aria2` user, so every start failed with `PermissionError: [Errno 13] Permission denied` and
the `||` fallback quietly started TOR on stock defaults. The guard and exit selection the
project pulls this script in for had never been applied.

It now runs from a writable directory. Because generating takes about 80 seconds — it
downloads the full relay descriptor set — an existing `/conf/torrc` is reused for a week;
delete it or run `just clean` to force a fresh one. The generated file sets `EntryNodes`,
`ExcludeNodes` and `ExitNodes` and deliberately no `SocksPort`, so TOR stays on
`127.0.0.1:9050` where v2ray expects it. `StrictNodes` is not set, so the 4000-relay
`ExcludeNodes` list does not prevent hidden services from resolving — verified by fetching a
`.onion` through the chain after the change.

#### 13. The move step in `done.sh` was one-shot and unverified (fixed)

It moved files with a bare `mv`: no success check, and a name collision would silently
overwrite whichever file was already there. It was also guarded by a one-shot `/files/extract`
flag, so a file that failed to move, or simply landed late, was never picked up again. (Note
the naming, unrelated to the fix: `deis/done.sh` moves the files, `deis/download.sh` polls
aria2 — the two read as if they were swapped.)

The guard is now "not yet fully moved" rather than one-shot, so it retries every cron tick
until every file is actually across. A collision renames to `name-dup2.ext` (`-dup3`, ...)
rather than overwriting or skipping — skipping would leave that file stuck forever and stall
everything downstream. A failed `mv` is logged and retried next tick like an unmoved file.
Verified in isolation against the built image: a normal move, a colliding filename resolved
to `-dup2`, and — with `/files` bind-mounted read-only to force a real failure — the file
logged and left in place with no `/files/unpack` created, completing correctly once writable
again.

### E — Extract (fixed)

#### 14. Nested extraction was two fixed passes, not recursion to a fixpoint (fixed)

Pass 1 handled `/files/*`; pass 2 walked `/extracted/files` once. Anything three levels deep
(zip → zip → pst) was only picked up by accident of `find` traversal order while the same run
was still writing files - undocumented and non-deterministic.

`unpack/start.sh` is now one recursive loop: extract a round of files, queue whatever came
out of them for the next round, stop when a round produces nothing new or `max_depth` rounds
have run (`deis.cfg`, default 6 - past that, files are left as-is and logged as such rather
than silently dropped). Verified with an archive nested four levels deep through mixed
formats (zip → zip → tar → plain file): the deepest file's actual content came back correctly
after four rounds. A deliberately 8-level-deep archive stopped exactly at round 6 with a
logged `DEPTH-LIMIT` line naming what was left unprocessed, confirming the cap is both real
and visible rather than a silent truncation.

#### 15. Archive detection was a fixed extension list (fixed)

Pass 2 only matched `zip|gz|7z|gzip`, so `.rar`, `.tar`, `.tgz`, `.bz2` and `.xz` were
excluded - a `.rar` inside a `.zip` was never extracted even though 7-Zip handles it fine and
the top-level pass did too.

Detection is now "attempt extraction and see", not an extension check at all - which also
folds this into item 14's unified loop rather than needing a separate content-sniffing step.
Verified: a `.tar` nested inside a `.zip` (excluded by the old pass-2 list) was found and
extracted in the same test as item 14.

#### 16. `ZIP_PASSWORD` was silently ignored on nested archives (fixed)

Now folded into item 17 below, since both were fixed by the same rewrite: password handling
is the same code path at every nesting level, so there is no longer a "pass 2" for it to be
missing from. Verified: a password-protected archive nested inside another archive extracted
correctly using `ZIP_PASSWORD`.

#### 17. "Wrong password", "corrupt" and "not an archive" were indistinguishable (fixed)

All three used to collapse to the same fallback and the same misleading log line,
`"Not an archive, copying as-is"`. `unpack/start.sh` now checks 7-Zip's actual error text
(its exit code alone does not distinguish these) and logs one of three outcomes -
`ENCRYPTED`, `CORRUPT`, or `COPIED` (not an archive) - to `logs/unpack.log`.

Also added, both requested alongside this item: a password **list** rather than one global
password - `passwords/*.txt`, one per line, tried in order (unencrypted first) at every
nesting level and deduplicated against `ZIP_PASSWORD` - and a record of what remains
encrypted after every candidate fails, `extracted/still_encrypted.txt`, so those archives can
be revisited rather than silently lost among successfully extracted files.

Verified end-to-end against a purpose-built fixture covering all of 14-17 together: a
password only present in `passwords.txt` (not `ZIP_PASSWORD`) extracted correctly after two
wrong guesses; a genuinely corrupt (truncated) archive was logged as `CORRUPT` and a plain
text file with a misleading `.zip.txt.pdf` name was logged as "not an archive" - both
distinctly, not conflated; an archive with no working password anywhere was logged as
`ENCRYPTED` and listed in `still_encrypted.txt`; duplicate top-level archives with identical
content were extracted once but both still received their configured disposition
(`zip_archive`/`zip_remove`); and none of the eleven marker files `deis/*.sh` and
`web/startup.sh` drop into `/files` (`added_urls`, `batch_gids`, `batch_started`, `dies_done`,
`done`, `download_failed`, `downloaded`, `extract`, `pending_count`, `running`, `unpack`) were
ever picked up as if they were leak data - the exclude list had drifted out of sync with two
newer marker files added by earlier P0 fixes and was corrected as part of this change.

One correctness bug was caught and fixed during testing rather than shipped: a file found
already correctly placed inside a parent archive's extraction directory does not need to be
copied again into `/extracted/files` itself - doing so would reintroduce the exact
cross-archive filename collision that namespacing extractions by the archive's own sha256
exists to prevent. The first draft did this for every nested leaf file; the fix checks
whether a path is already under `/extracted/files/` before copying.

Also, as a direct and necessary consequence of unifying top-level and nested handling into
one code path: `zip_archive`/`zip_remove`/`pst_archive`/`pst_remove` now apply uniformly to
every successful extraction, top-level or nested. Previously only nested archives were ever
moved or removed after extraction - top-level originals in `/files` were left in place
forever regardless of configuration, which was an inconsistency rather than a deliberate
choice. With the default `deis.cfg` (`zip_archive=true`), successfully extracted top-level
originals now also move to `extracted/archive/`, where they did not before. `unzip=true` is
no longer read (there is only one pass now, so a separate toggle for a second one is
meaningless) and has been dropped from `deis.cfg.default`; a `pst=true`/`false` toggle to
skip PST extraction entirely, which the pre-rewrite code had, is preserved.

#### 18. No guards on hostile archives (fixed)

No zip-slip check on extracted paths, no expansion-ratio or output-size cap, no disk-space
check, no timeout on `7zz` or `readpst`. A decompression bomb could fill the volume and a
hung archive could stall the serial pipeline indefinitely; `web/app.py` already had careful
path-traversal defenses, but the extract stage - the part actually handling
adversary-supplied archives - had none.

`unpack/start.sh`'s `check_archive_safety()` runs `7zz l -slt` against an archive before ever
attempting extraction and rejects it outright (`extraction_status: unsafe`, listed in the new
`extracted/still_unsafe.txt`, left as-is unextracted) if: any entry's path is absolute or
contains a `..` component (zip-slip); the archive's declared uncompressed size exceeds
`max_extract_bytes` (`deis.cfg`'s `[unpack]` section, default 10 GiB); its compression ratio
exceeds `max_compression_ratio` (default 200x - the shape of a decompression bomb); or there
isn't enough free disk space to hold it. `try_extract()` and `process_pst()` are both wrapped
in `timeout "${extract_timeout}"` (default 1800s) around the actual `7zz x`/`readpst` call, so
one hung archive can no longer stall a worker - and, once every worker is hung the same way,
the whole round-based pipeline - forever; a killed extraction falls into the existing
`corrupt` classification and its `rm -rf` cleanup (item 34 below), so a timed-out run leaves
no partial output behind either.

Verified against purpose-built fixture archives, not the live pipeline's real data
(deliberately never fed a hostile archive into the real `/files` or `/extracted` used by the
running stack - fixtures were run through isolated volume overrides instead): a zip-slip zip
(`../../../tmp/evil.txt`) and an absolute-path variant were both rejected before extraction; a
50 MB-of-zeros zip (1026x compression ratio) was rejected for exceeding the ratio cap; a
normal small zip extracted and recursed correctly in the same run. The timeout wrapper was
verified directly against a deliberately hung command (`timeout 2 <a process that sleeps 30>`
returns exit 124 after ~2s, exactly the mechanism now wrapping `7zz`/`readpst`).

One real bug was caught by this testing, not by reading the code: the three new
`*_DEFAULT` config constants were plain (non-exported) shell variables, invisible inside the
`xargs -P` worker subprocesses that actually call `check_archive_safety()` - every archive was
being rejected with an empty (so arithmetically zero) size cap until this was found by running
the guard against a real archive and getting "would extract to 18 bytes, over the -byte cap"
instead of the expected pass. Fixed by exporting the three constants.

#### 19. Extraction was fully serial (fixed)

`ingest.py` used a `ProcessPoolExecutor`, but `unpack/start.sh` processed one archive at a
time with no parallelism, no progress bar, no timestamps and no log file - the timestamps
and log file were already fixed by items 14-17 (`logs/unpack.log`); parallelism is fixed here.

Each round (see item 14) now dedupes by content first - files sharing a sha256 within the
same round are grouped, and only one representative per group ("primary") actually runs
7-Zip/readpst, since two files with identical bytes are guaranteed to succeed or fail
identically, so extracting both is wasted work at best and, run truly concurrently, a real
race (two processes writing into the same destination directory at once). Primaries are then
dispatched to up to `PARALLELISM` (`.env`, default: CPU cores available to the container)
parallel workers via `xargs -P`; every duplicate is handled afterward by replaying its
primary's already-known outcome rather than repeating the attempt.

Workers are genuinely separate processes (spawned by `xargs -P`, not backgrounded subshells),
so they cannot share this script's in-memory state directly - passwords are written to a file
once and read back per worker, and each worker's newly-discovered files are written to its
own output file (`$$`-named) and merged by the parent once the round's whole batch completes.

Verified with a stress fixture designed to try to trigger exactly the races this design
avoids: 40 distinct archives plus 6 clusters of 5 identical duplicates each (70 files, 46
unique contents) dispatched in one round at `PARALLELISM=16`. Every one of the 46 pieces of
content came back byte-correct with no cross-contamination between concurrent workers'
output, exactly 46 "Extracted" log lines (no duplicate re-extracted its content), and all 70
originals correctly disposed of (30 duplicates disambiguated via the same `-dup2` mechanism
as item 17, since two unrelated originals can share a basename here too). The full 14-17
regression fixture, including the embedded-newline filename and cross-round duplicate-name
collision cases, was re-run against the parallel implementation with no change in outcome.

#### 20. Fragile shell mechanics in `unpack/start.sh` (fixed)

Three problems: config was read with substring matching (`grep "^unpack=true" /deis.cfg`),
not section-aware and would false-positive on a key like `not_unpack=true`; file discovery
was inconsistently null-delimited; and a failed extraction could leave an empty sha256-named
directory behind. The latter two were fixed as a side effect of the 14-17 rewrite. Config
reading is now genuinely section-aware, via a small `awk` reader scoped to `[unpack]` -
verified with a decoy `deis.cfg` containing `unpack=true` under `[ingest]` and `[other]`
sections and a `not_unpack=true` key under `[unpack]` itself: `config_true unpack` correctly
read `false` from the right section, unfooled by either trap.

Correction: the third problem was described here as fixed by the 14-17 rewrite (`rmdir`
cleaning up the speculative directory on failure), but that was only verified for the *empty*
case. Item 34's testing found the real, worse case: `rmdir` only removes empty directories,
and 7-Zip can leave partial output behind even on a reported failure - so a non-empty leftover
silently survived. Actually fixed (`rmdir` -> `rm -rf`) under item 34, not here.

#### 21. More extractors - OCR only (fixed; the rest of item 21 is still open)

Only the OCR bullet, the one this item called "the largest recall gap in the tool" - a scanned
passport or invoice saved as a plain image indexed with `content_length: 0` and was invisible
to every content search. The rest of item 21 (email formats beyond PST, structured data as
rows, disk/VM images, mobile backups, encrypted-archive listing) is unrelated work, still open.

Deliberately **not** done via Elasticsearch's own `ingest-attachment`/Tika pipeline, despite
that being the more "native" place for it. Researched first, not assumed: enabling Tesseract
inside Tika's embedded copy there is unsupported and, per multiple Elastic forum/GitHub
threads, requires disabling Elasticsearch's seccomp sandboxing entirely just so Tika can fork
a `tesseract` subprocess - and even then, several reports describe it not working reliably.
That's a real security regression (the ES process's own syscall sandbox, disabled cluster-wide)
for an unreliable result, not a trade worth making.

Instead, OCR runs where 7-Zip/`readpst` already run without any sandboxing concern: inside
`unpack`. `unpack/start.sh`'s `maybe_ocr()` runs after a file is determined not to be an
archive (the same `not-archive` outcome a plain image already produced) - if `file
--mime-type` says `image/*`, `tesseract` runs against it (`deis.cfg`'s `ocr`/`ocr_languages`,
default `eng`; `tesseract-ocr-eng`/`swe` are installed in the `unpack` image, matching this
project's existing English/Swedish precedent from the notebook's word cloud).
Found text is written to a `<name>.ocr.txt` sidecar next to the image - a **separate**
document, not merged into the original image's own Elasticsearch document: merging would mean
teaching `ingest.py` about a second text source for one document, touching the already
crash-safety-tuned bulk/marker flow items 1 and 41 depend on, for content that has nothing to
do with the sha256-confirmed-before-marked guarantee that flow provides. The sidecar is picked
up by `ingest.py`'s existing directory walk exactly like any other file, with its own sha256,
so it needs no changes there at all. Scoped to image files only for now - scanned PDFs (a
whole-page image with no text layer) would need PDF rasterization tooling first, a real gap
left open rather than attempted here.

Verified against the live stack (not just `tesseract --version`): a real 700x220 PNG rendered
with three lines of Arial text ("CONFIDENTIAL DOCUMENT", a personnummer, an email address) was
run through the actual `unpack/start.sh` flow via isolated volume overrides (not the live
pipeline's real `/files`/`/extracted`) - `not-archive` was correctly logged for the image, an
`[OCR]` line followed, and the resulting `scan.png.ocr.txt` sidecar's content matched the
rendered text exactly, including the personnummer and email (both of which item 31's detectors
would in turn find in it, once ingested). Confirmed `ocr=false` produces zero `.ocr.txt` files
and zero `[OCR]` log lines against the same fixture; confirmed a plain (non-image) text file
alongside the same image produces no sidecar for itself while the image still gets one,
confirming the mime-type gate is doing its job rather than OCR running unconditionally on
every non-archive file. The dashboard gained an "OCR-extracted text" saved search
(`filename: *.ocr.txt`).

### I — Ingest (fixed)

#### 22. Define an explicit index template instead of relying on dynamic mapping (fixed)

The mapping was entirely dynamic. `setup/entrypoint.sh`'s `leakdata` index template (already
existed for the `top_folder` runtime field) now also declares `sha256` and `filename` as
`keyword` - a hash analyzed as English prose was wasted work and tokenized oddly - drops
`attachment.content`'s unused `.keyword` multi-field, and sets `index.mapping.total_fields.limit`
(2000), `highlight.max_analyzed_offset` and `number_of_replicas: 0` as template defaults.

Elasticsearch cannot change an already-mapped field's type in place without a reindex, so the
`sha256`/`filename` retyping and dropping `attachment.content.keyword` only take effect on an
index created **after** this template exists, not on one that predates it - which is why these
two were also backfilled/applied directly onto the *existing* index below, for the instance
this was developed against. That instance-specific gap isn't a design problem, though: DEIS is
one instance per leak, and the Elasticsearch data volume gets wiped and the stack recreated
for each new leak (confirmed against this project's actual usage - the live instance had
already been through this at least ten times). On any freshly recreated instance,
`setup/entrypoint.sh` creates this template before `ingest.py` ever runs, so `leakdata-index-000001`
is auto-created from it on first ingest and gets the correct mapping from the start - no
rollover or alias needed to make that happen (see "Deliberately not doing" for why item 23,
proposed for exactly that, isn't being done). The settings (replicas, total_fields.limit,
max_analyzed_offset) are dynamic and were also applied directly to the existing index, which
does take effect immediately - the cluster went from `yellow` (one permanently unassigned
replica shard, no benefit on a single node) to `green`, and README's manual Dev Tools step for
`max_analyzed_offset` is gone.

Verified: Elasticsearch's own `_index_template/_simulate_index` API confirms a new index would
get exactly the intended mapping and settings; the live index's settings were confirmed
applied and cluster health confirmed `green`; the `top_folder` runtime field (whose script
reads `filename.keyword` on the live index, but `filename` directly in the template, since the
two now have different types) still resolves correctly on both; and a full ingest run against
the live, newly-templated index completed with no change in behavior.

#### 24. Use the `_bulk` API (fixed)

Every file was a separate `PUT /_doc/<sha>` from a worker process. `ingest.py` now splits
hashing/reading (still CPU/IO-parallel across worker processes, unchanged) from sending: a
worker's prepared file is queued in the main process and flushed as one `_bulk` request once a
batch reaches 200 documents or 20 MB, whichever comes first. The crash-safety property from
item 1 (a file's sha256 marker is written only once Elasticsearch actually confirms it) is
preserved per-document, driven by each item's own status in the bulk response, not the whole
batch's.

One real cost, worth knowing: the `_bulk` endpoint only accepts JSON, not the CBOR the
single-document `PUT` used to carry raw file bytes with (`_bulk` rejects
`Content-Type: application/cbor` outright) - confirmed against the live cluster before
committing to this design. File content is base64-encoded into the JSON body instead, which
the ingest attachment processor still decodes correctly (also confirmed live), at the cost of
~33% more bytes on the wire for the content field specifically. Given this corpus's realistic
file sizes (a few hundred KB on average), that trade is worth it for the reduction in HTTP
round-trips. The now-unused `cbor2` dependency is removed from `ingest/pyproject.toml`.

Verified against the live stack, not just in isolation: a normal full run (nothing to do)
reconciles exactly as before; removing 5 markers and re-running sent them through one real
bulk batch, each recovering the correct filename via the (now also fixed, item 27) sqlite
lookup path, each attachment correctly Tika-parsed via the base64-encoded content; blocking
writes on the index mid-batch correctly failed every item in that batch with no markers
created and exit code 1, and unblocking and retrying recovered cleanly; and removing 250
markers (forcing at least two separate `_bulk` batches, since that exceeds the 200-document
cap) reconciled back to the correct count with zero failures.

One consequence surfaced by that last test, worth recording honestly: item 41's redundant
duplicate-content sends got measurably more pronounced under this change, not less. Deferring
every marker write until an entire batch (up to 200 documents) flushes, instead of after each
individual file's own request, widens the race window duplicate copies of the same content can
fall into before any of them is marked done - observed directly: removing 250 markers (with
124 known duplicate copies in this corpus) produced 366 bulk index operations for what should
have been 250 unique ones, rather than the much smaller overcounts item 41 originally
documented. Still fully correct - same document id, idempotent overwrite, final counts and
content all verified correct - purely wasted work, but this raises the case for actually
implementing item 41's suggested fix (an atomic claim-before-send marker) rather than leaving
it as an accepted trade-off.

#### 27. `use_sqlite=True` was broken in the container (fixed)

`sqlite3.connect("db/file_hashes.db")` resolved to `/db/file_hashes.db` because the working
directory is `/`, and the `ingest` service mounted no such volume, so it failed immediately.
`bin/pathfix.py`, the only thing that ever populates the database, also used a different
relative path (`file_hashes.db`, relative to wherever it happened to be invoked from), so
even fixing the container mount alone would not have made the two agree.

`docker-compose.yml` now mounts `./db:/db` on the `ingest` service, and `bin/pathfix.py`'s
`DB_PATH` is `db/file_hashes.db` - both relative to the repo root, which is where
`ingest.py`'s container cwd (`/`) and `pathfix.py`'s expected invocation (`.venv/bin/python3
bin/pathfix.py ...` from the repo root) now agree. Verified end to end: ran `pathfix.py`
against a file named `Original Name With Spaces.txt`, then ran `ingest.py` with
`use_sqlite=True` pointed at the resulting sha-bucketed tree, and confirmed the indexed
document's `filename` field is the original name recovered from the database, not the
sha-based path on disk - the feature's actual purpose, which had never worked before.

#### 28. `bin/pathfix.py` read whole files into memory (fixed)

`compute_sha256` called `f.read()` on the entire file, which would OOM on any large archive
or disk image. Now streams in 64 KB blocks, the same technique `ingest.py` already used (it
uses a smaller 4 KB block size; pathfix.py's files are typically larger, so a bigger block
trades a little more peak memory for fewer read() calls).

#### 29. Dead ingest pipeline (fixed) - the rest of this item was a mischaracterization

The `attachment` pipeline, created by `setup/entrypoint.sh` but never referenced (only
`cbor-attachment` is used - `ingest.py` posts CBOR, not JSON, so the JSON-only `attachment`
pipeline was never reachable regardless), is removed.

The other two things this item called stale boilerplate are not: `kibana/config/kibana.yml`'s
Fleet and Elastic Agent configuration block is the documented pre-configuration for the
optional `extensions/fleet/` overlay - that extension's own README explicitly points at this
exact file for its pre-configured Agent Policy. And `setup/export.ndjson`'s `system-*` and
`elastic_agent-*` saved objects follow Elastic's own ID-prefixing convention for the stock
dashboards Filebeat/Metricbeat's `system` module and the Fleet `elastic_agent` integration
ship with - consistent with this repo's `extensions/filebeat/`, `extensions/metricbeat/` and
`extensions/fleet/` overlays, not orphaned cruft. Left alone; deleting either would have
broken a real, documented, optional feature. (Compare item 9, where a similar
looks-dead-but-isn't read turned out to be wrong in the same way, and was corrected the same
way: verify before deleting, and correct the finding when it turns out to be wrong.)

#### 30. `README.md` referenced `./bin/ingest.sh`, which does not exist (fixed)

The documented "only run ingest" path was broken. Replaced with
`.venv/bin/python3 ingest/ingest.py`, which matches how `just ingest` already prepares the
venv and how `bin/progress.py` is invoked elsewhere in the same README. Verified: run from
the repo root with `ELASTIC_PASSWORD` exported, it connects to `127.0.0.1:9200` (ingest.py's
own host/container detection) and completes a normal run against the live stack.

#### 41. Identical files could be indexed multiple times on first ingest (fixed)

A consequence of the fix for item 1: the marker was only written after Elasticsearch
confirmed, so multiple copies of a file that had never been ingested could all be sent before
any of their markers existed. Harmless - the document id is the sha256, so it was the same
document written repeatedly - but it wasted an upload and a Tika parse per collision. Item
24's bulk batching widened the window rather than narrowing it: a marker was only written
once an entire batch (up to 200 documents) flushed, not after each file's own request, so
more duplicate copies had time to pile up as "not yet marked" first. Measured before this
fix: 4 identical copies produced 3 redundant sends before item 24; after item 24,
deliberately removing 250 markers from a corpus with 124 known duplicate pairs produced 366
index operations for 250 unique files.

Fixed differently than originally planned here (an `O_EXCL`-claimed marker file, promoted on
success, removed on failure) - that design still needs its own crash-recovery story for a
claim left behind by a killed process, which is real added complexity. Simpler and
sufficient: `ProcessPoolExecutor.map()` is documented to yield results in input order, not
completion order (confirmed directly, since this was the one assumption load-bearing enough
to verify rather than trust: a small reproduction script run inside the actual container
image), so `process_files()`'s single-threaded consumer loop can deduplicate by sha256 as
results arrive, entirely in-memory, with no locking and nothing written to disk that could be
left in a bad state by a crash. The first file with a given sha256 in a run is queued
normally; every later one is deferred and resolved afterward against whatever the first
one's outcome turned out to be, without a second upload.

Getting this verified took an extra round: the first two container test runs still showed
the old duplicate counts even after rebuilding the image, which briefly looked like the fix
itself was wrong. It wasn't - `docker compose restart` reuses whatever container already
exists rather than recreating it from a freshly built image, so those two runs were silently
executing the pre-fix code. Confirmed by comparing the running container's image hash against
the newly built one (they differed) and by running the identical code manually with `docker
run` (always a fresh container), which gave the correct, expected result immediately. Redone
with `docker compose up -d --force-recreate` from there: the 4-copy case produced exactly one
`[INDEXED]` line, and the 250-marker case produced exactly 250 - both matched the fixture,
with zero duplicate sha256 values remaining in `logs/ingest.log`.

### S — Search (fixed)

#### 31. PII and personnummer detection (fixed)

The origin story of this project is "does this leak contain my friend's personal data", yet
the only way to answer it was free-text KQL. Built as a post-pass (`bin/deis pii-scan`), not
an ingest-time enrichment: detection needs the Tika-extracted text, which only exists once
Elasticsearch's own ingest pipeline has parsed a document - `ingest.py` itself only ever
handles the original file's raw bytes, so there was nothing useful to scan at ingest time
without duplicating Tika's own parsing there.

`bin/pii.py` (pure functions, no network) finds and **checksum-validates** every numeric
identifier before counting it - a bare regex-shape match would produce constant false
positives against this corpus's dense financial/numeric content (amounts, dates, account
numbers):

- Swedish personnummer/samordningsnummer, via the real Skatteverket mod-10 checksum (day+60
  identifies a samordningsnummer; the checksum formula doubles every odd 1-indexed digit from
  the left, over all 10 digits including the check digit itself, valid iff the total is a
  multiple of 10).
- IBAN, via the ISO 7064 mod-97 checksum.
- Card numbers, via the standard Luhn checksum.
- Emails and phone numbers, which have no universal checksum, so these are pattern-matched
  only (phone numbers deliberately conservative - requires a `+`-prefixed country code or a
  recognizable Swedish mobile format, not just "any long digit run", for the same false-positive
  reason as above).

Scoped to Swedish identifiers and locale-agnostic ones (email, IBAN, card number) - not other
national IDs, even though this project's actual corpus turned out to be Brazilian financial
data (see item 32's language-detection write-up) where a Brazilian CPF detector would have
found real matches. Deliberately not added: this project's own scope is Swedish personal data
("does this leak contain my friend's personal data", where "friend" means Sweden), and adding
detectors for every locale a leak might happen to be in is unbounded scope creep the item
never actually asked for by name - "national IDs for other locales" was the item's own
speculative extension, not the core ask.

All-same-digit numbers (`0000000000000000`, ...) are explicitly excluded even when they'd
otherwise pass a checksum, since they're common placeholders, never a real identifier. Results
land in each document's `pii` field (`personnummer`/`emails`/`phone_numbers`/`ibans`/
`card_numbers` as keyword arrays, plus a `has_pii` boolean) - added to the index template and
backfilled onto the live index, same pattern as `extraction_status`. Only unscanned documents
(`pii.has_pii` missing) are processed by default; `--rescan` redoes everything. The dashboard
gained a "Documents with personal identifiers" saved search (`pii.has_pii: true`).

Verified with `tests/test_pii.py` (21 tests): personnummer fixtures are constructed by
computing the correct check digit ourselves (`compute_personnummer_check_digit`) rather than
trusting a memorized "real" example number, so a mistaken memory can't silently make a test
meaningless; IBAN/card-number fixtures use widely published standard test/example values
(Wikipedia's IBAN example, the ubiquitous Visa test card number) rather than anything real.
Verified against the live stack: `pii-scan` against the real 273-document corpus found 12
documents with at least one identifier (20 card numbers, 5 emails, 1 personnummer), re-running
with no flag scanned 0 documents (already tagged - confirmed idempotent), and `--rescan`
reproduced the identical breakdown. Spot-checked individual hits rather than trusting the
aggregate count: the card-number matches were 14-digit codes in a spreadsheet named for
Brazil's Alelo corporate meal-card benefit system (`ALELO REFEIÇÃO`) - a genuine-looking
identifier, not coincidence, even though it isn't the kind of identifier this detector set was
built for. The single personnummer hit, by contrast, was inside a Brazilian boleto (payment
slip) PDF dense with barcode/numeric codes - plausibly a coincidental checksum pass rather than
a real Swedish personnummer showing up in Portuguese-language data, which is exactly the class
of false positive a 1-in-10 checksum can't fully rule out and is called out as a known
limitation in the README rather than glossed over.

#### 32. Language detection (fixed; entity extraction is unrelated work, still open)

Only the language-detection half of this item - see item 32 above (in "Open items") for why
entity extraction (names/organisations/locations) is unrelated work, deliberately not
attempted here.

Scoped to Swedish and English, the two languages this project actually cares about (see item
31's PII write-up for the same scoping decision), not general-purpose language ID - even
though the live corpus turned out to be Brazilian Portuguese financial data (see verification
below), which a from-scratch design might have added Portuguese support for. Deliberately not
done: that would be scope creep past what this project is actually for.

Implemented ingest-time, not as a post-pass like items 25/31: added as a third processor in
the `cbor-attachment` ingest pipeline, right after the existing `attachment`/`date`
processors, so every future document gets tagged automatically with no separate scan step
needed. A stopword-frequency heuristic (Painless, ~10 distinctive words per language - "the",
"and", "was" for English; "och", "det", "har" for Swedish - counted via plain substring
`contains(" word ")` checks, deliberately not regex: Painless disables regex by default behind
a cluster-wide `script.painless.regex.enabled` setting, and substring checks on space-padded
content are simple enough here not to need it), whichever language scores highest wins (a tie,
or both under a threshold of 3, resolves to `unknown` - correctly the common case for this
corpus's numeric/tabular financial content, and for non-English/Swedish text generally, which
is expected given the intentional scope). `attachment.content` also gained two per-language
analyzed sub-fields (`.english`/`.swedish`, using Elasticsearch's own built-in `english`/
`swedish` stemming analyzers) - these don't need language detection at all, since both are
always computed regardless of which language a given document turns out to be, trading some
extra indexing cost for not having to get detection right before indexing. Both added to the
index template and backfilled onto the live index/existing documents (`_mapping` PUT for the
new fields, `_update_by_query` running the identical detection logic for the `language` field
itself, both mirroring the `extraction_status` precedent). The notebook's word cloud cell now
calls `detect_corpus_languages()` (a terms aggregation on `language`, languages present in at
least 3 documents, "+"-joined, falling back to English) instead of a hardcoded
`wordcloud_language`. The dashboard gained a "Language" donut panel, cloned from the existing
"Extraction status" one.

Verified via Elasticsearch's own `_ingest/pipeline/_simulate` API before ever touching the
live pipeline (the same category of tool as `_index_template/_simulate_index`, used
previously for item 22): English and Swedish sample text each correctly identified, a
Portuguese sample correctly fell through to `unknown` (confirming the scope decision actually
takes effect, not just that Portuguese support was never written), and - caught by this
testing, not by reading the code - a document with no `attachment.content` at all (a real,
common case: files Tika extracts nothing from) threw a Painless `NullPointerException` in the
first version, fixed with an explicit null guard before it ever reached the live cluster.
Verified against the live stack: the real pipeline and both backfills ran cleanly through
`setup`, the mapping shows `language` and the two analyzer sub-fields correctly, and the real
corpus backfilled to 272 `unknown` / 1 `english` - consistent with this corpus being almost
entirely Portuguese-language financial data (invisible to English/Swedish detection by design)
plus numeric/tabular content, matching what item 31's PII findings already suggested about its
Brazilian origin. The notebook was executed end-to-end in the live kernel (`jupyter nbconvert
--execute`, matching item 35's verification standard): zero errors across every cell, and the
word cloud's actual rendered output (a 440 KB base64 PNG, extracted from the executed
notebook's saved widget state) confirmed `detect_corpus_languages()` drove real behavior (here,
correctly falling back to English, since 1 document is below the 3-document threshold) rather
than just returning without exception. A full `ingest.py` run against the live stack afterward
reconciled to the same 273/0/0 counts as before, confirming the new pipeline processor doesn't
disturb ingest.

#### 34. Surface the failure states in the dashboard (fixed)

This needed more than a panel: `unpack.log` and `still_encrypted.txt` are plain files on
disk, not in Elasticsearch, so Kibana had nothing to show a panel from - there was no
Kibana-visible signal for "still encrypted" or "corrupt" at all before this.

`unpack/start.sh` now also writes `extracted/still_corrupt.txt` (sha256 per line, mirroring
`still_encrypted.txt`), and `ingest.py` tags every document with an `extraction_status` field
(`ok`/`encrypted`/`corrupt`/`unsafe`, the last added by item 18) by checking a file's sha256
against those lists at ingest time - a small, surgical addition that keeps everything in the
existing `leakdata-*` index rather than standing up a second one (`unpack` has no network
route to Elasticsearch at all, so posting from unpack directly was not an option without
wiring that up too). The dashboard gained an "Extraction status" donut and a "Files needing
attention" saved search (`extraction_status: (encrypted or corrupt or unsafe)`). Documents
indexed before this field existed are backfilled to `ok` by `setup/entrypoint.sh`, the same
pattern already used for `top_folder`. Zero-content files (`attachment.content_length: 0`)
already had a saved search; still true, and distinct from this - a file can have zero content
for reasons unrelated to unpack ever flagging it (an image, a format Tika doesn't parse).

Testing this surfaced a real, unrelated bug from the item 14-17 work, fixed here rather than
carried forward: on a failed extraction, cleanup used `rmdir`, which only removes *empty*
directories - but 7-Zip can leave partial output behind even on a reported failure (observed
directly: a wrong-password attempt left a 0-byte placeholder; a deliberately truncated
"corrupt" archive left the *complete, correct* file content, recovered from the still-intact
early part of the archive despite the reported error). `rmdir` silently no-opped on this
non-empty leftover, so the untrusted fragment survived alongside the safe fallback copy of
the original - meaning it could be picked up and indexed as if it were a second, separate
file. Changed to `rm -rf`, which cannot fail this way; verified the leftover fragment is now
correctly gone in both cases.

Ingest reconciliation counts (item 25) were also finished as part of closing this item out:
`ingest.py`'s `print_summary()` now indexes one document per run into a new `deis-ingest-runs`
index (`index_run_summary()`) - files looked at, unique files, duplicate copies, indexed this
run, already indexed, failed, and the live Elasticsearch document count - instead of only
printing them to the container's own stdout. A new `deis-ingest-runs*` index template
(`@timestamp` as an explicit `date` field) and an "Ingest run history" saved search
(sorted by `@timestamp` desc) were added to the "Leaked data" dashboard, matching this item's
existing saved-search pattern. Best-effort by design: a failure to index the summary is
logged but does not fail an otherwise-successful run. Verified against the live stack: a real
`ingest.py` run produced exactly one new `deis-ingest-runs` document with the expected counts
(399 files looked at, 273 already indexed, 0 failed, 273 in Elasticsearch), and the saved
search/data view both imported correctly via `setup`'s Kibana import.

#### 35. Notebook hardening (fixed)

The Elasticsearch password was embedded in the connection URL string; now passed via the
client's `basic_auth` parameter instead, so it no longer ends up in that process's URL string
or connection logs. The index name was hardcoded (and duplicated the one in `ingest.py`,
which was the actual complaint - not that a literal string appeared once, but that it could
drift from ingest.py's without anyone noticing); now a single `INDEX` constant, referenced by
every cell that queries Elasticsearch.

The word cloud pulled the entire corpus into Python with `match_all` and `size=10000` -
fine at a few hundred documents, fatal at scale, exactly as this item said. `significant_text`
(suggested here) turned out to be the wrong tool, confirmed by testing against the live
cluster before committing to an approach: it scores terms by how much more they appear in a
foreground subset than a background sample, and against an unfiltered `match_all` query the
two are identical by construction, so nothing ever scored as significant - empty buckets
every time, regardless of `min_doc_count`. Used a plain `terms` aggregation on
`attachment.content` instead (the other option this item named), which needs `fielddata`
enabled on that field - added to the index template and backfilled onto the live index, with
the memory trade-off noted in `setup/entrypoint.sh`: it scales with vocabulary size, not
corpus size, a much smaller bound than pulling every document's content client-side.

Verified by executing the whole notebook end to end in the live kernel (`jupyter nbconvert
--execute` against the running notebook container, not just reading the diff): every cell
completed with no errors, the document search cell correctly returned real hits using the
new `INDEX` constant and `basic_auth` connection, and a diagnostic cell confirmed the
word-cloud path produced 891 real term/count pairs and a genuine rendered PNG - not merely
"no exception," but the actual expected output.

### CLI (fixed)

Running DEIS meant editing `.env`/`deis.cfg` by hand, chaining `docker compose --profile X up
-d` incantations from memory, tailing `docker logs deis-setup-1 -f`, and - when something was
wrong - knowing to check four different marker-file directories or paste JSON into Kibana's
Dev Tools console. `bin/deis.py` (stdlib `argparse` + `rich`, matching this project's
minimize-dependencies posture; no new dependency needed since `rich` was already a `bin`
dependency) wraps the existing scripts rather than replacing them, invoked via a thin
executable wrapper, `bin/deis` (named to avoid colliding with the existing `deis/` service
directory at the repo root):

- `deis init` - bootstraps `.env` (every `changeme` replaced with a real generated secret) and
  `deis.cfg` from their `.default` templates if they don't already exist (idempotent -
  verified it leaves both alone untouched on a second run), and checks Docker's configured
  memory against the README's 18 GB guidance.
- `deis doctor` - Docker reachability, Elasticsearch/Kibana health, and any container in a
  genuinely concerning state (`restarting`, or `exited` with a non-zero exit code - **not**
  simply `exited`, since `setup`/`unpack`/`ingest` are one-shot containers that exit 0 when
  their work is done; the first version of this check flagged those as errors on every run
  until caught by testing against the live stack, not by reading the code). Does not implement
  a TOR egress leak test - that's item 42, not built yet - and says so explicitly.
- `deis run [--only {download,extract,ingest}]` - maps directly onto existing compose
  profiles (no compose changes needed): no `--only` → `--profile deis`; `--only download` →
  `--profile download`; `--only extract` → `--profile unpack`; `--only ingest` →
  `--profile ingest`.
- `deis status` - a one-shot snapshot of marker-file stage status plus funnel counts (files
  downloaded, files extracted, unique sha256, Elasticsearch document count, failed count from
  the latest `deis-ingest-runs` document - item 25's index, now useful outside Kibana too).
  Deliberately doesn't duplicate `bin/progress.py`'s live-updating loop (still `just
  progress`'s job); adds the counts that didn't have anywhere to live before.
- `deis search <term>` - the same `match` query on `attachment.content` the notebook runs,
  rendered as a table with each hit's filename and `/view/<sha256>` link.
- `deis report` - the latest ingest run's full breakdown plus counts from
  `still_encrypted.txt`/`still_corrupt.txt`/`still_unsafe.txt` - a text rendering of what item
  34's dashboard panels already show, for someone who never opens Kibana.
- `deis add-urls <url-or-file>` - validates scheme (the same allowlist `deis/urls.sh` already
  enforces) before ever queueing, so a bad URL is rejected immediately with a reason instead of
  only surfacing later in `logs/download_errors.log`; deduplicates against what's already
  queued.
- `deis clean` / `deis reset` - confirmation-gated (`y/N`, refuses on anything but `yes`)
  wrappers around `just clean` / `just dist-clean`, since both delete evidence.
- `deis completion {bash,zsh}` - prints a shell completion script for the commands above,
  hand-written (not `argcomplete`-generated - avoids a new dependency for ~9 fixed words) and
  kept in sync with `build_parser()` by a single `SUBCOMMANDS` tuple both read from, checked
  by a test that fails if the two ever diverge.

Verified against the live stack for everything that needs one: `doctor` correctly
distinguished the always-on `deis` orchestrator container (legitimately `exited`) from the
one-shot containers that had simply finished; `status`/`report` matched the numbers already
confirmed under "Live reconciliation" (273 unique, 273 in Elasticsearch, 0 failed); `search
megawork` returned 269 real hits with correct filenames and links (verified `megawork` was
actually a top term via the same aggregation the notebook's word cloud uses, since `search
secret` legitimately found nothing in this Portuguese-language corpus); `run --only ingest`
started exactly the right container. `add-urls` and `init` were verified against scratch
copies of `urls/`/`.env`/`deis.cfg`, not the real ones, so testing never queued a real
download or touched the real live instance's config; `clean`/`reset` were verified by
answering "n" and confirming the destructive command never actually ran. `completion bash`'s
output was `shellcheck`-clean and functionally exercised in a real bash subprocess (simulated
`COMP_WORDS`/`COMP_CWORD`, confirmed the right completions came back for the bare command and
for `run --only`); `completion zsh`'s output was confirmed syntactically valid (`zsh -n`) and
loads without error, though zsh's completion-system builtins (`_describe`/`_arguments`) aren't
practically driven end-to-end outside an interactive shell the way bash's `COMPREPLY` is.
Added `tests/test_deis_cli.py` (24 tests) for the pure logic (URL scheme validation, `.env`
parsing, marker-file status computation, and the completion scripts' consistency/syntax/
behavior) that doesn't need a live stack.

### Cross-cutting (fixed)

#### 37. No tests at all (fixed)

There was no test suite, and CI ran only super-linter and osv-scanner. Added a `tests/`
suite (pytest) covering the pure, security-relevant functions that don't need the
containerized environment: `web/app.py`'s path validation (`validate_sha256_and_get_symlink_path`,
`resolve_and_verify_target_file` - the functions already flagged by CodeQL alert #103),
`ingest/ingest.py`'s hashing and sha256-marker dedup logic (items 1 and 41 both depend on
`create_hash_link` being a true no-op the second time), `extraction_status` classification
(item 34), and `bin/pathfix.py`'s streaming sha256 (item 28). 36 tests, all passing.

`web/app.py` and `ingest/ingest.py` aren't packages, and `ingest.py` has module-level
side effects (config loading, `sqlite3.connect`, directory creation) that make them awkward
to import directly - `tests/conftest.py` loads each as a module via
`importlib.util.spec_from_file_location` and points its side effects at a `tmp_path` via
`monkeypatch`. `ingest.py`'s dedup tests substitute `ThreadPoolExecutor` for
`ProcessPoolExecutor`, since a dynamically-loaded module can't be re-imported by name in a
spawned worker process - `prepare_file()` is plain I/O with no shared state, so this doesn't
change what's under test.

Writing the `web/app.py` tests found a real gap: `validate_sha256_and_get_symlink_path` used
`re.match(r"^[a-f0-9]{64}$", sha256)`, and `re.match`'s trailing `$` also matches just before
a single trailing newline, so a hash with `\n` appended passed this check. Traced by hand
before fixing it: `basename()`/`normpath()` downstream don't strip `\n` and no real file has
one in its name, so this never actually escaped `SYMLINKS_DIR` - but it defeated the point of
an exact-format check. Fixed with `re.fullmatch(r"[a-f0-9]{64}", sha256)`, which doesn't have
this gap; verified against the live `web` container that a normal `/view/<sha256>` still
returns 200 and a hash with `%0A` appended now correctly returns 400.

Wired into `.github/workflows/tests.yml` (new): `uv sync --dev`, `ruff check`/`ruff format
--check`, then `pytest`, on every push/PR to `main`. Added `just test` to run the same
locally. Checking CI also turned up two things unrelated to this item but caught by the same
"always green" pass: the existing Linter workflow was failing on `main` independent of this
work (EDITORCONFIG/PYTHON_ISORT/PYTHON_RUFF findings, almost entirely in the vendored,
third-party `downloader/creatorrc/*.py` - now excluded from super-linter and from this
repo's own `ruff` config rather than reformatted, since they aren't ours to restyle; the rest
were pre-existing findings in files this session had touched, fixed directly), and two
CodeQL alerts (#104, #105) on `ingest.py`'s `index_url()`, which built request URLs with the
Elasticsearch password embedded (`http://elastic:<password>@host:9200/...`) - a password
embedded in a URL string can leak into an exception message, a printed error, or
`logs/ingest.log` the moment anything downstream logs or prints that URL or a `request`
exception raised against it. Fixed by moving the credential to `requests`' `auth=` parameter
instead (`elastic_auth()`), matching the approach `Elastic.ipynb` already used for the ES
Python client (item 35). Verified against the live stack: rebuilt and ran `ingest.py`
directly against the running Elasticsearch (399 files looked at, 273 already indexed, 0
failed, in Elasticsearch: 273) and confirmed `logs/ingest.log` contains no trace of the
password either way.

#### 38. Make resource limits configurable (fixed)

`ES_JAVA_OPTS: -Xms2g -Xmx16g` was hardcoded in `docker-compose.yml`, forcing the README's
18 GB Docker requirement on everyone regardless of dump size. Now
`${ES_JAVA_OPTS:--Xms2g -Xmx16g}`, overridable via `.env`, same default. Verified against the
live stack: recreating Elasticsearch with `ES_JAVA_OPTS="-Xms512m -Xmx1g"` came up healthy
with `heap.max` reporting `1gb` via `_cat/nodes`; recreating again with the default restored
16gb and confirmed the existing 273 documents survived both restarts untouched.

#### 39. Structured logging across stages (fixed for ingest; download/extract were already there)

`deis/download.sh` and `deis/urls.sh` already wrote timestamped entries to
`logs/download_errors.log`, and `unpack/start.sh` writes per-file outcomes to
`logs/unpack.log` (item 17). Ingest was the one stage with no per-file log, only its
end-of-run summary (item 25) printed to the container log. `ingest.py` now also appends to
`logs/ingest.log`: one line per file that was actually indexed or that failed, timestamped,
in the same spirit as `unpack.log` - and, matching `unpack.log`'s own convention, nothing is
logged for files that were already indexed and needed no work, so the file doesn't grow
without bound on repeat runs over an unchanged corpus. Verified against the live stack: a
clean run touched nothing in the log; removing a marker and re-running produced a real
`[INDEXED] sha256=...` line; blocking writes on the index and retrying produced real
`[FAILED] ...` lines naming the actual file paths.

Three different formats across `logs/download_errors.log`, `logs/unpack.log` and
`logs/ingest.log` remain (this item's original "one log directory, one format" ask) - a
smaller polish item now, since the substrate the CLI's `status`/`report` and item 34 depend
on exists for every stage.

#### 43. Two separate sha256 symlink trees (fixed)

`ingest.py` wrote its dedup markers to `extracted/sha256` on the host, while `web` had its
own `deis_shasum` named volume, populated independently by `web/startup.sh` rehashing every
file at startup. Two sources of truth for the same mapping, kept in sync by nothing, which is
what made the miscount described under "Live reconciliation" possible.

`web` now bind-mounts the same host directory (`./extracted/sha256:/extracted/sha256:ro`)
ingest.py already writes to, instead of the separate volume - one source of truth, read-only
from `web`'s side so a compromised `web` container cannot tamper with ingest's markers. The
`deis_shasum` volume and `web/startup.sh`'s entire rehash-on-empty block are gone; a link
served by `web` now only ever reflects what has actually been indexed, rather than
potentially including files `web` rehashed on its own before ingest ever got to them, which
is a real (if minor) correctness improvement, not just a wash. Verified: recreated `web`
against the new mount and confirmed `/view/<sha256>` still resolves and serves correctly, and
that `web` cannot write into the shared directory.

## Deliberately not doing

| Item | Why not |
|---|---|
| 12 | Integrity verification against a published checksum. Leak sites essentially never publish hashes for their dumps, so it would sit unused. `ingest.py` already computes a real sha256 from the file as ingested, which catches corruption indirectly. |
| 23 | ILM/rollover for `leakdata-*`. Solves an index growing unbounded across many cases sharing one long-lived cluster - not this project's model, which is one DEIS instance per leak, with the Elasticsearch volume wiped and the stack recreated for each new one. Item 22's mapping fix already takes effect from scratch on every freshly recreated instance without it; rollover would only add alias-management complexity (touching `ingest.py`, the notebook, and the dashboards) for a scenario that doesn't occur here. |
