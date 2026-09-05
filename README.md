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

### Extract

Automated extraction of compressed files with a simple container running [7-zip][7zz].
Extraction is recursive: whatever comes out of an archive is checked again, so an archive
nested inside an archive inside an archive is still found, regardless of its extension -
detection is "try extracting it" rather than a fixed list of extensions, since leak dumps are
full of wrong or missing ones. This continues for up to `max_depth` rounds (`deis.cfg`,
default 6); anything nested deeper than that is left as-is, and files still encrypted after
every password below has been tried are listed in `extracted/still_encrypted.txt`.

- Run [readpst][res] on files with the extension [.pst][pst] (Outlook Data File).
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
  `extracted/still_unsafe.txt`. A single archive's extraction is also capped at
  `extract_timeout` seconds (default 1800) so one hung `7-Zip`/`readpst` run can't stall the
  pipeline indefinitely.
- OCR for image files (`deis.cfg`'s `ocr`/`ocr_languages`, on by default with `eng`; also
  installed: `swe`) - a scanned passport or invoice saved as a plain image otherwise has
  no searchable text at all. Writes a `<name>.ocr.txt` file next to the image, indexed as its
  own separate document.

### Ingest

Ingest files to search into [Elasticsearch][els] with the [attachment processor][eap] enabled. The processor uses Apache [Tika][tik] to extract text from files.

I've incorporated the [docker-elk][del] repository setup and run Elasticsearch and Kibana but have removed Logstash.

Newly indexed and failed files are logged to `logs/ingest.log`; a summary prints at the end of every run.

### Search

Search can be done with [Kibana][kib] and a [JupyterLab][jup] notebook. The notebook is my [reuteras/container-notebook][con].

Every document is tagged with a detected `language` (`english`/`swedish`/`unknown`, via a
stopword-frequency heuristic run server-side at ingest time) - filterable in Kibana, and used
by the notebook's word cloud to pick the right stopword list automatically instead of needing
it set by hand. `attachment.content` also carries per-language analyzed sub-fields
(`.english`/`.swedish`, using Elasticsearch's built-in stemming analyzers) for better recall on
Swedish text.

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

Configure DEIS by changing three files:

- Copy *.env.default* to *.env* and modify the passwords in it. Set **JUPYTER_TOKEN** to a
  random value (`openssl rand -hex 32`) - JupyterLab will not start without one. If the
  downloaded files are password protected set the **ZIP_PASSWORD**, or - if there is more
  than one password in play - add them one per line to files in the *passwords* directory
  instead; every archive at every nesting level is tried against all of them. *.env* is
  not tracked by git, so your passwords stay on your machine.
- Add a list of URLs (one per line) for files to download to a file in the *urls* directory.
- Copy *deis.cfg.default* to *deis.cfg* and update the settings described in the file.

`.onion` URLs are downloaded over TOR and everything else is downloaded directly, which is
much faster. Set **FORCE_TOR=true** in *.env* to send every download through TOR instead.

Setup Elasticsearch and Kibana by running the command below which will start a configuration container and dependent containers.

```bash
docker compose --profile setup up -d
```

Wait for *deis-setup-1* to exit. Tailing the container logs will exit when the container is done after about 45 seconds.

```bash
docker logs deis-setup-1 -f
```

## Run all steps

To run all steps in **DEIS** run.

```bash
docker compose --profile deis up -d
```

Monitor progress by first running:

```bash
just venv
```

And then run the *bin/progress.py* Python script with:

```bash
just progress
```

Press CTRL-C to exit the progress display.

The following web services are available. All of them listen on 127.0.0.1 only, so they are
not reachable from other machines on your network:

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

## Command-line interface

`bin/deis` wraps most of the above into a single command (requires `just venv` to have been
run once, so `.venv` exists):

```bash
bin/deis init            # bootstrap .env and deis.cfg if they don't exist yet
bin/deis doctor          # preflight checks: Docker, memory, Elasticsearch/Kibana, containers
bin/deis run             # start the full pipeline (docker compose --profile deis up -d)
bin/deis run --only ingest   # or just one stage: download, extract, or ingest
bin/deis add-urls <url>  # queue a URL (or a file of URLs) for download, with validation
bin/deis status          # snapshot of pipeline stage state and funnel counts
bin/deis search <term>   # search indexed content from the terminal
bin/deis report          # what was found, what could not be processed
bin/deis pii-scan        # detect personal identifiers in indexed content (see below)
bin/deis dedupe-scan     # cluster near-duplicate documents (see below)
bin/deis clean           # wraps 'just clean' behind a confirmation prompt
bin/deis reset           # wraps 'just dist-clean' behind a confirmation prompt (deletes evidence)
```

`bin/deis doctor` does not yet include a TOR egress leak test (see
[docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md) item 42) - it does not confirm downloads are
actually routed through TOR.

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
