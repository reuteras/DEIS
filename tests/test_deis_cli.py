"""Tests for bin/deis.py's pure logic: URL scheme validation, .env parsing,
and marker-file status/funnel-count computation. The parts that need a live
stack (doctor's Elasticsearch/Kibana health, search, run) are verified
manually against the running stack instead - see docs/IMPROVEMENTS.md's CLI
write-up.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def deis_module():
    spec = importlib.util.spec_from_file_location("deis_cli", REPO_ROOT / "bin" / "deis.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestIsValidUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/file.zip",
            "https://example.com/file.zip",
            "ftp://example.com/file.zip",
        ],
    )
    def test_allowed_schemes(self, deis_module, url):
        assert deis_module.is_valid_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "magnet:?xt=urn:btih:abc",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "example.com/file.zip",
            "",
        ],
    )
    def test_rejected_schemes(self, deis_module, url):
        assert deis_module.is_valid_url(url) is False


class TestReadEnv:
    def test_missing_file_yields_empty_dict(self, deis_module, tmp_path):
        assert deis_module.read_env(tmp_path / "missing.env") == {}

    def test_parses_key_value_lines(self, deis_module, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("ELASTIC_PASSWORD=hunter2\nJUPYTER_TOKEN=abc123\n")
        assert deis_module.read_env(env_file) == {
            "ELASTIC_PASSWORD": "hunter2",
            "JUPYTER_TOKEN": "abc123",
        }

    def test_skips_blank_and_comment_lines(self, deis_module, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# a comment\n\nELASTIC_PASSWORD=hunter2\n")
        assert deis_module.read_env(env_file) == {"ELASTIC_PASSWORD": "hunter2"}


class TestMarkerStatus:
    def test_all_waiting_on_empty_tree(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        (tmp_path / "files").mkdir()
        (tmp_path / "extracted").mkdir()
        status = deis_module.marker_status()
        assert status == {"download": "not running", "extract": "waiting", "ingest": "waiting"}

    def test_full_pipeline_done(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        files = tmp_path / "files"
        extracted = tmp_path / "extracted"
        files.mkdir()
        (extracted / "files").mkdir(parents=True)
        (files / "downloaded").touch()
        (extracted / "files" / "done").touch()
        (extracted / "ingest_done").touch()
        status = deis_module.marker_status()
        assert status == {"download": "done", "extract": "done", "ingest": "done"}

    def test_download_failed_takes_priority(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        files = tmp_path / "files"
        files.mkdir()
        (tmp_path / "extracted").mkdir()
        (files / "downloaded").touch()
        (files / "download_failed").touch()
        assert deis_module.marker_status()["download"] == "failed"


class TestCountFiles:
    def test_missing_directory_is_zero(self, deis_module, tmp_path):
        assert deis_module.count_files(tmp_path / "missing", exclude=set()) == 0

    def test_counts_files_recursively(self, deis_module, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_text("b")
        assert deis_module.count_files(tmp_path, exclude=set()) == 2

    def test_excludes_named_bookkeeping_files(self, deis_module, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "done").touch()
        (tmp_path / "path.txt").touch()
        assert deis_module.count_files(tmp_path, exclude={"done", "path.txt"}) == 1
