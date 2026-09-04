"""Tests for web/app.py's path validation - the two functions CodeQL flagged
(alert #103) and the ones the rest of the service's file access depends on.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest
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
