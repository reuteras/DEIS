#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""."""
#

import configparser
import hashlib
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from tqdm import tqdm
from typing import Iterable

def get_files(directory: Path) -> Iterable[Path]:
    """Return all files in specified and recursive directories."""
    return (file for file in directory.glob("**/*") if file.is_file())


def read_configuration(config_file):
    """Read configuration file."""
    config = configparser.RawConfigParser()
    config.read(config_file)
    if not config.sections():
        print("Can't find configuration file.")
        sys.exit(1)
    return config

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


def handle_file(fname: Path):
    """Handle files."""
    sha256 = get_filehash(fname)
    if len(sha256) != 64:
        print("ERROR: Could not get sha256 for file: ", fname)
        return
    if not Path(new_dir+"/"+sha256[0]).is_dir():
        Path(new_dir+"/"+sha256[0]).mkdir()
    if not Path(new_dir+"/"+sha256[0]+"/"++sha256[1]).is_dir():
        Path(new_dir+"/"+sha256[0]+"/"++sha256[1]).mkdir()
    
    return

def process_files(directory: Path):
    """Process all files in parallel (number of CPUs)."""
    with ProcessPoolExecutor() as executor:
        files = get_files(directory)
        files_list = list(files)
        files_count = len(files_list)
        files = iter(files_list)
        results = list(tqdm(executor.map(handle_file, files), total=files_count))
        for r in results:
            if r:
                print(r)

cfg = read_configuration("./deis.cfg")
new_dir = str(cfg.get("pathfix", "new_dir"))

if __name__ == "__main__":
    process_files(Path(sys.argv[1]))
