#!/usr/bin/env python3
"""Ingest files to Elasticsearch"""

import configparser
import hashlib
import os
import sqlite3
import sys
import time
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cbor2
import requests
from tqdm import tqdm

if not Path("./extracted/sha256").is_dir():
    Path("./extracted/sha256").mkdir()

headers = {"content-type": "application/cbor"}
success_list = [200, 201]
INDEX = "leakdata-index-000001"

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

    Only called once Elasticsearch has confirmed the document, so the symlink
    doubles as the "this file is done" marker for later runs. Creating it
    earlier would mean a crash between the symlink and the upload leaves the
    file marked as done but never indexed - it would then be skipped forever.
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


def request_retry(url, data, num_retries=5):
    for _ in range(num_retries):
        try:
            response = requests.put(url, data=data, headers=headers, timeout=50)
            if response.status_code in success_list:
                ## Return response if successful
                return response
            if response.status_code == 400:
                return response
            time.sleep(15)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            time.sleep(60)
        except requests.exceptions.RequestException as error:
            # Anything else requests can raise (SSL errors, malformed URLs,
            # too many redirects, ...). Retry rather than killing the worker.
            print("ERROR: Request to Elastic failed:", error, flush=True)
            time.sleep(15)
    return None


def send_elastic(filename, content, hash_value, message):
    """Send files to elastic."""

    filepath = filename

    if use_sqlite:
        cur = con.cursor()
        res = cur.execute("SELECT original_filename FROM files WHERE sha256=?", (hash_value,))
        filepath = res.fetchone()[0]

    doc = {
        "filename": str(filepath),
        "sha256": hash_value,
        "data": content,
        "mtime": int(filename.stat().st_mtime),
        "message": message,
    }

    return request_retry(
        index_url() + "/_doc/" + hash_value + "?pipeline=cbor-attachment",
        data=cbor2.dumps(doc),
    )


def index_url():
    """Return the base URL of the index, credentials included."""
    return "http://elastic:" + password + "@" + elastic_host + ":9200/" + INDEX


def elastic_document_count():
    """Return how many documents the index holds, or None if it can't be read.

    Refresh first, otherwise documents indexed moments ago are not counted yet.
    """
    try:
        requests.post(index_url() + "/_refresh", timeout=30)
        response = requests.get(index_url() + "/_count", timeout=30)
        if response.status_code not in success_list:
            return None
        return int(response.json()["count"])
    except (requests.exceptions.RequestException, ValueError, KeyError):
        return None


def handle_file(fname: Path):
    """Handle files.

    Returns (status, sha256, error) so process_files() can both report failures
    and account for every file in the summary. Nothing raises out of here: a
    single unreadable or unusual file must not abort the whole batch, since
    every worker shares one ProcessPoolExecutor.
    """
    if str(fname) in ["extracted/files/done", "extracted/files/path.txt"]:
        return (SKIPPED, None, None)
    try:
        sha256 = get_filehash(fname)
        if sha256 is None or len(sha256) != 64:
            return (FAILED, None, f"ERROR: Could not get sha256 for file: {fname}")
        if hash_link_exists(sha256):
            # This content is already indexed, by an earlier run or by an
            # identical copy of the file elsewhere in the tree.
            return (PRESENT, sha256, None)
        if fname.stat().st_size > max_size:
            content = ""
            message = "to large"
        else:
            with open(fname, "rb") as f:
                content = f.read()
            message = "ok"
        response = send_elastic(fname, content, sha256, message)
        if response is None:
            return (FAILED, sha256, f"Error sending file to Elastic (Null returned): {fname}")
        if response.status_code == 400:
            # Tika could not parse it. Index it again without the content so
            # the file is still findable by name/hash, with the parser error
            # kept in "message".
            response = send_elastic(fname, "", sha256, response.text)
            if response is None:
                return (FAILED, sha256, f"Error sending file to Elastic (Null returned): {fname}")
            if response.status_code not in success_list:
                return (
                    FAILED,
                    sha256,
                    f"Error sending file to Elastic (second try returned): {fname} Code: {response.status_code}",
                )
        # Only now is the file really done, so mark it for the next run.
        create_hash_link(sha256, fname)
    except Exception as error:  # noqa: BLE001 - one bad file must not stop the batch
        return (FAILED, None, f"ERROR: Failed to ingest {fname}: {error!r}")
    return (INDEXED, sha256, None)


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
    """Process all files in parallel (number of CPUs).

    Returns the number of files that failed, so the caller can decide whether
    the run counts as finished.
    """
    with ProcessPoolExecutor() as executor:
        files = get_files(directory)
        files_list = list(files)
        files_count = len(files_list)
        files = iter(files_list)
        results = list(tqdm(executor.map(handle_file, files), total=files_count, desc="Processing files", unit="files"))
        return print_summary(results, directory)


cfg = read_configuration("./deis.cfg")
max_size = int(cfg.get("ingest", "max_size"))
use_sqlite = cfg.getboolean("ingest", "use_sqlite")
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
