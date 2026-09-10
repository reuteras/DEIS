# DEIS

## Background

Project to create an automated pipeline with [Docker][doc] and [docker compose][dco] to investigate data from ransomware leaks.

The project started after a friend asked for help investigating if a data leak contained the friends personal information.

This tool can be used to automate all of (or a selection of) the steps below.

- **D**ownload files from a leek site via [TOR][tor]
- **E**xtract files from .rar, .ZIP, .tgz and more with the help of [7-zip][7zz]
- **I**ngest into [Elasticsearch][els] with the [Tika][tik] [pipeline][eap]
- **S**earch via [Kibana][kib] and [JupyterLab][jup] notebooks

### Download

Download files automatically from leek sites using [TOR][tor]. I use a [forked][for] version of [aria2-onion-downloader][aod].

`.onion` URLs go through TOR because nothing else resolves them; everything else is fetched
directly, which is faster and costs nothing in privacy as long as you are on a VPN. Set
**FORCE_TOR**`=true` in *.env* to push everything through TOR anyway.

Before a single URL is queued, a preflight check asks `check.torproject.org` which address it
actually sees, using the same proxy settings the real download would use - a broken proxy
chain does not announce itself, it just quietly downloads in the clear. If the batch contains
a `.onion` URL (or **FORCE_TOR** is set) and that traffic turns out **not** to be leaving via
TOR, nothing is queued at all and the reason is written to `logs/download_errors.log`. A
clearnet-only batch is not blocked - it never wanted TOR - but the exit address is still
printed so you can confirm it is your VPN and not your own. `deis doctor` runs the same check
on demand.

### Extract

Automated extraction of compressed files with a simple container running [7-zip][7zz].
Extraction is recursive: whatever comes out of an archive is checked again, so an archive
nested inside an archive inside an archive is still found, regardless of its extension -
detection is "try extracting it" rather than a fixed list of extensions, since leak dumps are
full of wrong or missing ones. This continues for up to `max_depth` rounds (`deis.cfg`,
default 6); anything nested deeper than that is left as-is, and files still encrypted after
every password below has been tried are listed in `status/still_encrypted.txt`.

- Run [readpst][res] on [`.pst` and `.ost`][pst] (Outlook Data File, and Outlook's offline cache
  of the same data) files - both share the same underlying format, so both are gated by the
  same `pst`/`pst_archive`/`pst_remove` `deis.cfg` keys.
- `.msg` (a single Outlook message) is left untouched here rather than run through 7-Zip: it
  uses the same OLE/CFBF container format as legacy `.doc`/`.xls`/`.ppt`, which 7-Zip
  recognizes as an archive by content signature regardless of extension, so extracting it would
  shred it into unreadable internal streams instead of a message. It's parsed into searchable
  text by Tika during ingest instead - as are `.eml` and `.mbox`, which are plain text and were
  never touched by extraction in the first place. This isn't purely extension-based: a file's
  first 8 bytes are also checked against the OLE/CFBF magic number, so a legacy Office/Outlook
  document saved under some other extension (found in a real corpus: a `.kli` accounting
  export, byte-for-byte an OLE2 Word document) still gets left whole instead of shredded. PDFs
  are skipped the same way for a different reason: some PDF authoring tools append proprietary
  sidecar data as a small ZIP fragment after the file's own `%%EOF`, which 7-Zip's signature
  scan finds and tries to open as an archive - the real PDF content is unaffected either way,
  but 7-Zip would otherwise report the file as "corrupt".
- A multi-volume RAR set (`foo.part01.rar`, `foo.part02.rar`, ...) only needs its first volume
  extracted - 7-Zip pulls in every sibling volume automatically as long as they're all present
  in the same directory. The other volumes, tried on their own, fail (misleadingly, as "wrong
  password" on every entry - no password fixes a missing volume) and are tracked separately in
  `status/still_multivolume.txt` rather than `still_encrypted.txt`. Once extraction as a whole
  finishes, any of those still sitting in `files/` are checked again: if a sibling volume of the
  same set already succeeded, the file is moved into `extracted/archive/` the same as a directly
  extracted original, rather than being left looking like unfinished work. A set missing a
  volume entirely (never downloaded, for example) stays flagged in `still_multivolume.txt`
  instead - that's a genuinely different, actionable problem from "wrong password".
- If the downloaded files are password protected, set **ZIP_PASSWORD** in *.env*, or add one
  password per line to files in the *passwords* directory - every archive is tried against
  all of them, in order, at every nesting level.
- Per-file results are logged to `logs/unpack.log`.
- Each round of extraction runs in parallel, up to **PARALLELISM** archives at once (set in
  *.env*; defaults to the number of CPU cores available to the container).
- Guards against hostile archives (`deis.cfg`'s `[unpack]` section): an archive is rejected
  before extraction if any entry's path would escape the destination directory (zip-slip), or
  if its declared uncompressed size is over `max_extract_bytes` (default 10 GiB) or its
  compression ratio is over `max_compression_ratio` (default 200x, the shape of a
  decompression bomb) - either way it's left as-is, unextracted, and listed in
  `status/still_unsafe.txt`. A single archive's extraction is also capped at
  `extract_timeout` seconds (default 1800) so one hung `7-Zip`/`readpst` run can't stall the
  pipeline indefinitely.
- OCR for image files (`deis.cfg`'s `ocr`/`ocr_languages`, on by default with `eng`; also
  installed: `swe`) - a scanned passport or invoice saved as a plain image otherwise has
  no searchable text at all. Writes a `<name>.ocr.txt` file next to the image, indexed as its
  own separate document.
- MS Access table export (`deis.cfg`'s `access_export`, on by default) - Tika has no Access
  parser, so a `.mdb`/`.wdb` database otherwise indexes with no searchable text at all despite
  holding real structured data. Every table is exported to a `<name>.<table>.csv` sidecar via
  `mdbtools`, each indexed as its own separate document. `.accdb` (Access 2007+/ACE) is only
  partially supported by `mdbtools`; `.dbf` (dBase) isn't attempted at all.
- Password-cracking for individually password-protected documents (`deis.cfg`'s
  `document_decrypt_office`/`document_decrypt_pdf`, both on by default) - a single encrypted
  `.docx`/`.xlsx`/`.pdf`, as opposed to an encrypted *archive*, is tried against the same
  password list above (`qpdf` for PDF, `msoffcrypto-tool` for Office). A recovered document is
  decrypted in place and searchable as normal, flagged `extraction_status: decrypted` in
  Kibana so it's distinguishable from a document that was never protected; one still stuck
  after every password is flagged `extraction_status: encrypted`, same as an undecryptable
  archive. PDF detection needs one `qpdf` encryption check per PDF (mime type alone can't tell
  an encrypted PDF from an ordinary one, unlike Office's reliable `application/encrypted`
  signal) - turn `document_decrypt_pdf` off if that per-PDF cost isn't wanted.

### Ingest

Ingest files to search into [Elasticsearch][els] with the [attachment processor][eap] enabled. The processor uses Apache [Tika][tik] to extract text from files.

I've incorporated the [docker-elk][del] repository setup and run Elasticsearch and Kibana but have removed Logstash.

Newly indexed and failed files are logged to `logs/ingest.log`; a summary prints at the end of every run.

`.csv` files (`deis.cfg`'s `csv_rows`, on by default) are additionally indexed one document
*per row*, in their own `leakdata-rows-*` index, alongside their normal whole-file document -
Tika's flattened text blob can't answer "which row has my friend's data", a per-row document
with real column names can. Delimiter is auto-detected (`,`/`;`/tab - Swedish locale exports
commonly use `;`); the first row is always treated as a header, so a genuinely header-less
file has its first data row misread as column names - a real corpus turned up both cases:
Swedish debt-collection (`Kronofogden`) exports with real headers (`Personnummer`, `Namn`,
`Belopp`, ...) index perfectly, while large `Quinyx` workforce-scheduling exports (no header
at all) get synthetic-looking column names instead. Exact-match queries
(`row.Personnummer: "<value>"` in Kibana) are fully reliable; a numeric range query is not -
`row`'s `flattened` mapping compares values lexicographically as strings, confirmed live
(`"906"` matches `> "1000"`). Capped at `csv_max_rows` (default 50000) per file, truncation
logged, never silently dropped.

`.xlsx` workbooks get the same treatment (`deis.cfg`'s `xlsx_rows`), parsed stdlib-only
(`zipfile` + `xml.etree.ElementTree`, not `openpyxl` - see `ingest/ingest.py`'s
`parse_xlsx_rows`). Same header-first-row rule as `.csv`. A workbook can have several sheets,
which `.csv` never does - each row carries a `_source_table` field set to the sheet name, so
`row._source_table: "Sheet1"` filters to one sheet, and `row_number` is one sequence across the
whole file rather than resetting per sheet. Capped at `xlsx_max_rows` (default 50000) across all
sheets combined. SQL dumps/SQLite are not handled yet.

### Search

Search can be done with [Kibana][kib] and a [JupyterLab][jup] notebook. The notebook is my [reuteras/container-notebook][con].

Every document is tagged with a detected `language` (`english`/`swedish`/`unknown`, via a
stopword-presence heuristic run server-side at ingest time) - filterable in Kibana, and used
by the notebook's word cloud to pick the right stopword list automatically instead of needing
it set by hand.

`deis pii-scan` fills each document's `pii` field with the personal identifiers found in its
text - Swedish personnummer/samordningsnummer, IBANs and card numbers (each validated against
its own checksum, so a random run of digits is not reported), plus emails and phone numbers.
Values are stored **in full**, not masked: finding every document that mentions one specific
person is the question this tool exists to answer, and that needs the actual value to pivot
on. Treat the index accordingly - it is as sensitive as the dump it came from.

`deis pii-report` lists what a prior `pii-scan` already found, without rescanning anything -
a capped preview table by default (filenames shortened to their basename, values truncated,
just for a quick look), or the full result set as CSV via `--output <file>.csv`.

`deis dedupe-scan` groups near-identical documents (the same template letter, a monthly report
with one number changed) into `duplicate_cluster`, using a SimHash fingerprint rather than the
exact sha256 match that ingest already deduplicates on. Documents with no alphabetic words at
all - pure numeric or tabular exports - have nothing to compare and are reported as skipped
rather than being grouped together.

### Known limitations and planned work

Known gaps and the backlog of planned improvements are documented in
[docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md). Worth reading before trusting a result:
it explains what the pipeline currently does not extract or detect.

## Requirements

You must increase the RAM that Docker can use to 18 GB or more. If you don't want to raise it
that high - for example when working with a small dump - lower **ES_JAVA_OPTS** in *.env*
instead (default `-Xms2g -Xmx16g`).

## Install and configure

Download the repository from GitHub and change to the new directory.

```bash
git clone https://github.com/reuteras/DEIS.git
cd DEIS
```

First we need to create the venv and install Python packages. If you have `just` installed you can run `just venv`. Otherwise run:

```bash
uv sync --dev
source .venv/bin/activate
```

Configure DEIS by running:

```bash
./bin/deis init
```

Look through the created *.env* and *deis.cfg* files and update as needed.

Add a list of URLs (one per line) for files to download to a file in the *urls* directory. If you already have the files downloaded look for the `add-files` subcommand of `./bin/deis`.

`.onion` URLs are downloaded over TOR and everything else is downloaded directly, which is
much faster. Set **FORCE_TOR=true** in *.env* to send every download through TOR instead.

Setup Elasticsearch and Kibana by running the command below which will start a configuration container and dependent containers.

```bash
./bin/deis setup
```

## Run all steps

To run all steps in **DEIS** run.

```bash
./bin/deis run
```

Monitor progress by running:

```bash
just progress
```

Press CTRL-C to exit the progress display.

The following web services are available. All of them listen on 127.0.0.1 only, so they are
not reachable from other machines on your network:

- [http://127.0.0.1:8081/](http://127.0.0.1:8081/) - **Start here.** Pipeline status (download/
  extract/ingest), funnel counts, still-encrypted/corrupt/unsafe counts, the latest ingest run,
  and links to the other services below. Comes up as soon as the `web` container starts - it
  doesn't wait for extraction or ingest to finish - and refreshes itself every 30 seconds.
- [http://127.0.0.1:3000/](http://127.0.0.1:3000/) - Gotenberg server
- [http://127.0.0.1:5601/](http://127.0.0.1:5601/) - Elastic/Kibana
- [http://127.0.0.1:8080/](http://127.0.0.1:8080/) - AriaNg
- [http://127.0.0.1:8081/file/<sha256>](http://127.0.0.1:8081/file/) - Download file based on sha256
- [http://127.0.0.1:8081/convert/<sha256>](http://127.0.0.1:8081/convert/) - Convert file to PDF (if possible) and download file based on sha256
- [http://127.0.0.1:8081/view/<sha256>](http://127.0.0.1:8081/view/) - Preview a file and download the original
- [http://127.0.0.1:8888/](http://127.0.0.1:8888/) - JupyterLab, log in with the **JUPYTER_TOKEN** from *.env*

## Only run a subset of the steps

### Only run ingest

If you already have the files available you can skip the download and extraction steps and only ingest the files to Elasticsearch. The files must be in the directory *extracted* or you have to update *deis.cfg*.

```bash
just ingest
.venv/bin/python3 ingest/ingest.py
```

### Skip download, using files you already have

If the original URLs are dead but you have the files some other way (a colleague's copy, a
different mirror, ...) and still want them extracted and OCR'd as usual, `bin/deis add-files`
copies them into *files/* and marks the download stage as done, so the pipeline picks up from
extraction. It accepts one or more files and/or directories (directories are searched
recursively), so a shell glob like `bin/deis add-files /path/to/*.rar` works too:

```bash
bin/deis add-files <file-or-directory> [file-or-directory ...]
bin/deis run --only extract
bin/deis run --only ingest
```

`run --only ingest` doesn't need to be re-run after every extraction batch once the `ingest`
container is up: it isn't a one-shot job, it's a long-lived process that polls every 30 seconds
for `status/extract_done` and starts `ingest.py` itself as soon as that marker appears
(`ingest/start.sh`). So if `ingest` is already running from an earlier `bin/deis run` or
`bin/deis run --only ingest`, a fresh `bin/deis run --only extract` is enough on its own -
ingestion picks up automatically once extraction finishes.

`bin/deis status` (or `just progress`, or the web dashboard) tracks `extract` and `ingest`
through four states: `waiting` (nothing to do yet) -> `pending` (the prior stage is done and
this one is ready, but hasn't actually started working) -> `running` (actively extracting/
ingesting right now - `unpack/start.sh` and `ingest/start.sh` touch `status/extracting`/
`status/ingesting` right before they start the real work, the same way `download.sh` touches
`status/running`) -> `done`. If `ingest` sits at `pending` rather than moving to `running`,
that usually means the `ingest` container was never started - check with `docker compose ps
ingest` and start it with `bin/deis run --only ingest` if it's missing.

## Command-line interface

`bin/deis` wraps most of the above into a single command (requires `just venv` to have been
run once, so `.venv` exists):

```bash
bin/deis init            # bootstrap .env and deis.cfg if they don't exist yet
bin/deis doctor          # preflight checks: Docker, memory, Elasticsearch/Kibana, containers
bin/deis build           # build every container image (docker compose build), before setup or run
bin/deis setup           # alias for 'run --only setup': start the setup container and follow its logs until it exits
bin/deis run             # start the full pipeline (docker compose --profile deis up -d)
bin/deis run --only ingest   # or just one stage: setup, download, extract, or ingest
bin/deis add-urls <url>  # queue a URL (or a file of URLs) for download, with validation
bin/deis add-files <path> [path ...]  # copy already-downloaded files in, skipping the download stage
bin/deis status          # snapshot of pipeline stage state and funnel counts
bin/deis search <term>   # search indexed content from the terminal
bin/deis report          # what was found, what could not be processed
bin/deis pii-scan        # detect personal identifiers in indexed content (see below)
bin/deis pii-report      # list what pii-scan already found (see below)
bin/deis dedupe-scan     # cluster near-duplicate documents (see below)
bin/deis clean           # wraps 'just clean' behind a confirmation prompt
bin/deis reset           # wraps 'just dist-clean' behind a confirmation prompt (deletes evidence)
```

A full run from a clean checkout:

```bash
just venv
./bin/deis init
./bin/deis build   # optional - setup/run build images on demand too, this just does it upfront
./bin/deis setup
./bin/deis run
```

`bin/deis pii-scan` is a post-pass, run after ingest: it fetches each document's already
Tika-extracted text and looks for Swedish personnummer/samordningsnummer, emails, phone
numbers, IBANs and card numbers, validating each numeric one against its real
checksum before counting it (so a random 9- or 10-digit number in a spreadsheet isn't reported
as a personnummer just because it has the right shape). Results land in each document's `pii`
field, searchable in Kibana or via `bin/deis search`, and the "Leaked data" dashboard has a
"Documents with personal identifiers" panel. Only unscanned documents are processed by
default; pass `--rescan` to redo everything (for example after upgrading the detectors).
Checksum validation cuts false positives sharply but doesn't eliminate them entirely - a
personnummer or card number match still only needs to satisfy a 1-in-10 chance by coincidence,
so treat a `pii-scan` hit as a strong lead worth checking, not absolute proof.

`bin/deis dedupe-scan` clusters near-duplicate documents - mail threads, template letters, and
recurring monthly reports that share most of their content without being byte-identical (exact
sha256 dedup, always on, only catches files that are byte-for-byte the same). Uses SimHash: a
64-bit fingerprint per document such that near-identical text produces a small Hamming distance
between fingerprints. Unlike `pii-scan`/language detection, this always recomputes the whole
corpus on every run (whether two documents cluster together depends on every other document
too, not just themselves) - pass `--max-distance` to loosen or tighten how similar two
documents need to be (default 10 of 64 bits). Results land in each clustered document's
`duplicate_cluster` field (the representative member's sha256), and the "Leaked data"
dashboard has a "Near-duplicate clusters" panel, sorted so each cluster's members sit together.

Shell completion for subcommands (and `run --only`'s choices) is available for bash and zsh:

```bash
# Bash - add to ~/.bashrc to persist:
eval "$(bin/deis completion bash)"

# Zsh - add to ~/.zshrc to persist, or drop into a directory already on $fpath:
eval "$(bin/deis completion zsh)"
```

## Development

The per-service scripts (`web/app.py`'s path validation, `ingest/ingest.py`'s hashing/dedup
logic, `bin/pathfix.py`'s hashing) have a test suite under `tests/`, run with:

```bash
just test
```

This also runs `ruff check`/`ruff format --check`. CI runs the same checks on every push and
pull request (`.github/workflows/tests.yml`).

## Search tips

Disable collection by Elastic by opening [http://127.0.0.1:5601/app/management/kibana/settings](http://127.0.0.1:5601/app/management/kibana/settings), click on **Global Settings** and scroll down and click **off** on **Share usage with Elastic**.

Files are added to elastic with timestamp from the filesystem. Search in discovery with absolute time range from *Jan 1, 1970 @ 00:00:00.000* to *now*.

A quick overview of the data is available in the dashboard named **Leaked data**.

To only search a for data already in elastic you can use **docker compose up -d** as start command.

Stop all services with **docker compose --profile deis down**.

**highlight.max_analyzed_offset** is set for you by the setup container, both in the index
template for future indices and directly on the existing one - no manual step needed.

If you get an error about **search.max_async_search_response_size**, open the developer
console at [http://127.0.0.1:5601/app/dev_tools#/console](http://127.0.0.1:5601/app/dev_tools#/console)
and execute:

```txt
PUT _cluster/settings
{
  "persistent": {
    "search.max_async_search_response_size": "50mb"
  }
}
```

## Based on

This project uses several open source tools in combination. A list below and please submit an issue if I have missed any:

- [docker-elk][del]
- [aria2-onion-downloader][aod] which uses [AriaNg][maa]
- Apache [Tika][tik]
- [readpst][res]
- [Tor][tor]
- The whole ELK-stack by [Elastic.co][eco]
- [Jupyterlab][jup]

- Monitor [mayswind/AriaNg][maa] for new releases.

  [7zz]: https://www.7-zip.org/
  [aod]: https://github.com/sn0b4ll/aria2-onion-downloader
  [con]: https://github.com/reuteras/container-notebook
  [del]: https://github.com/deviantony/docker-elk
  [dco]: https://docs.docker.com/compose/
  [doc]: https://www.docker.com/
  [eap]: https://www.elastic.co/guide/en/elasticsearch/reference/current/attachment.html
  [eco]: https://www.elastic.co/
  [els]: https://www.elastic.co/elasticsearch/
  [for]: https://github.com/reece394/aria2-onion-downloader
  [jup]: https://github.com/jupyterlab/jupyterlab
  [kib]: https://www.elastic.co/kibana
  [maa]: https://github.com/mayswind/AriaNg
  [pst]: https://support.microsoft.com/en-au/office/introduction-to-outlook-data-files-pst-and-ost-222eaf92-a995-45d9-bde2-f331f60e2790
  [res]: https://linux.die.net/man/1/readpst
  [tik]: https://tika.apache.org/
  [tor]: https://www.torproject.org/
