#!/usr/bin/env python3
"""Ingest files to Elasticsearch"""

import base64
import configparser
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

headers = {"content-type": "application/x-ndjson"}
success_list = [200, 201]
INDEX = "leakdata-index-000001"
# Keep individual _bulk requests to a sane size - standard Elasticsearch
# guidance is a handful of MB per request, not so large that one slow/huge
# batch dominates a retry, not so small that batching stops paying off.
BULK_MAX_DOCS = 200
BULK_MAX_BYTES = 20 * 1024 * 1024

# What happened to a single file, for the summary at the end of a run.
SKIPPED = "skipped"  # bookkeeping file, never meant to be indexed
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


def resolve_filepath(filename, hash_value):
    """Look up the original filename via sqlite, if enabled."""
    if not use_sqlite:
        return filename
    cur = con.cursor()
    res = cur.execute("SELECT original_filename FROM files WHERE sha256=?", (hash_value,))
    return res.fetchone()[0]


def prepare_file(fname: Path):
    """Hash and read one file, ready for a later bulk request.

    Runs in a worker process - this is the CPU/IO-bound part, still worth
    parallelizing - but never touches the network and never creates a
    marker; those only happen once a bulk request confirms success, back in
    the main process (see process_batch). Nothing raises out of here: a
    single unreadable or unusual file must not abort the whole run.

    Returns a dict, always with a "status" key: SKIPPED (a bookkeeping file),
    PRESENT (already indexed, carries "sha256"), FAILED (could not even be
    hashed, carries "error"), or "ready" (carries "fname", "sha256",
    "content" bytes and "message", waiting to be sent).
    """
    if str(fname) in ["extracted/files/done", "extracted/files/path.txt"]:
        return {"status": SKIPPED}
    try:
        sha256 = get_filehash(fname)
        if sha256 is None or len(sha256) != 64:
            return {"status": FAILED, "sha256": None, "error": f"ERROR: Could not get sha256 for file: {fname}"}
        if hash_link_exists(sha256):
            # This content is already indexed, by an earlier run or by an
            # identical copy of the file elsewhere in the tree.
            return {"status": PRESENT, "sha256": sha256}
        if fname.stat().st_size > max_size:
            content = b""
            message = "to large"
        else:
            with open(fname, "rb") as f:
                content = f.read()
            message = "ok"
    except Exception as error:  # noqa: BLE001 - one bad file must not stop the batch
        return {"status": FAILED, "sha256": None, "error": f"ERROR: Failed to prepare {fname}: {error!r}"}
    return {"status": "ready", "fname": fname, "sha256": sha256, "content": content, "message": message}


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
    archive at all - "encrypted"/"corrupt" mean the document's content is
    the opaque original, not what was inside it. Kibana's "File without text
    content" search already showed files with no content; this says why,
    for files unpack itself flagged rather than a still-unclassified case
    (an image, a format Tika doesn't parse, etc.) - see item 34.
    """
    if hash_value in still_encrypted:
        return "encrypted"
    if hash_value in still_corrupt:
        return "corrupt"
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


def log_results(results):
    """Appends one line per interesting outcome to logs/ingest.log, matching
    unpack/start.sh's logs/unpack.log: never truncated, timestamped, and
    only outcomes worth knowing about later - not every already-indexed file
    on every run, the same way unpack's log() is never called for content
    that was already there.
    """
    try:
        with open("/logs/ingest.log", "a", encoding="utf-8") as f:
            now = datetime.now(UTC).isoformat()
            for status, sha256, error in results:
                if status == INDEXED:
                    f.write(f"{now} [INDEXED] sha256={sha256}\n")
                elif status == FAILED:
                    f.write(f"{now} [FAILED] {error}\n")
    except OSError as error:
        print("ERROR: Could not write to /logs/ingest.log:", error, flush=True)


def print_summary(results, directory):
    """Account for every file that was looked at.

    Far fewer documents than files is normal and expected - identical files
    share one document, because the document id is the sha256 - but without
    this breakdown the difference looks like data went missing.
    """
    statuses = [status for status, _, _ in results]
    errors = [error for _, _, error in results if error]
    # Files that are represented by a document, whether this run wrote it or not.
    covered = {sha256 for status, sha256, _ in results if status in (INDEXED, PRESENT)}
    counted = statuses.count(INDEXED) + statuses.count(PRESENT)

    log_results(results)
    for error in errors:
        print(error)

    print()
    print("Ingest summary")
    print("--------------")
    print(f"  files looked at:      {len(results)}")
    print(f"  internal files:       {statuses.count(SKIPPED)}")
    print(f"  unique files:         {len(covered)}")
    print(f"  duplicate copies:     {counted - len(covered)}")
    print(f"  indexed this run:     {statuses.count(INDEXED)}")
    print(f"  already indexed:      {statuses.count(PRESENT)}")
    print(f"  failed:               {statuses.count(FAILED)}")

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
    batch = []
    batch_bytes = 0
    seen_this_run = set()
    pending_duplicates = []

    def flush():
        nonlocal batch, batch_bytes
        if batch:
            results.extend(process_batch(batch))
            batch = []
            batch_bytes = 0

    with ProcessPoolExecutor() as executor:
        files = list(get_files(directory))
        for prepared in tqdm(
            executor.map(prepare_file, files), total=len(files), desc="Processing files", unit="files"
        ):
            status = prepared["status"]
            if status == "ready":
                sha256 = prepared["sha256"]
                if sha256 in seen_this_run:
                    pending_duplicates.append(prepared)
                    continue
                seen_this_run.add(sha256)
                batch.append(prepared)
                batch_bytes += len(prepared["content"])
                if len(batch) >= BULK_MAX_DOCS or batch_bytes >= BULK_MAX_BYTES:
                    flush()
            else:
                results.append((status, prepared.get("sha256"), prepared.get("error")))
        flush()

    # Every representative's outcome is now known (create_hash_link only
    # runs on confirmed success), so resolve every deferred duplicate
    # against it without a second upload - two files with the same sha256
    # are byte-identical, so a retry could never produce a different result.
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

    return print_summary(results, directory)


cfg = read_configuration("./deis.cfg")
max_size = int(cfg.get("ingest", "max_size"))
use_sqlite = cfg.getboolean("ingest", "use_sqlite")
still_encrypted = load_sha256_set("extracted/still_encrypted.txt")
still_corrupt = load_sha256_set("extracted/still_corrupt.txt")
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
    Path("./extracted/ingest_done").touch()
    print("Ingest done.")
