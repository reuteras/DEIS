"""Shared fixtures for importing this project's per-service scripts as
modules under test, without needing the real containerized environment each
was written to run in.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ingest_module(tmp_path, monkeypatch):
    """A fresh import of ingest/ingest.py, with its module-level config
    loading (deis.cfg, extracted/sha256, still_encrypted.txt/still_corrupt.txt)
    pointed at an isolated temp directory instead of the real repo.
    """
    (tmp_path / "deis.cfg").write_text(
        "[elastic]\npassword=test-password\n[ingest]\nfiles=./extracted/files/\nmax_size=1000000\nuse_sqlite=False\n"
    )
    (tmp_path / "extracted").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ELASTIC_PASSWORD", raising=False)
    return _load_module("deis_ingest", REPO_ROOT / "ingest" / "ingest.py")
