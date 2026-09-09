# DEIS improvement backlog

## How to read this

An audit of all four DEIS stages plus the operator experience, written as a prioritized
backlog. Item numbers are stable: they are referenced from commit messages and from the
sections below, so fixed items keep their number rather than being renumbered away.

Open items come first, grouped by stage (D/E/I/S), then cross-cutting items, then suggested
sequencing and how to verify a fix. Fixed items are listed at the end as a quick-scan table
with the commit that fixed each one.

This file used to also carry a full prose write-up per fixed item - what was wrong, what was
done, and how it was verified - which grew to around 800 lines and made the open items, the
part anyone actually needs to act on, hard to find. Those write-ups were removed once the
work landed rather than deleted outright: each fix's own commit message carries the same
reasoning, and the write-ups themselves are still in this file's history
(`git log -p -- docs/IMPROVEMENTS.md`). Start from the commit hash in the table.

Effort is rough: **S** = an afternoon, **M** = a few days, **L** = a project.

## Context

DEIS is a docker compose pipeline for investigating ransomware leak dumps:
**D**ownload over TOR → **E**xtract archives → **I**ngest into Elasticsearch via Tika →
**S**earch in Kibana and JupyterLab. It is meant to be usable by people who are not
developers, and it handles stolen data containing other people's personal information.

Two properties should drive prioritization, because they follow from what the tool is:

1. Silent data loss is the worst failure mode. Someone asking "does this leak contain my friend's personal data?" gets a wrong answer if a file was skipped and nobody noticed.
2. The data is toxic and the operator may be a target, so defaults matter more here than in a normal side project.

## Reconciliation

There is no fixed test corpus in this repo, so absolute counts are not reproducible and are
deliberately not recorded here - they changed on every run and went stale immediately. What
should hold on any healthy run is the shape:

```text
files on disk  →  unique sha256  →  markers  →  documents in Elasticsearch
on disk but not in Elasticsearch:  0
in Elasticsearch but not on disk:  0
```

Far fewer documents than files is normal and not a loss: the document id **is** the sha256, so
identical files collapse into one document. `deis status` prints the funnel and `deis report`
the last run's breakdown; the ingest summary prints most of it at the end of every run.

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

### E — Extract

#### 21. More extractors (OCR, email beyond PST, MS Access table export, and password-cracking for individually-encrypted documents done - see "Already fixed"; the rest is still open)

Roughly in order of real-world value for leak dumps:

- **Structured data as rows rather than blobs**: `.csv`, `.xlsx`, `.sql` dumps and SQLite
  files are where personal data actually lives in these leaks. Tika flattens them to text;
  parsing them into per-record documents would turn "is my friend in here?" into a precise
  query.
- **Disk and VM images**: `.vmdk`, `.vhdx`, `.E01`, raw `.dd`.
- **Mobile backups**, `.iso`/`.wim`, mail-server maildirs.
- **dBase (`.dbf`)**: deferred alongside MS Access support (see "Already fixed") - `mdbtools`
  doesn't parse dBase at all, it's a wholly different container format, and would need its own
  tool for a much smaller file count than the `.mdb`/`.wdb` case had. Not attempted yet.

### I — Ingest

#### 46. No provenance/lineage tracking - "which download did file.txt inside archive.zip come from?"

Today a document's only location info is `filename` (`resolve_filepath()`), pointing either
at the sqlite-recorded original name or the literal extracted-tree path
(`/extracted/files/<parent-sha256>/...`) - which reveals one level of nesting by accident (the
immediate parent's sha256 as a directory name) but not the full chain back to the original
download, and isn't a structured, queryable field. Reconstructing "which URL did this ultimately
come from" today means manually walking `logs/unpack.log`'s `[EXTRACTED] ... -> ...` lines
backward, sha256 by sha256, then cross-referencing the top-level filename against aria2's own
download history (`downloader:6800`'s JSON-RPC, or AriaNg) for the URL - aria2 knows the
URL-per-file association at download time, but nothing persists or threads it forward past
`deis/download.sh`.

A real fix needs two things: (1) `deis/download.sh` (or `done.sh`) recording url→filename
before the marker files it already writes are touched, and (2) each extraction step in
`unpack/start.sh` appending to (rather than starting fresh at) a lineage chain per file -
`dispatch_round`/`process_zip_like` already know both a file's own sha256 and the sha256 of
whatever archive it came out of, so the data exists at exactly the right point, it's just
never written down. Store the chain as a `source_chain` array field
(`[{url}, {filename, sha256, archive_type}, ...]`) so a document is traceable end-to-end and
Kibana can filter/aggregate by original download. *Effort: M. Impact: high for "does this leak
contain X" answers that need to state provenance, not just content.*

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

#### 36. Result quality

Highlighted snippets rather than raw content, a saved search per detected entity type, and
export of a result set as CSV or JSON for reporting back to whoever asked. *Effort: M.*

## Suggested sequencing

All of the "analytical power" work is done: PII detection (31), OCR (the highest-value part of
21), language detection (the tractable half of 32), near-duplicate clustering (33) and the CLI.
So are the two opsec/housekeeping items that used to sit here, 42 (preflight TOR leak test) and
40 (log-ingest scaffolding). What remains:

1. Entity extraction (the rest of 32) is blocked on a dependency decision (spaCy vs.
    Elasticsearch's inference API), not effort - worth settling before picking one.
2. The rest of 21 (email formats beyond PST, structured data as rows, disk/VM images, mobile
    backups, encrypted-archive listing), 36 (result quality), and 10's v2ray remainder are each
    individually small and can be picked up whenever the surrounding code is being touched.

## Decided, not open

Recording these so they are not re-litigated later:

- **PII is stored in full, not masked.** `deis pii-scan` writes complete personnummer, IBANs
  and card numbers into each document's `pii` field. Masking would make the index safer to
  hand around, but "does this leak contain this specific person's data" is the question the
  tool exists to answer, and answering it means pivoting on the actual value. The index is
  therefore as sensitive as the dump itself and should be treated that way.
- **`attachment.content` has no per-language sub-fields.** The `.english`/`.swedish` analyzed
  copies added with item 32 were measured with the `_disk_usage` API on a real index: 20.3%
  each, against 20.5% for the base field - they tripled content indexing and accounted for
  ~41% of the whole index, for stemming that buys little on a corpus item 32 found to be
  largely Portuguese. Removed. The `language` keyword field, which is what the notebook and
  the Kibana filter actually use, costs nothing by comparison and stays. Note that dropping
  them only affects indices created afterwards - Elasticsearch cannot remove a field from an
  existing mapping, so an index built before this keeps paying for them until it is rebuilt.
- **Logstash is gone.** It had a compose profile but nothing ever fed it - `ingest.py` writes
  to Elasticsearch directly - and its remaining justification, the optional extensions in
  `extensions/`, went with item 40. Removed along with its Elasticsearch account and role.

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
| 18 | No zip-slip/decompression-bomb/disk-space guard and no timeout on `7zz`/`readpst` | `44ff308` |
| 19 | `unpack/start.sh` extracted one archive at a time regardless of available CPU cores | `010903c` |
| 20 | Config reading in `unpack/start.sh` used substring matching instead of being section-aware | `010903c` |
| 21 (OCR only) | A scanned passport or invoice saved as a plain image indexed with no searchable text at all | `23ac2e8` |
| 21 (email beyond PST) | Only `.pst` was handled; `.msg`/`.eml`/`.mbox`/`.ost` were left as opaque blobs, and `.msg` in particular risked being shredded by 7-Zip's own OLE/CFBF "Compound" archive detection before Tika ever saw it | `2c8809f` |
| 22 | The index mapping was entirely dynamic; `sha256`/`filename` were analyzed as text and the cluster was permanently `yellow` | `c8b0f59` |
| 24 | Every ingested file was a separate `PUT /_doc`, not batched via `_bulk` | `c8b0f59` |
| 25 | Nothing reconciled files on disk against documents indexed, and the counts were never Kibana-visible | `44ff308` |
| 26 | A run marked itself complete even when files had failed | `8e6bbdf` |
| 27 | `use_sqlite=True` failed immediately in the container, and its own database's own producer used a different path | `c8b0f59` |
| 28 | `bin/pathfix.py` read whole files into memory instead of streaming | `c8b0f59` |
| 29 (partly) | The unused `attachment` ingest pipeline was created alongside the one actually used | `c8b0f59` |
| 30 | README's "only run ingest" command referenced a script that does not exist | `c8b0f59` |
| 31 | The only way to answer "does this leak contain my friend's personal data" was free-text KQL | `23ac2e8` |
| 32 (language detection only) | The notebook hand-applied both stopword lists at once instead of detecting which language a document is actually in | `23ac2e8` |
| 33 | Mail threads, template letters, and versioned files that share most of their content had no way to be grouped short of exact sha256 matches | `4d94a47` |
| 34 | No Kibana-visible signal existed for "still encrypted"/"corrupt"/"unsafe", only plain files on disk | `a6fef83` |
| 35 | Password in the connection URL, hardcoded/duplicated index name, and a full-corpus pull for the word cloud | `a6fef83` |
| 37 | No test suite and CI ran only super-linter/osv-scanner; found and fixed a `re.match` gap in `web/app.py` and two CodeQL findings (embedded-credential URLs) in `ingest.py` along the way | `931e508` |
| 38 | `ES_JAVA_OPTS` was hardcoded, forcing the 18 GB Docker requirement on everyone regardless of dump size | `3f3cf9b` |
| 39 (ingest) | `ingest.py` had no per-file log, only an end-of-run summary | `3f3cf9b` |
| 40 | `evtx2json/`, `evtx/`, `json/`, `syslog/` and `extensions/` were a half-built log-ingest path from closed issue #1, wired into nothing - and it silently created a credentialed `filebeat_internal` account with a write role on every instance | `c300891` |
| 42 | Nothing verified that traffic meant for TOR actually left via TOR; a broken proxy chain would have downloaded in the clear without saying so | `TORCHECK_COMMIT` |
| 41 | Multiple copies of a never-before-seen file could all be uploaded and Tika-parsed before any marker existed | `f228de5` |
| 43 | `web` and `ingest.py` kept two independent, unsynchronized sha256 symlink trees | `c8b0f59` |
| 44 | `creatorrc.py` failed on every start, so TOR ran on stock defaults and the guard tuning was never applied | `014be0f` |
| CLI | Running DEIS meant memorizing docker compose profile incantations and checking four marker-file directories by hand | `065c714` |
| 45 | unpack's "try extracting it" detection is signature-based, not extension-based, so `.xlsx`/`.docx`/`.pptx`/ODF files (real ZIP archives internally) and legacy `.doc`/`.xls`/`.ppt` (OLE/CFBF, 7-Zip's own "Compound" format) were shredded into internal XML parts or raw property streams instead of reaching Tika whole - found via the "Top folders" dashboard panel showing OOXML-internal folder names, then confirmed against a real corpus: one `.xlsx` became 1029 meaningless documents, one `.xls` extracted to only its two metadata streams with the actual spreadsheet data stream never surviving at all | `4837d32` |
| 21 (MS Access) | `.mdb`/`.wdb` databases indexed with `content_length: 0` - Tika has no Access parser. `maybe_export_access_tables()` exports every table to a `<name>.<table>.csv` sidecar via `mdbtools`, same pattern as OCR's `.ocr.txt` - confirmed against a real corpus (a Swedish accounting export's `.wdb` files), including one 160KB staff/payroll table that previously had zero searchable content | `264fc39` |
| 21 (password-cracking) | A single individually-encrypted document (`.docx`/`.xlsx`/`.pdf`, as opposed to an encrypted *archive*) never had `deis.cfg`'s password list tried against it. `decrypt_office_document()`/`decrypt_pdf_document()` do, via `msoffcrypto-tool`/`qpdf` respectively - confirmed against a real corpus (8 individually-encrypted files, all Office format) and a synthetic encrypted-PDF fixture; a recovered document is flagged `extraction_status: decrypted` in Kibana, distinct from a document that was never protected | `264fc39` |

A review of items 21/31/32/33 and the CLI afterwards found four defects in the work above,
fixed in `f2702b5` and `f35014c`: `simhash.fingerprint()` returned 0 rather than "no result"
for text with no alphabetic words, so every numeric/tabular document was a distance-0 match
for every other one and they collapsed into one bogus cluster; `es_bulk()` ignored its
response, and `_bulk` answers 200 OK even when every item in it failed, so `pii-scan` and
`dedupe-scan` could write nothing while printing a full results table; `deis init` left
`.env` world-readable; and the notebook's results table interpolated leak-dump filenames
into an HTML widget unescaped. Item 33's clustering counts were re-measured after the
fingerprint fix; documents with no alphabetic words are now reported as skipped instead
of being grouped together, which is where most of the earlier inflation came from.

Item 10 is only partly fixed — `creatorrc.py` and `guard_country_resolver.py` are vendored
and 7-Zip is checksummed (`c80f15c`), but the v2ray installer is still fetched unpinned. See
item 10 above for what remains.

Item 11 was resolved as a deliberate decision, worth recording: **`.onion` goes through TOR
because nothing else resolves it, and everything else is fetched directly**, because the
operator is expected to be on a VPN and routing clearnet traffic through TOR only makes it
slower. `FORCE_TOR=true` restores the everything-through-TOR behaviour. The preflight TOR
leak test suggested in the original item 11 was not implemented and is still worth having;
see item 42 above.

## Deliberately not doing

| Item | Why not |
|---|---|
| 12 | Integrity verification against a published checksum. Leak sites essentially never publish hashes for their dumps, so it would sit unused. `ingest.py` already computes a real sha256 from the file as ingested, which catches corruption indirectly. |
| 23 | ILM/rollover for `leakdata-*`. Solves an index growing unbounded across many cases sharing one long-lived cluster - not this project's model, which is one DEIS instance per leak, with the Elasticsearch volume wiped and the stack recreated for each new one. Item 22's mapping fix already takes effect from scratch on every freshly recreated instance without it; rollover would only add alias-management complexity (touching `ingest.py`, the notebook, and the dashboards) for a scenario that doesn't occur here. |
