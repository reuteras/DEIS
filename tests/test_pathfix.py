"""Tests for bin/pathfix.py's streaming sha256 (item 28)."""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def pathfix_module():
    spec = importlib.util.spec_from_file_location("deis_pathfix", REPO_ROOT / "bin" / "pathfix.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_matches_hashlib_for_small_content(pathfix_module, tmp_path):
    f = tmp_path / "small.txt"
    f.write_text("some content")
    assert pathfix_module.compute_sha256(f) == hashlib.sha256(b"some content").hexdigest()


def test_matches_hashlib_across_multiple_chunk_boundaries(pathfix_module, tmp_path):
    # compute_sha256 streams in 64KB blocks - content spanning several of
    # those blocks must hash identically to reading it all at once.
    f = tmp_path / "big.bin"
    data = bytes(range(256)) * 4000  # > 3 * 64KB
    f.write_bytes(data)
    assert pathfix_module.compute_sha256(f) == hashlib.sha256(data).hexdigest()


def test_empty_file(pathfix_module, tmp_path):
    f = tmp_path / "empty.txt"
    f.write_bytes(b"")
    assert pathfix_module.compute_sha256(f) == hashlib.sha256(b"").hexdigest()
