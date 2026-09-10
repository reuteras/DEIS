"""Tests for web/app.py's path validation - the two functions CodeQL flagged
(alert #103) and the ones the rest of the service's file access depends on -
plus the landing page's status/funnel helpers.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
import requests
from fastapi import HTTPException

WEB_APP_PATH = Path(__file__).resolve().parent.parent / "web" / "app.py"


def _load_app_module():
    spec = importlib.util.spec_from_file_location("deis_web_app", WEB_APP_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app_module():
    return _load_app_module()


VALID_SHA256 = "a" * 64


class TestValidateSha256AndGetSymlinkPath:
    def test_valid_hash_returns_path_inside_symlinks_dir(self, app_module):
        result = app_module.validate_sha256_and_get_symlink_path(VALID_SHA256)
        assert result == os.path.join(app_module.SYMLINKS_DIR, VALID_SHA256)

    @pytest.mark.parametrize(
        "bad_value",
        [
            "",
            "short",
            "A" * 64,  # uppercase not allowed
            "g" * 64,  # not hex
            "a" * 63,  # too short
            "a" * 65,  # too long
            "../../etc/passwd",
            "a" * 64 + "\n",
            "a" * 64 + "/../../etc/passwd",
            '"><script>alert(1)</script>',
        ],
    )
    def test_invalid_input_rejected(self, app_module, bad_value):
        with pytest.raises(HTTPException) as excinfo:
            app_module.validate_sha256_and_get_symlink_path(bad_value)
        assert excinfo.value.status_code == 400

    def test_path_traversal_via_basename_is_neutralized(self, app_module):
        # basename() strips any directory component, so even a value that
        # slipped past the regex could never escape SYMLINKS_DIR by itself -
        # this only matters if the regex step were ever weakened, which is
        # exactly the kind of regression this test is meant to catch.
        traversal = "../" * 20 + VALID_SHA256
        with pytest.raises(HTTPException):
            app_module.validate_sha256_and_get_symlink_path(traversal)

    def test_result_never_escapes_symlinks_dir(self, app_module):
        result = app_module.validate_sha256_and_get_symlink_path(VALID_SHA256)
        base = os.path.normpath(app_module.SYMLINKS_DIR)
        assert result == base or result.startswith(base + os.sep)


class TestResolveAndVerifyTargetFile:
    def test_rejects_path_outside_symlinks_dir(self, app_module, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(tmp_path / "sha256"))
        with pytest.raises(HTTPException) as excinfo:
            app_module.resolve_and_verify_target_file("/somewhere/else/" + VALID_SHA256)
        assert excinfo.value.status_code == 400

    def test_rejects_missing_symlink(self, app_module, monkeypatch, tmp_path):
        symlinks_dir = tmp_path / "sha256"
        symlinks_dir.mkdir()
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(symlinks_dir))
        monkeypatch.setattr(app_module, "EXTRACTED_ROOT", str(tmp_path))
        with pytest.raises(HTTPException) as excinfo:
            app_module.resolve_and_verify_target_file(str(symlinks_dir / VALID_SHA256))
        assert excinfo.value.status_code == 404

    def test_rejects_a_regular_file_that_is_not_a_symlink(self, app_module, monkeypatch, tmp_path):
        symlinks_dir = tmp_path / "sha256"
        symlinks_dir.mkdir()
        (symlinks_dir / VALID_SHA256).write_text("not a symlink")
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(symlinks_dir))
        monkeypatch.setattr(app_module, "EXTRACTED_ROOT", str(tmp_path))
        with pytest.raises(HTTPException) as excinfo:
            app_module.resolve_and_verify_target_file(str(symlinks_dir / VALID_SHA256))
        assert excinfo.value.status_code == 404

    def test_rejects_symlink_resolving_outside_extracted_root(self, app_module, monkeypatch, tmp_path):
        symlinks_dir = tmp_path / "extracted" / "sha256"
        symlinks_dir.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("should never be reachable")
        (symlinks_dir / VALID_SHA256).symlink_to(outside)
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(symlinks_dir))
        monkeypatch.setattr(app_module, "EXTRACTED_ROOT", str(tmp_path / "extracted"))
        with pytest.raises(HTTPException) as excinfo:
            app_module.resolve_and_verify_target_file(str(symlinks_dir / VALID_SHA256))
        assert excinfo.value.status_code == 400

    def test_rejects_symlink_to_a_target_that_no_longer_exists(self, app_module, monkeypatch, tmp_path):
        extracted_root = tmp_path / "extracted"
        symlinks_dir = extracted_root / "sha256"
        symlinks_dir.mkdir(parents=True)
        target = extracted_root / "files" / "gone.txt"
        target.parent.mkdir(parents=True)
        target.write_text("will be deleted")
        (symlinks_dir / VALID_SHA256).symlink_to(target)
        target.unlink()
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(symlinks_dir))
        monkeypatch.setattr(app_module, "EXTRACTED_ROOT", str(extracted_root))
        with pytest.raises(HTTPException) as excinfo:
            app_module.resolve_and_verify_target_file(str(symlinks_dir / VALID_SHA256))
        assert excinfo.value.status_code == 404

    def test_accepts_a_valid_symlink_inside_extracted_root(self, app_module, monkeypatch, tmp_path):
        extracted_root = tmp_path / "extracted"
        symlinks_dir = extracted_root / "sha256"
        symlinks_dir.mkdir(parents=True)
        target = extracted_root / "files" / "real.txt"
        target.parent.mkdir(parents=True)
        target.write_text("real content")
        (symlinks_dir / VALID_SHA256).symlink_to(target)
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(symlinks_dir))
        monkeypatch.setattr(app_module, "EXTRACTED_ROOT", str(extracted_root))

        result = app_module.resolve_and_verify_target_file(str(symlinks_dir / VALID_SHA256))

        assert result == os.path.normpath(str(target))


class TestPipelineStatus:
    def test_nothing_written_yet(self, app_module, tmp_path):
        status = app_module.pipeline_status(str(tmp_path))
        assert status == {"download": "not running", "extract": "waiting", "ingest": "waiting"}

    def test_download_running(self, app_module, tmp_path):
        (tmp_path / "running").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status["download"] == "running"

    def test_download_failed_takes_priority_over_downloaded(self, app_module, tmp_path):
        (tmp_path / "downloaded").touch()
        (tmp_path / "download_failed").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status["download"] == "failed"

    def test_extract_pending_once_unpack_marker_exists(self, app_module, tmp_path):
        (tmp_path / "unpack").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status["extract"] == "pending"
        assert status["ingest"] == "waiting"

    def test_extract_running_once_extracting_marker_exists(self, app_module, tmp_path):
        (tmp_path / "unpack").touch()
        (tmp_path / "extracting").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status["extract"] == "running"

    def test_extract_done_takes_priority_over_extracting(self, app_module, tmp_path):
        (tmp_path / "unpack").touch()
        (tmp_path / "extracting").touch()
        (tmp_path / "extract_done").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status["extract"] == "done"

    def test_ingest_pending_once_extract_done(self, app_module, tmp_path):
        (tmp_path / "extract_done").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status["extract"] == "done"
        assert status["ingest"] == "pending"

    def test_ingest_running_once_ingesting_marker_exists(self, app_module, tmp_path):
        (tmp_path / "extract_done").touch()
        (tmp_path / "ingesting").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status["ingest"] == "running"

    def test_ingest_done_takes_priority_over_ingesting(self, app_module, tmp_path):
        (tmp_path / "extract_done").touch()
        (tmp_path / "ingesting").touch()
        (tmp_path / "ingest_done").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status["ingest"] == "done"

    def test_everything_done(self, app_module, tmp_path):
        (tmp_path / "downloaded").touch()
        (tmp_path / "extract_done").touch()
        (tmp_path / "ingest_done").touch()
        status = app_module.pipeline_status(str(tmp_path))
        assert status == {"download": "done", "extract": "done", "ingest": "done"}


class TestCountFiles:
    def test_missing_directory_is_zero(self, app_module, tmp_path):
        assert app_module.count_files(str(tmp_path / "does-not-exist")) == 0

    def test_counts_files_recursively(self, app_module, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "b.txt").write_text("y")
        assert app_module.count_files(str(tmp_path)) == 2

    def test_does_not_count_directories(self, app_module, tmp_path):
        (tmp_path / "subdir").mkdir()
        assert app_module.count_files(str(tmp_path)) == 0


class TestCountLines:
    def test_missing_file_is_zero(self, app_module, tmp_path):
        assert app_module.count_lines(str(tmp_path / "missing.txt")) == 0

    def test_counts_non_blank_lines(self, app_module, tmp_path):
        f = tmp_path / "hashes.txt"
        f.write_text("abc\n\ndef\n  \nghi\n")
        assert app_module.count_lines(str(f)) == 3


class TestElasticDocumentCount:
    def test_returns_none_without_password(self, app_module, monkeypatch):
        monkeypatch.delenv("ELASTIC_PASSWORD", raising=False)
        assert app_module.elastic_document_count() is None

    def test_returns_count_on_success(self, app_module, monkeypatch):
        monkeypatch.setenv("ELASTIC_PASSWORD", "secret")

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"count": 42}

        monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse())
        assert app_module.elastic_document_count() == 42

    def test_returns_none_when_unreachable(self, app_module, monkeypatch):
        monkeypatch.setenv("ELASTIC_PASSWORD", "secret")

        def raise_connection_error(*_a, **_kw):
            raise requests.exceptions.ConnectionError

        monkeypatch.setattr(app_module.requests, "get", raise_connection_error)
        assert app_module.elastic_document_count() is None


class TestLatestRunSummary:
    def test_returns_none_without_password(self, app_module, monkeypatch):
        monkeypatch.delenv("ELASTIC_PASSWORD", raising=False)
        assert app_module.latest_run_summary() is None

    def test_returns_none_when_no_hits(self, app_module, monkeypatch):
        monkeypatch.setenv("ELASTIC_PASSWORD", "secret")

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"hits": {"hits": []}}

        monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse())
        assert app_module.latest_run_summary() is None

    def test_returns_source_of_latest_hit(self, app_module, monkeypatch):
        monkeypatch.setenv("ELASTIC_PASSWORD", "secret")

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"hits": {"hits": [{"_source": {"indexed_this_run": 5}}]}}

        monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: FakeResponse())
        assert app_module.latest_run_summary() == {"indexed_this_run": 5}


class TestRenderIndexHtml:
    def test_renders_without_elasticsearch(self, app_module, monkeypatch, tmp_path):
        monkeypatch.delenv("ELASTIC_PASSWORD", raising=False)
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        monkeypatch.setattr(app_module, "FILES_DIR", str(tmp_path / "files"))
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(tmp_path / "sha256"))

        page = app_module.render_index_html()

        assert "DEIS" in page
        assert "could not be read" in page
        assert app_module.KIBANA_LINK in page
        assert app_module.JUPYTER_LINK in page
        assert app_module.DOWNLOAD_STATUS_LINK in page

    def test_still_counts_shown_only_when_nonzero(self, app_module, monkeypatch, tmp_path):
        monkeypatch.delenv("ELASTIC_PASSWORD", raising=False)
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        monkeypatch.setattr(app_module, "FILES_DIR", str(tmp_path / "files"))
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(tmp_path / "sha256"))
        (tmp_path / "still_encrypted.txt").write_text("abc\n")

        page = app_module.render_index_html()

        assert "Still encrypted" in page
        assert "Still corrupt" not in page
        assert "Stuck multi-volume parts" not in page
        assert "Recovered by password-cracking" not in page

    def test_still_multivolume_count_shown_when_nonzero(self, app_module, monkeypatch, tmp_path):
        monkeypatch.delenv("ELASTIC_PASSWORD", raising=False)
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        monkeypatch.setattr(app_module, "FILES_DIR", str(tmp_path / "files"))
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(tmp_path / "sha256"))
        (tmp_path / "still_multivolume.txt").write_text("abc\n")

        page = app_module.render_index_html()

        assert "Stuck multi-volume parts" in page

    def test_decrypted_count_shown_when_nonzero(self, app_module, monkeypatch, tmp_path):
        monkeypatch.delenv("ELASTIC_PASSWORD", raising=False)
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        monkeypatch.setattr(app_module, "FILES_DIR", str(tmp_path / "files"))
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(tmp_path / "sha256"))
        (tmp_path / "decrypted.txt").write_text("abc\n")

        page = app_module.render_index_html()

        assert "Recovered by password-cracking" in page

    def test_scan_progress_and_liveness_shown(self, app_module, monkeypatch, tmp_path):
        monkeypatch.setenv("ELASTIC_PASSWORD", "secret")
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        monkeypatch.setattr(app_module, "FILES_DIR", str(tmp_path / "files"))
        monkeypatch.setattr(app_module, "SYMLINKS_DIR", str(tmp_path / "sha256"))
        (tmp_path / "pii_scanning").touch()
        (tmp_path / "dedupe_scan_summary.json").write_text(
            '{"@timestamp": "2026-09-10T00:00:00+00:00", "documents_clustered": 12, "cluster_count": 3}'
        )

        def fake_get(url, **_kw):
            class FakeResponse:
                status_code = 200

                def json(self):
                    return {"count": 100}

            return FakeResponse()

        def fake_post(url, **_kw):
            class FakeResponse:
                status_code = 200

                def json(self):
                    return {"count": 40}

            return FakeResponse()

        monkeypatch.setattr(app_module.requests, "get", fake_get)
        monkeypatch.setattr(app_module.requests, "post", fake_post)

        page = app_module.render_index_html()

        assert "PII scan" in page
        assert "40 / 100" in page
        assert "(running)" in page
        assert "12 document(s) in 3 cluster(s)" in page


class TestElasticScanProgress:
    def test_returns_none_without_password(self, app_module, monkeypatch):
        monkeypatch.delenv("ELASTIC_PASSWORD", raising=False)
        assert app_module.elastic_scan_progress("pii.has_pii") is None

    def test_returns_tagged_and_total(self, app_module, monkeypatch):
        monkeypatch.setenv("ELASTIC_PASSWORD", "secret")

        class TotalResponse:
            status_code = 200

            def json(self):
                return {"count": 100}

        class TaggedResponse:
            status_code = 200

            def json(self):
                return {"count": 25}

        monkeypatch.setattr(app_module.requests, "get", lambda *a, **kw: TotalResponse())
        monkeypatch.setattr(app_module.requests, "post", lambda *a, **kw: TaggedResponse())
        assert app_module.elastic_scan_progress("pii.has_pii") == (25, 100)

    def test_returns_none_when_unreachable(self, app_module, monkeypatch):
        monkeypatch.setenv("ELASTIC_PASSWORD", "secret")

        def raise_connection_error(*_a, **_kw):
            raise requests.exceptions.ConnectionError

        monkeypatch.setattr(app_module.requests, "get", raise_connection_error)
        assert app_module.elastic_scan_progress("pii.has_pii") is None


class TestDedupeScanSummary:
    def test_returns_none_when_missing(self, app_module, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        assert app_module.dedupe_scan_summary() is None

    def test_returns_parsed_summary(self, app_module, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        (tmp_path / "dedupe_scan_summary.json").write_text('{"cluster_count": 5}')
        assert app_module.dedupe_scan_summary() == {"cluster_count": 5}

    def test_returns_none_on_invalid_json(self, app_module, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        (tmp_path / "dedupe_scan_summary.json").write_text("not json")
        assert app_module.dedupe_scan_summary() is None


class TestScanRunning:
    def test_false_when_marker_absent(self, app_module, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        assert app_module.scan_running("pii_scanning") is False

    def test_true_when_marker_present(self, app_module, monkeypatch, tmp_path):
        monkeypatch.setattr(app_module, "STATUS_DIR", str(tmp_path))
        (tmp_path / "pii_scanning").touch()
        assert app_module.scan_running("pii_scanning") is True
