#!/usr/bin/env python3
"""Ingest files to Elasticsearch"""

import base64
import configparser
import csv
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import requests
from tqdm import tqdm

if not Path("./extracted/sha256").is_dir():
    Path("./extracted/sha256").mkdir()
if not Path("./extracted/sha256_rows").is_dir():
    Path("./extracted/sha256_rows").mkdir()

headers = {"content-type": "application/x-ndjson"}
success_list = [200, 201]
INDEX = "leakdata-index-000001"
# Separate from INDEX: one small document per ingest run, not leak data, so
# it has its own lifecycle and is never at risk of being mixed into a
# content search (item 25's remaining piece - the reconciliation counts
# below were previously only visible in the container's own stdout).
RUNS_INDEX = "deis-ingest-runs"
# Separate from INDEX for the same reason RUNS_INDEX is: item 21's
# structured-data-as-rows piece, .csv only for now. One document per CSV
# row rather than one flattened text blob per file - see parse_csv_rows()
# and build_rows_bulk_body(). Its own index (rather than a field on the
# file's own document) because a single CSV can have thousands of rows,
# and "leakdata-rows-*" already matches the existing Kibana index
# pattern's "leakdata-*" title, so no new one was needed there.
ROW_INDEX = "leakdata-rows-000001"
# Keep individual _bulk requests to a sane size - standard Elasticsearch
# guidance is a handful of MB per request, not so large that one slow/huge
# batch dominates a retry, not so small that batching stops paying off.
BULK_MAX_DOCS = 200
BULK_MAX_BYTES = 20 * 1024 * 1024

# What happened to a single file, for the summary at the end of a run.
INDEXED = "indexed"  # sent to Elasticsearch during this run
PRESENT = "present"  # a document for this content already existed
FAILED = "failed"  # could not be indexed, will be retried on the next run


def read_configuration(config_file):
    """Read configuration file."""
    config = configparser.RawConfigParser()
    config.read(config_file)
    if not config.sections():
        print("Can't find configuration file.")
        sys.exit(1)
    return config


def get_files(directory: Path) -> Iterable[Path]:
    """Return all files in specified and recursive directories."""
    return (file for file in directory.glob("**/*") if file.is_file())


def get_filehash(filename):
    """Return sha256 hash for filename"""
    sha256_hash = hashlib.sha256()
    try:
        with open(filename, "rb") as f:
            # Read and update hash string value in blocks of 4K
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
    except FileNotFoundError:
        print("ERROR: Could not get sha256 for:", filename, flush=True)
        return None
    return sha256_hash.hexdigest()


def hash_link_exists(hash_value):
    """Return True if this hash has already been ingested successfully."""
    return Path("extracted/sha256/" + str(hash_value)).is_symlink()


def create_hash_link(hash_value, filename):
    """Create link from hash to file.

    Only called once Elasticsearch has confirmed the document (in a bulk
    response, per item), so the symlink doubles as the "this file is done"
    marker for later runs. Creating it earlier would mean a crash between the
    symlink and the upload leaves the file marked as done but never indexed -
    it would then be skipped forever.
    """
    sha256_link = Path("extracted/sha256/" + str(hash_value))
    if sha256_link.is_symlink():
        return False
    try:
        sha256_link.symlink_to("../" + str(filename).replace("extracted/", ""))
    except (FileExistsError, OSError, RuntimeError):
        if not sha256_link.is_symlink():
            print("ERROR: Could not create symlink for file:", filename, flush=True)
    return True


def row_hash_link_exists(hash_value):
    """Same idea as hash_link_exists, but tracked separately: a file whose
    content was already blob-indexed by an earlier run (before this
    feature existed, or with csv_rows disabled) still needs its rows
    picked up once csv_rows is on - a single shared marker would silently
    skip rows for the whole existing corpus forever. Keyed by the same
    content sha256, so a byte-identical duplicate never re-parses/re-sends
    rows either, the same as the blob marker already ensures for content.
    """
    return Path("extracted/sha256_rows/" + str(hash_value)).is_symlink()


def create_row_hash_link(hash_value, filename):
    """Create link from hash to file for the rows marker - see
    row_hash_link_exists. Only called once Elasticsearch has confirmed
    every row for this file (in a rows _bulk response), for the same
    crash-safety reason create_hash_link's docstring gives for the blob
    marker.
    """
    sha256_link = Path("extracted/sha256_rows/" + str(hash_value))
    if sha256_link.is_symlink():
        return False
    try:
        sha256_link.symlink_to("../" + str(filename).replace("extracted/", ""))
    except (FileExistsError, OSError, RuntimeError):
        if not sha256_link.is_symlink():
            print("ERROR: Could not create rows symlink for file:", filename, flush=True)
    return True


def index_url():
    """Return the base URL of the index.

    Credentials are never embedded here - passed separately via requests'
    auth= parameter (see elastic_auth()) instead, so a password can't end up
    in a request's URL and leak into an exception message, a printed error,
    or /logs/ingest.log the way it would if it were part of the URL string.
    """
    return "http://" + elastic_host + ":9200/" + INDEX


def elastic_auth():
    """Return the (username, password) tuple for requests' auth= parameter."""
    return ("elastic", password)


def elastic_document_count():
    """Return how many documents the index holds, or None if it can't be read.

    Refresh first, otherwise documents indexed moments ago are not counted yet.
    """
    try:
        requests.post(index_url() + "/_refresh", auth=elastic_auth(), timeout=30)
        response = requests.get(index_url() + "/_count", auth=elastic_auth(), timeout=30)
        if response.status_code not in success_list:
            return None
        return int(response.json()["count"])
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return None


def index_run_summary(counts):
    """Best-effort: index one reconciliation document per ingest run into
    RUNS_INDEX, so the counts print_summary() already prints to the
    container log are also visible in Kibana - a saved search sorted by
    @timestamp shows the full run history, not just whatever is in the log
    of the most recently run container. Never raises: a run that fully
    succeeded but couldn't reach Elasticsearch for this one extra write
    should not be reported as failed.
    """
    try:
        response = requests.post(
            f"http://{elastic_host}:9200/{RUNS_INDEX}/_doc",
            json=counts,
            auth=elastic_auth(),
            timeout=30,
        )
        if response.status_code not in success_list:
            print(f"ERROR: Could not index run summary (status {response.status_code}).", flush=True)
    except requests.exceptions.RequestException as error:
        print("ERROR: Could not index run summary:", error, flush=True)


def resolve_filepath(filename, hash_value):
    """Look up the original filename via sqlite, if enabled."""
    if not use_sqlite:
        return filename
    cur = con.cursor()
    res = cur.execute("SELECT original_filename FROM files WHERE sha256=?", (hash_value,))
    return res.fetchone()[0]


def parse_csv_rows(content: bytes, max_rows: int) -> tuple[list[dict], dict]:
    """Parses CSV bytes into one {column: value} dict per data row (the
    header row isn't counted as one), auto-detecting the delimiter with
    csv.Sniffer rather than assuming a fixed dialect - real corpus data
    mixes comma- and semicolon-delimited exports (Swedish locale
    conventions commonly use ';', since ',' is the decimal separator).

    The first row is always treated as a header. csv.Sniffer.has_header()
    was tried and dropped: confirmed empirically unreliable on exactly the
    realistic case this feature exists for - a header row of all-string
    column names (e.g. "namn;stad") followed by all-string data rows gives
    Sniffer no type contrast to key off, and it returns a confident but
    wrong False, silently demoting real column names to
    column_1/column_2/... for a large share of real business CSVs. A fixed
    "always a header" assumption is simpler and more predictable than a
    heuristic that fails on its own primary use case; the tradeoff is a
    genuinely header-less file has its first data row misread as column
    names instead.

    Never raises: garbage/non-CSV bytes, an empty file, or anything
    csv.Error can throw all come back as ([], {"error": ...}) instead of
    propagating - a single malformed CSV must not take down the whole
    ingest run (via prepare_file's own outer try/except turning any
    exception into a whole-file FAILED status) or block that file's own
    normal full-text indexing, only its per-row breakdown. Note this is
    "never raises", not "never produces rows for non-CSV input" - genuinely
    unparseable bytes may still yield some garbage-in-garbage-out rows
    rather than an empty result, since there's no reliable way to detect
    "this isn't really CSV-shaped" short of the parse itself succeeding.

    Encoding is guessed by trying, in order, utf-8-sig (strips a BOM -
    the common Excel-on-Windows case), cp1252 (the common Windows/
    Swedish "Save As CSV" default), then latin-1, which maps every byte
    0-255 and so can never itself raise UnicodeDecodeError - the last
    attempt always succeeds.

    Stops at max_rows and sets meta["truncated"] = True rather than
    parsing an unbounded number of rows into memory - a hostile or just
    unexpectedly huge CSV should degrade gracefully, not exhaust worker
    memory or stall the run. The file's own full-text blob document is
    unaffected by this cap either way.
    """
    try:
        text = None
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not text or not text.strip():
            return [], {"error": "empty or undecodable content"}

        sample = text[:8192]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel

        rows = []
        columns = None
        truncated = False
        for raw_row in csv.reader(text.splitlines(), dialect):
            if not raw_row:
                continue
            if columns is None:
                columns = [cell.strip() or f"column_{i + 1}" for i, cell in enumerate(raw_row)]
                continue
            if len(rows) >= max_rows:
                truncated = True
                break
            rows.append(
                {columns[i] if i < len(columns) else f"column_{i + 1}": value for i, value in enumerate(raw_row)}
            )
        return rows, {"truncated": truncated}
    except Exception as error:  # noqa: BLE001 - one bad CSV must not stop the batch/run
        return [], {"error": repr(error)}


def prepare_file(fname: Path):
    """Hash and read one file, ready for a later bulk request.

    Runs in a worker process - this is the CPU/IO-bound part, still worth
    parallelizing - but never touches the network and never creates a
    marker; those only happen once a bulk request confirms success, back in
    the main process (see process_batch/process_rows_batch). Nothing raises
    out of here: a single unreadable or unusual file must not abort the
    whole run.

    Blob indexing (the file's full content, via Tika) and row indexing
    (item 21: .csv only, one document per row) are tracked by two
    independent markers (see row_hash_link_exists), so a file already
    blob-indexed by an earlier run - most of the corpus, once csv_rows
    ships - still gets its rows picked up rather than being silently
    skipped forever.

    Returns a dict, always with a "status" key: PRESENT (both already
    indexed, carries "sha256"), FAILED (could not even be hashed, carries
    "error"), "ready" (blob indexing needed, carries "fname", "sha256",
    "content" bytes, "message", and "rows"/"row_meta" if this is a .csv
    needing row indexing too), or "ready_rows_only" (blob already indexed,
    only rows are needed - carries the same keys as "ready" except the
    blob content is never sent to the file-level index, only used to parse
    rows).
    """
    try:
        sha256 = get_filehash(fname)
        if sha256 is None or len(sha256) != 64:
            return {"status": FAILED, "sha256": None, "error": f"ERROR: Could not get sha256 for file: {fname}"}

        blob_needed = not hash_link_exists(sha256)
        needs_rows = csv_rows_enabled and fname.suffix.lower() == ".csv" and not row_hash_link_exists(sha256)
        if not blob_needed and not needs_rows:
            # This content is already indexed (blob and, if it's a .csv,
            # rows too), by an earlier run or by an identical copy of the
            # file elsewhere in the tree.
            return {"status": PRESENT, "sha256": sha256}

        if fname.stat().st_size > max_size:
            content = b""
            message = "to large"
        else:
            with open(fname, "rb") as f:
                content = f.read()
            message = "ok"

        rows, row_meta = (None, None)
        if needs_rows:
            rows, row_meta = parse_csv_rows(content, csv_max_rows) if message == "ok" else ([], {"error": message})
    except Exception as error:  # noqa: BLE001 - one bad file must not stop the batch
        return {"status": FAILED, "sha256": None, "error": f"ERROR: Failed to prepare {fname}: {error!r}"}
    return {
        "status": "ready" if blob_needed else "ready_rows_only",
        "fname": fname,
        "sha256": sha256,
        "content": content,
        "message": message,
        "rows": rows,
        "row_meta": row_meta,
    }


def load_sha256_set(path):
    """Reads a file of one sha256 per line into a set, or an empty set if it
    doesn't exist (e.g. unpack hasn't run, or ran before this file existed).
    """
    try:
        with open(path, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def extraction_status(hash_value):
    """Whether unpack could extract this file's content, if it was ever an
    archive at all - "encrypted"/"corrupt"/"unsafe"/"multivolume" mean the
    document's content is the opaque original, not what was inside it.
    Kibana's "File without text content" search already showed files with
    no content; this says why, for files unpack itself flagged rather than
    a still-unclassified case (an image, a format Tika doesn't parse,
    etc.) - see item 34. "unsafe" (item 18) means unpack rejected the
    archive before ever attempting extraction - a zip-slip entry, a
    decompression-bomb shape, or not enough disk space - so unlike
    "corrupt" it says nothing about whether the archive itself was
    otherwise valid. "multivolume" means a non-first volume of a
    multi-volume archive that's missing its other parts entirely (the
    ordinary case - a full, resolvable set - never reaches ingest at all;
    unpack's resolve_multivolume_stragglers disposes of those before this
    file ever runs) - unlike the others, that makes it an actionable "go
    find the missing volume" flag rather than a permanent characteristic
    of the file itself. "decrypted" means this document was individually
    password-protected (a single encrypted .docx/.xlsx/.pdf, as opposed to
    an encrypted archive - see item 21's password-cracking write-up) and
    unpack recovered it by trying deis.cfg's password list - a positive
    signal (full content is available and was Tika-parsed normally), but
    flagged so an analyst can tell "this was originally encrypted and we
    cracked it" apart from a document that was never protected, matching
    the audit-trail spirit of the other flags above. Never collides with
    them: a recovered document's hash is computed from its decrypted
    content, which by definition differs from whatever hash (if any) an
    unresolved encrypted/corrupt/unsafe/multivolume file was tracked under.
    """
    if hash_value in still_encrypted:
        return "encrypted"
    if hash_value in still_corrupt:
        return "corrupt"
    if hash_value in still_unsafe:
        return "unsafe"
    if hash_value in still_multivolume:
        return "multivolume"
    if hash_value in decrypted:
        return "decrypted"
    return "ok"


def build_bulk_body(items):
    """Builds the newline-delimited JSON body for one _bulk request.

    File content is base64-encoded: the _bulk endpoint only accepts JSON,
    not the CBOR the single-document PUT this replaced used to send raw
    bytes with (Elasticsearch rejects "Content-Type: application/cbor" on
    _bulk) - verified against the live pipeline that a base64 "data" field
    still gets correctly picked up and decoded by the attachment processor.
    """
    lines = []
    for item in items:
        lines.append(json.dumps({"index": {"_id": item["sha256"]}}))
        doc = {
            "filename": resolve_filepath(str(item["fname"]), item["sha256"]),
            "sha256": item["sha256"],
            "data": base64.b64encode(item["content"]).decode("ascii"),
            "mtime": int(item["fname"].stat().st_mtime),
            "message": item["message"],
            "extraction_status": extraction_status(item["sha256"]),
        }
        lines.append(json.dumps(doc))
    return ("\n".join(lines) + "\n").encode("utf-8")


def bulk_request(items, num_retries=5):
    """POSTs one _bulk request, retrying the whole batch on connection or
    transient failures - the same backoff request_retry used before.
    Returns the response's "items" list, in the same order as the request
    (guaranteed by the _bulk API), or None if every retry failed.
    """
    url = index_url() + "/_bulk?pipeline=cbor-attachment"
    body = build_bulk_body(items)
    for _ in range(num_retries):
        try:
            response = requests.post(url, data=body, headers=headers, auth=elastic_auth(), timeout=120)
            if response.status_code in success_list:
                return response.json()["items"]
            time.sleep(15)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            time.sleep(60)
        except requests.exceptions.RequestException as error:
            # Anything else requests can raise (SSL errors, malformed URLs,
            # too many redirects, ...). Retry rather than giving up outright.
            print("ERROR: Bulk request to Elastic failed:", error, flush=True)
            time.sleep(15)
    return None


def process_batch(items):
    """Sends one batch and applies the crash-safe marker-only-after-confirmed
    rule per document. Returns (status, sha256, error) tuples, one per item,
    the same shape prepare_file's callers already expect.
    """
    results = []
    responses = bulk_request(items)
    if responses is None:
        return [
            (FAILED, item["sha256"], f"Error sending file to Elastic (bulk request failed): {item['fname']}")
            for item in items
        ]

    retry_items = []
    for item, item_response in zip(items, responses):
        action_result = item_response["index"]
        status = action_result["status"]
        if status in success_list:
            create_hash_link(item["sha256"], item["fname"])
            results.append((INDEXED, item["sha256"], None))
        elif status == 400:
            # Tika could not parse it. Retry without content so the file is
            # still findable by name/hash, with the parser error kept in
            # "message".
            retry_items.append({**item, "content": b"", "message": json.dumps(action_result.get("error", {}))})
        else:
            results.append(
                (FAILED, item["sha256"], f"Error sending file to Elastic (status {status}): {item['fname']}")
            )

    if retry_items:
        retry_responses = bulk_request(retry_items)
        if retry_responses is None:
            results.extend(
                (FAILED, item["sha256"], f"Error sending file to Elastic (bulk retry failed): {item['fname']}")
                for item in retry_items
            )
        else:
            for item, item_response in zip(retry_items, retry_responses):
                action_result = item_response["index"]
                status = action_result["status"]
                if status in success_list:
                    create_hash_link(item["sha256"], item["fname"])
                    results.append((INDEXED, item["sha256"], None))
                else:
                    results.append(
                        (
                            FAILED,
                            item["sha256"],
                            f"Error sending file to Elastic (second try, status {status}): {item['fname']}",
                        )
                    )

    return results


def build_rows_bulk_body(items):
    """Builds the newline-delimited JSON body for one CSV-rows _bulk
    request. Unlike build_bulk_body: no cbor-attachment pipeline (rows are
    already-parsed JSON, not raw file bytes needing Tika) and no base64.
    One item here is one file, but it expands to one bulk action pair per
    row within it - _id is "<sha256>:<row_number>", deterministic given
    deterministic parsing, so re-sending the same file's rows (e.g. a
    retried batch) is a safe, idempotent overwrite rather than a duplicate.
    """
    lines = []
    for item in items:
        source_filename = resolve_filepath(str(item["fname"]), item["sha256"])
        for row_number, row in enumerate(item["rows"], start=1):
            lines.append(json.dumps({"index": {"_id": f"{item['sha256']}:{row_number}"}}))
            doc = {
                "source_sha256": item["sha256"],
                "source_filename": source_filename,
                "row_number": row_number,
                "row": row,
            }
            lines.append(json.dumps(doc))
    return ("\n".join(lines) + "\n").encode("utf-8")


def rows_bulk_request(items, num_retries=5):
    """Same retry/backoff shape as bulk_request, against ROW_INDEX instead
    of INDEX and with no ingest pipeline - see build_rows_bulk_body.
    """
    url = f"http://{elastic_host}:9200/{ROW_INDEX}/_bulk"
    body = build_rows_bulk_body(items)
    for _ in range(num_retries):
        try:
            response = requests.post(url, data=body, headers=headers, auth=elastic_auth(), timeout=120)
            if response.status_code in success_list:
                return response.json()["items"]
            time.sleep(15)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            time.sleep(60)
        except requests.exceptions.RequestException as error:
            print("ERROR: Bulk request to Elastic (CSV rows) failed:", error, flush=True)
            time.sleep(15)
    return None


def process_rows_batch(items):
    """Sends one CSV-rows batch and applies the same crash-safe
    marker-only-after-confirmed rule process_batch uses, keyed per file
    (one rows marker covers all of that file's rows, not one per row).
    Returns (status, sha256, error) tuples, one per file - kept separate
    from process_batch's own results (see process_files): a CSV's row
    breakdown is additive enrichment, not a substitute for its normal
    full-text indexing, so a row-indexing failure here must never fail
    the file's own blob-indexing outcome or the run's overall exit code.
    """
    results = []
    responses = rows_bulk_request(items)
    if responses is None:
        return [
            (FAILED, item["sha256"], f"Error sending CSV rows to Elastic (bulk request failed): {item['fname']}")
            for item in items
        ]

    offset = 0
    for item in items:
        row_count = len(item["rows"])
        item_responses = responses[offset : offset + row_count]
        offset += row_count
        failed = [r for r in item_responses if r["index"]["status"] not in success_list]
        if not failed:
            create_row_hash_link(item["sha256"], item["fname"])
            results.append((INDEXED, item["sha256"], None))
        else:
            results.append(
                (
                    FAILED,
                    item["sha256"],
                    f"Error sending {len(failed)}/{row_count} CSV row(s) to Elastic: {item['fname']}",
                )
            )
    return results


def log_results(results, row_results):
    """Appends one line per interesting outcome to logs/ingest.log, matching
    unpack/start.sh's logs/unpack.log: never truncated, timestamped, and
    only outcomes worth knowing about later - not every already-indexed file
    on every run, the same way unpack's log() is never called for content
    that was already there. Row-indexing outcomes get their own tags
    (ROWS_INDEXED/ROWS_FAILED) so they're distinguishable from blob-level
    ones at a glance - the two are independent outcomes for the same file.
    """
    try:
        with open("/logs/ingest.log", "a", encoding="utf-8") as f:
            now = datetime.now(UTC).isoformat()
            for status, sha256, error in results:
                if status == INDEXED:
                    f.write(f"{now} [INDEXED] sha256={sha256}\n")
                elif status == FAILED:
                    f.write(f"{now} [FAILED] {error}\n")
            for status, sha256, error in row_results:
                if status == INDEXED:
                    f.write(f"{now} [ROWS_INDEXED] sha256={sha256}\n")
                elif status == FAILED:
                    f.write(f"{now} [ROWS_FAILED] {error}\n")
    except OSError as error:
        print("ERROR: Could not write to /logs/ingest.log:", error, flush=True)


def print_summary(results, row_results, directory):
    """Account for every file that was looked at.

    Far fewer documents than files is normal and expected - identical files
    share one document, because the document id is the sha256 - but without
    this breakdown the difference looks like data went missing.

    row_results (item 21's CSV-as-rows piece) is accounted for separately
    and deliberately does not affect the returned failure count: a row's
    structured breakdown is additive enrichment on top of a file's normal
    full-text indexing, not a substitute for it, so a row-indexing hiccup
    must never fail the run the way a file failing to index at all does.
    """
    statuses = [status for status, _, _ in results]
    errors = [error for _, _, error in results if error]
    # Files that are represented by a document, whether this run wrote it or not.
    covered = {sha256 for status, sha256, _ in results if status in (INDEXED, PRESENT)}
    counted = statuses.count(INDEXED) + statuses.count(PRESENT)

    row_statuses = [status for status, _, _ in row_results]
    row_errors = [error for _, _, error in row_results if error]

    log_results(results, row_results)
    for error in errors:
        print(error)
    for error in row_errors:
        print(error)

    print()
    print("Ingest summary")
    print("--------------")
    print(f"  files looked at:      {len(results)}")
    print(f"  unique files:         {len(covered)}")
    print(f"  duplicate copies:     {counted - len(covered)}")
    print(f"  indexed this run:     {statuses.count(INDEXED)}")
    print(f"  already indexed:      {statuses.count(PRESENT)}")
    print(f"  failed:               {statuses.count(FAILED)}")
    if row_statuses:
        print(f"  csv files w/ rows indexed: {row_statuses.count(INDEXED)}")
        if row_statuses.count(FAILED):
            print(f"  csv files w/ row errors:   {row_statuses.count(FAILED)}")

    documents = elastic_document_count()
    if documents is None:
        print("  in Elasticsearch:     could not be read")
    else:
        print(f"  in Elasticsearch:     {documents}")
        if documents < len(covered):
            print(f"  WARNING: {len(covered) - documents} unique file(s) have no document in Elasticsearch.")
        elif documents > len(covered):
            print(f"  Note: {documents - len(covered)} document(s) come from files no longer in {directory}.")
    print(flush=True)

    index_run_summary(
        {
            "@timestamp": datetime.now(UTC).isoformat(),
            "files_looked_at": len(results),
            "unique_files": len(covered),
            "duplicate_copies": counted - len(covered),
            "indexed_this_run": statuses.count(INDEXED),
            "already_indexed": statuses.count(PRESENT),
            "failed": statuses.count(FAILED),
            "csv_files_with_rows_indexed": row_statuses.count(INDEXED),
            "csv_files_with_row_errors": row_statuses.count(FAILED),
            "elasticsearch_document_count": documents,
        }
    )

    return statuses.count(FAILED)


def process_files(directory: Path):
    """Prepares files in parallel (number of CPUs), sends them to
    Elasticsearch in bulk batches, and returns the number that failed.

    Deduplicates content first seen this run before it is ever queued to
    send: ProcessPoolExecutor.map() yields results in input order, not
    completion order, so this loop - a single Python thread consuming that
    generator - sees every file in a fixed, deterministic sequence even
    though hashing/reading happens in parallel across worker processes. The
    first file with a given sha256 is queued normally; every later file with
    the same sha256 in this run is deferred instead of queued, so the same
    content is never uploaded and Tika-parsed twice just because two workers
    both happened to hash a never-before-seen duplicate before either's
    upload confirmed. No locking needed - the dedup decision itself never
    leaves this one serial loop.
    """
    results = []
    row_results = []
    batch = []
    batch_bytes = 0
    rows_batch = []
    rows_batch_count = 0
    seen_this_run = set()
    pending_duplicates = []

    def flush():
        nonlocal batch, batch_bytes
        if batch:
            results.extend(process_batch(batch))
            batch = []
            batch_bytes = 0

    def flush_rows():
        nonlocal rows_batch, rows_batch_count
        if rows_batch:
            row_results.extend(process_rows_batch(rows_batch))
            rows_batch = []
            rows_batch_count = 0

    with ProcessPoolExecutor() as executor:
        files = list(get_files(directory))
        for prepared in tqdm(
            executor.map(prepare_file, files), total=len(files), desc="Processing files", unit="files"
        ):
            status = prepared["status"]
            if status in ("ready", "ready_rows_only"):
                sha256 = prepared["sha256"]
                if sha256 in seen_this_run:
                    pending_duplicates.append(prepared)
                    continue
                seen_this_run.add(sha256)
                if status == "ready":
                    batch.append(prepared)
                    batch_bytes += len(prepared["content"])
                    if len(batch) >= BULK_MAX_DOCS or batch_bytes >= BULK_MAX_BYTES:
                        flush()
                else:
                    # "ready_rows_only": the blob is already confirmed
                    # indexed (that's what makes it rows-only), so unlike
                    # "ready" this doesn't wait on a bulk response to know
                    # its outcome - recorded here directly, or every such
                    # file would silently vanish from "files looked at"/
                    # "unique files" instead of counting as present.
                    results.append((PRESENT, sha256, None))
                if prepared.get("rows"):
                    rows_batch.append(prepared)
                    rows_batch_count += len(prepared["rows"])
                    if rows_batch_count >= BULK_MAX_DOCS:
                        flush_rows()
            else:
                results.append((status, prepared.get("sha256"), prepared.get("error")))
        flush()
        flush_rows()

    # Every representative's outcome is now known (create_hash_link only
    # runs on confirmed success), so resolve every deferred duplicate
    # against it without a second upload - two files with the same sha256
    # are byte-identical, so a retry could never produce a different result.
    # Row outcomes need no equivalent resolution here: row_hash_link_exists
    # is keyed by the same content sha256, so a duplicate's rows are
    # already covered once its representative's rows succeed.
    for item in pending_duplicates:
        if hash_link_exists(item["sha256"]):
            results.append((PRESENT, item["sha256"], None))
        else:
            results.append(
                (
                    FAILED,
                    item["sha256"],
                    f"Error sending file to Elastic (same content failed this run): {item['fname']}",
                )
            )

    return print_summary(results, row_results, directory)


cfg = read_configuration("./deis.cfg")
max_size = int(cfg.get("ingest", "max_size"))
use_sqlite = cfg.getboolean("ingest", "use_sqlite")
# fallback=True/50000, not a plain .getboolean()/.getint(): deis.cfg is
# gitignored and 'deis init' leaves an existing one alone, so a key added
# after someone's initial setup never appears in their config - reading
# that as a hard error (or silently as False/0) would either crash or
# disable a feature documented as on by default. Same reasoning as
# unpack/start.sh's config_true_default().
csv_rows_enabled = cfg.getboolean("ingest", "csv_rows", fallback=True)
csv_max_rows = cfg.getint("ingest", "csv_max_rows", fallback=50000)
still_encrypted = load_sha256_set("status/still_encrypted.txt")
still_corrupt = load_sha256_set("status/still_corrupt.txt")
still_unsafe = load_sha256_set("status/still_unsafe.txt")
still_multivolume = load_sha256_set("status/still_multivolume.txt")
decrypted = load_sha256_set("status/decrypted.txt")
if use_sqlite:
    con = sqlite3.connect("db/file_hashes.db")
try:
    password = os.environ["ELASTIC_PASSWORD"]
except (AttributeError, KeyError):
    password = str(cfg.get("elastic", "password"))

if Path("/.dockerenv").is_file():
    elastic_host = "elasticsearch"
else:
    elastic_host = "127.0.0.1"


if __name__ == "__main__":
    failed = process_files(Path(cfg.get("ingest", "files")))
    if failed:
        # Files that failed have no sha256 symlink, so they are picked up again
        # on the next run - but only if this run isn't marked as done.
        print("Ingest incomplete. Re-run with 'docker compose restart ingest' to retry the failed files.")
        sys.exit(1)
    Path("./status/ingest_done").touch()
    print("Ingest done.")
