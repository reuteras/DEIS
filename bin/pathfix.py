#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""."""

import hashlib
import argparse
import sqlite3
import sys
from pathlib import Path
from tqdm import tqdm

DB_PATH = "file_hashes.db"

def compute_sha256(file_path):
    """Return the sha256 hash of a file."""
    with file_path.open('rb') as f:
        return hashlib.sha256(f.read()).hexdigest()

def setup_database():
    """Initialize the SQLite database."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            original_filename TEXT NOT NULL,
            sha256 TEXT UNIQUE NOT NULL
        );
        """)
        conn.commit()

def insert_into_database(filename, file_hash):
    """Insert filename and its sha256 into the database."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT OR IGNORE INTO files (original_filename, sha256) VALUES (?, ?)
        """, (filename, file_hash))
        conn.commit()

def copy_or_move_files(source, dest, operation='copy'):
    all_files = list(source.rglob('*'))
    for file_path in tqdm(all_files, desc="Processing files", unit="file"):
        if file_path.is_file():
            file_hash = compute_sha256(file_path)
            new_file_path = dest / file_hash[:1] / file_hash[1:2] / (file_hash + file_path.suffix)

            insert_into_database(str(file_path), file_hash)

            # Only copy/move if the destination file doesn't exist
            if not new_file_path.exists():
                if not new_file_path.parent.exists():
                    new_file_path.parent.mkdir(parents=True)

                if operation == 'copy':
                    new_file_path.write_bytes(file_path.read_bytes())
                elif operation == 'move':
                    file_path.rename(new_file_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Copy or move files based on sha256 structure and track in SQLite database."
        )
    parser.add_argument("source", type=str, help="Source directory containing files to be processed.")
    parser.add_argument("dest", type=str, help="Destination directory where files will be placed.")
    parser.add_argument(
        "mode",
        type=str,
        choices=['copy', 'move'],
        help="Operation mode. Choose 'copy' to copy files or 'move' to move files."
        )

    args = parser.parse_args()

    source_path = Path(args.source)
    dest_path = Path(args.dest)

    if not source_path.exists() or not source_path.is_dir():
        print(f"Error: Source path '{source_path}' does not exist or is not a directory.")
        sys.exit(1)

    if dest_path.exists() and not dest_path.is_dir():
        print(f"Error: Destination path '{dest_path}' exists and is not a directory.")
        sys.exit(1)

    setup_database()

    try:
        copy_or_move_files(source_path, dest_path, operation=args.mode)
    except (sqlite3.Error, FileNotFoundError, PermissionError) as e:
        print(f"Error occurred during operation: {e}")
#