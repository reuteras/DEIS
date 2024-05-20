#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ingest files to Elasticsearch"""

import configparser
import hashlib
import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Iterable

import cbor2
import requests
from tqdm import tqdm

if not Path("./extracted/sha256").is_dir():
    Path("./extracted/sha256").mkdir()

headers = {'content-type': 'application/cbor'}
success_list = [200, 201]

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
        with open(filename,"rb") as f:
            # Read and update hash string value in blocks of 4K
            for byte_block in iter(lambda: f.read(4096),b""):
                sha256_hash.update(byte_block)
    except FileNotFoundError:
        print("ERROR: Could not get sha256 for:", filename, flush=True)
        return None
    return sha256_hash.hexdigest()


def create_hash_link(hash_value, filename):
    """Create link from hash to file."""
    sha256_link = Path("extracted/sha256/" + str(hash_value))
    if sha256_link.is_symlink():
        return False
    try:
        sha256_link.symlink_to("../"+str(filename).replace('extracted/', ''))
    except (FileExistsError, OSError, RuntimeError):
        if not sha256_link.is_symlink():
            print("ERROR: Could not create symlink for file:", filename, flush=True)
    return True


def remove_sha256(hash_value):
    sha256_link = Path("extracted/sha256/" + str(hash_value))
    try:
        sha256_link.unlink()
    except (FileExistsError, OSError, RuntimeError):
        print("ERROR: Could not remove link extraced/sha256/" + hash_value)


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
    return None


def send_elastic(filename, content, hash_value, message):
    """Send files to elastic."""

    filepath=filename

    if use_sqlite:
        cur = con.cursor()
        res = cur.execute("SELECT original_filename FROM files WHERE sha256=?", (hash_value,))
        filepath = res.fetchone()[0]

    doc = {
        'filename': str(filepath),
        'sha256': hash_value,
        'data': content,
        'mtime': int(filename.stat().st_mtime),
        'message': message,
    }

    return request_retry(
            'http://elastic:'+password+'@'+elastic_host+':9200/leakdata-index-000001/_doc/'+hash_value+'?pipeline=cbor-attachment',
            data=cbor2.dumps(doc),
    )


def handle_file(fname: Path):
    """Handle files."""
    if str(fname) in ["extracted/files/done", "extracted/files/path.txt"]:
        return
    sha256 = get_filehash(fname)
    if len(sha256) != 64:
        print("ERROR: Could not get sha256 for file: ", fname)
        return
    if not create_hash_link(sha256, fname):
        # Same hash has been added already
        return
    if Path(fname).stat().st_size > max_size:
        content = ""
        message = "to large"
    else:
        with open(fname, 'rb') as f:
            content = f.read()
        message = "ok"
    response = send_elastic(fname, content, sha256, message)
    if response is None:
        print("Error sending file to Elastic (Null returned):", fname)
        remove_sha256(sha256)
        return
    if response.status_code == 400:
        response = send_elastic(fname, "", sha256, response.text)
        if response.status_code == 400:
            print("Error sending file to Elastic (second 400 returned):", fname)
            remove_sha256(sha256)
        elif response.status_code not in success_list:
            print("Error sending file to Elastic (second try returned):", fname, "Code:", response.status_code)
            remove_sha256(sha256)
    return


def process_files(directory: Path):
    """Process all files in parallel (number of CPUs)."""
    with ProcessPoolExecutor() as executor:
        files = get_files(directory)
        files_list = list(files)
        files_count = len(files_list)
        files = iter(files_list)
        results = list(tqdm(executor.map(handle_file, files), total=files_count, desc="Processing files", unit="files"))
        for r in results:
            if r:
                print(r)


cfg = read_configuration("./deis.cfg")
max_size = int(cfg.get("ingest", "max_size"))
use_sqlite = cfg.getboolean("ingest", "use_sqlite")
if use_sqlite:
    con = sqlite3.connect("db/file_hashes.db")
try:
    password = os.environ['ELASTIC_PASSWORD']
except (AttributeError, KeyError):
    password = str(cfg.get("elastic", "password"))

if Path("/.dockerenv").is_file():
    elastic_host = "elasticsearch"
else:
    elastic_host = "127.0.0.1"


if __name__ == "__main__":
    process_files(Path(cfg.get("ingest", "files")))
    Path("./extracted/ingest_done").touch()
    print("Ingest done.")

