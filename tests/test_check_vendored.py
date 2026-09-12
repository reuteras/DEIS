"""Tests for bin/check_vendored.py's pure logic: pin-parsing and the
regex/string substitutions upgrade functions apply. Network calls
(pypi_latest_version, github_latest_release_tag, ...) are exercised
manually against the real APIs instead (see this project's own testing
convention - tests/test_deis_cli.py's module docstring) - a live version
check is exactly the kind of thing worth verifying against the real
service rather than a mock that can drift from what the API actually
returns.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_check_vendored", REPO_ROOT / "bin" / "check_vendored.py")
cv = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cv
spec.loader.exec_module(cv)


class TestGithubTokenHeader:
    """_http_json's Authorization header (see GITHUB_TOKEN's own comment:
    a developer-only, --github-token/$GITHUB_TOKEN-supplied convenience
    against GitHub's 60 req/hour unauthenticated limit - never from
    deis.cfg). Captures the built urllib.request.Request instead of
    hitting the network.
    """

    def test_attaches_bearer_token_for_github_api(self, monkeypatch):
        monkeypatch.setattr(cv, "GITHUB_TOKEN", "test-token-123")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["request"] = req

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self):
                    return b"{}"

            return FakeResponse()

        monkeypatch.setattr(cv.json, "load", lambda f: {})
        monkeypatch.setattr(cv.urllib.request, "urlopen", fake_urlopen)
        cv._http_json("https://api.github.com/repos/x/y/releases/latest")
        assert captured["request"].get_header("Authorization") == "Bearer test-token-123"

    def test_does_not_attach_token_to_non_github_hosts(self, monkeypatch):
        monkeypatch.setattr(cv, "GITHUB_TOKEN", "test-token-123")
        captured = {}

        def fake_urlopen(req, timeout):
            captured["request"] = req

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return FakeResponse()

        monkeypatch.setattr(cv.json, "load", lambda f: {})
        monkeypatch.setattr(cv.urllib.request, "urlopen", fake_urlopen)
        cv._http_json("https://pypi.org/pypi/spacy/json")
        assert captured["request"].get_header("Authorization") is None

    def test_no_token_means_no_header(self, monkeypatch):
        monkeypatch.setattr(cv, "GITHUB_TOKEN", None)
        captured = {}

        def fake_urlopen(req, timeout):
            captured["request"] = req

            class FakeResponse:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return FakeResponse()

        monkeypatch.setattr(cv.json, "load", lambda f: {})
        monkeypatch.setattr(cv.urllib.request, "urlopen", fake_urlopen)
        cv._http_json("https://api.github.com/repos/x/y/releases/latest")
        assert captured["request"].get_header("Authorization") is None


class TestSevenzipDisplay:
    def test_formats_raw_four_digit_version(self):
        assert cv._sevenzip_display("2603") == "26.03"

    def test_formats_leading_zero_minor(self):
        assert cv._sevenzip_display("2401") == "24.01"


class TestGithubLatestMatchingReleaseTag:
    def test_picks_highest_numeric_version_not_string_sort(self, monkeypatch):
        # String-sorted, "3.9.0" would (wrongly) sort after "3.10.0" -
        # this must compare the numeric parts instead.
        releases = [{"tag_name": t} for t in ("en_core_web_sm-3.9.0", "en_core_web_sm-3.10.0", "en_core_web_sm-3.2.0")]
        monkeypatch.setattr(cv, "_http_json", lambda url: releases)
        assert cv.github_latest_matching_release_tag("x/y", "en_core_web_sm-") == "en_core_web_sm-3.10.0"

    def test_ignores_non_matching_tags(self, monkeypatch):
        releases = [{"tag_name": "sv_core_news_sm-3.8.0"}, {"tag_name": "en_core_web_sm-3.8.0"}]
        monkeypatch.setattr(cv, "_http_json", lambda url: releases)
        assert cv.github_latest_matching_release_tag("x/y", "en_core_web_sm-") == "en_core_web_sm-3.8.0"

    def test_returns_none_when_nothing_matches(self, monkeypatch):
        monkeypatch.setattr(cv, "_http_json", lambda url: [{"tag_name": "sv_core_news_sm-3.8.0"}])
        assert cv.github_latest_matching_release_tag("x/y", "en_core_web_sm-") is None


class TestSpacyPin:
    def test_current_reads_pinned_version(self, tmp_path, monkeypatch):
        f = tmp_path / "pyproject.toml"
        f.write_text('dependencies = [\n    "spacy==3.8.16",\n]\n')
        monkeypatch.setattr(cv, "BIN_PYPROJECT", f)
        assert cv.spacy_current() == "3.8.16"

    def test_current_missing_pin_raises(self, tmp_path, monkeypatch):
        f = tmp_path / "pyproject.toml"
        f.write_text("dependencies = []\n")
        monkeypatch.setattr(cv, "BIN_PYPROJECT", f)
        with pytest.raises(RuntimeError, match="not find"):
            cv.spacy_current()

    def test_upgrade_replaces_pin(self, tmp_path, monkeypatch):
        f = tmp_path / "pyproject.toml"
        f.write_text('dependencies = [\n    "spacy==3.8.16",\n]\n')
        monkeypatch.setattr(cv, "BIN_PYPROJECT", f)
        notes = cv.spacy_upgrade("3.9.0")
        assert '"spacy==3.9.0"' in f.read_text()
        assert any("3.8.16 -> 3.9.0" in note for note in notes)

    def test_upgrade_same_version_still_succeeds(self, tmp_path, monkeypatch):
        # Regression test: the first implementation of this (and
        # msoffcrypto_upgrade) checked "did replacing change the text"
        # rather than "was the old pin present" - a same-version re-pin
        # (--force against an already-up-to-date item) leaves the text
        # byte-identical even on a real, successful substitution, and
        # was wrongly reported as "pin not found". Confirmed live against
        # this project's own already-current pins before being fixed.
        f = tmp_path / "pyproject.toml"
        f.write_text('dependencies = [\n    "spacy==3.8.16",\n]\n')
        monkeypatch.setattr(cv, "BIN_PYPROJECT", f)
        notes = cv.spacy_upgrade("3.8.16")
        assert '"spacy==3.8.16"' in f.read_text()
        assert any("3.8.16 -> 3.8.16" in note for note in notes)

    def test_upgrade_missing_pin_raises_without_writing(self, tmp_path, monkeypatch):
        f = tmp_path / "pyproject.toml"
        original = 'dependencies = [\n    "spacy==3.8.16",\n]\n'
        f.write_text(original)
        monkeypatch.setattr(cv, "BIN_PYPROJECT", f)
        # Corrupt the file between current()'s read and upgrade()'s own -
        # simulated here by monkeypatching spacy_current directly, since
        # upgrade() calls it again itself.
        monkeypatch.setattr(cv, "spacy_current", lambda: "9.9.9")
        with pytest.raises(RuntimeError, match="not found to replace"):
            cv.spacy_upgrade("3.10.0")
        assert f.read_text() == original


class TestMsoffcryptoPin:
    def test_current_reads_pinned_version(self, tmp_path, monkeypatch):
        f = tmp_path / "Dockerfile"
        f.write_text('pip install --no-cache-dir "msoffcrypto-tool==6.0.0"\n')
        monkeypatch.setattr(cv, "UNPACK_DOCKERFILE", f)
        assert cv.msoffcrypto_current() == "6.0.0"

    def test_upgrade_same_version_still_succeeds(self, tmp_path, monkeypatch):
        f = tmp_path / "Dockerfile"
        f.write_text('pip install --no-cache-dir "msoffcrypto-tool==6.0.0"\n')
        monkeypatch.setattr(cv, "UNPACK_DOCKERFILE", f)
        notes = cv.msoffcrypto_upgrade("6.0.0")
        assert '"msoffcrypto-tool==6.0.0"' in f.read_text()
        assert any("6.0.0 -> 6.0.0" in note for note in notes)


class TestModelPin:
    _PYPROJECT_TEXT = (
        "[tool.uv.sources]\n"
        'en-core-web-sm = { url = "https://github.com/explosion/spacy-models/releases/'
        'download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl" }\n'
    )

    def test_current_reads_version_from_wheel_url(self, tmp_path, monkeypatch):
        f = tmp_path / "pyproject.toml"
        f.write_text(self._PYPROJECT_TEXT)
        monkeypatch.setattr(cv, "BIN_PYPROJECT", f)
        assert cv._model_current("en_core_web_sm") == "3.8.0"

    def test_upgrade_replaces_both_occurrences_in_url(self, tmp_path, monkeypatch):
        f = tmp_path / "pyproject.toml"
        f.write_text(self._PYPROJECT_TEXT)
        monkeypatch.setattr(cv, "BIN_PYPROJECT", f)
        cv._model_upgrade("en_core_web_sm", "3.9.0")
        text = f.read_text()
        assert "en_core_web_sm-3.9.0/en_core_web_sm-3.9.0-py3-none-any.whl" in text
        assert "3.8.0" not in text


class TestSevenzipPin:
    _INSTALL_SH = (
        'VERSION="2603"\n'
        "\n"
        'if [[ "$(uname -m)" == "aarch64" ]]; then\n'
        '    DOWNLOAD_ARCH="arm64"\n'
        '    SHA256="2389ba20e4d8295e8709c20b6263b69bd1ec4972fe38a04ad7a1badbf595b996"\n'
        "else\n"
        '    DOWNLOAD_ARCH="x64"\n'
        '    SHA256="dc99eff5008f1ab79bd7084c68513701547a808a89502bf4133683535ab3c695"\n'
        "fi\n"
    )

    def test_current_formats_display_version(self, tmp_path, monkeypatch):
        f = tmp_path / "install.sh"
        f.write_text(self._INSTALL_SH)
        monkeypatch.setattr(cv, "UNPACK_INSTALL_SH", f)
        assert cv.sevenzip_current() == "26.03"

    def test_upgrade_replaces_version_and_both_hashes(self, tmp_path, monkeypatch):
        f = tmp_path / "install.sh"
        f.write_text(self._INSTALL_SH)
        monkeypatch.setattr(cv, "UNPACK_INSTALL_SH", f)
        monkeypatch.setattr(cv, "_http_bytes", lambda url: url.encode())  # sha256 of the url itself, deterministic

        notes = cv.sevenzip_upgrade("27.00")
        text = f.read_text()
        assert 'VERSION="2700"' in text
        assert "2389ba20e4d8295e8709c20b6263b69bd1ec4972fe38a04ad7a1badbf595b996" not in text
        assert "dc99eff5008f1ab79bd7084c68513701547a808a89502bf4133683535ab3c695" not in text
        assert any("26.03 -> 27.00" in note for note in notes)


class TestNotebookPipPin:
    # A minimal stand-in for the real notebook's JSON - only the one
    # escaped shell-string substring _notebook_pip_current/_upgrade
    # actually look for, not a real nbformat structure.
    _NOTEBOOK_JSON = (
        '{"cells": [{"source": ["python3 -m pip -q install '
        '\\"elasticsearch==9.5.0\\" numpy pandas ipywidgets \\"wordcloud==1.9.6\\""]}]}'
    )

    def test_current_reads_elasticsearch_pin(self, tmp_path, monkeypatch):
        f = tmp_path / "Elastic.ipynb"
        f.write_text(self._NOTEBOOK_JSON)
        monkeypatch.setattr(cv, "NOTEBOOK_IPYNB", f)
        assert cv._notebook_pip_current("elasticsearch") == "9.5.0"

    def test_current_reads_wordcloud_pin(self, tmp_path, monkeypatch):
        f = tmp_path / "Elastic.ipynb"
        f.write_text(self._NOTEBOOK_JSON)
        monkeypatch.setattr(cv, "NOTEBOOK_IPYNB", f)
        assert cv._notebook_pip_current("wordcloud") == "1.9.6"

    def test_current_missing_pin_raises(self, tmp_path, monkeypatch):
        f = tmp_path / "Elastic.ipynb"
        f.write_text(self._NOTEBOOK_JSON)
        monkeypatch.setattr(cv, "NOTEBOOK_IPYNB", f)
        with pytest.raises(RuntimeError, match="not find"):
            cv._notebook_pip_current("numpy")  # unpinned in the real cell, no == to find

    def test_upgrade_replaces_only_the_named_package(self, tmp_path, monkeypatch):
        f = tmp_path / "Elastic.ipynb"
        f.write_text(self._NOTEBOOK_JSON)
        monkeypatch.setattr(cv, "NOTEBOOK_IPYNB", f)
        notes = cv._notebook_pip_upgrade("elasticsearch", "9.6.0")
        text = f.read_text()
        assert '\\"elasticsearch==9.6.0\\"' in text
        assert '\\"wordcloud==1.9.6\\"' in text  # untouched
        assert any("9.5.0 -> 9.6.0" in note for note in notes)

    def test_upgrade_same_version_still_succeeds(self, tmp_path, monkeypatch):
        f = tmp_path / "Elastic.ipynb"
        f.write_text(self._NOTEBOOK_JSON)
        monkeypatch.setattr(cv, "NOTEBOOK_IPYNB", f)
        notes = cv._notebook_pip_upgrade("wordcloud", "1.9.6")
        assert '\\"wordcloud==1.9.6\\"' in f.read_text()
        assert any("1.9.6 -> 1.9.6" in note for note in notes)


class TestElasticStackPin:
    _ENV_DEFAULT = "ELASTIC_VERSION=9.5.3\nELASTIC_PASSWORD=changeme\n"

    def test_current_reads_pinned_version(self, tmp_path, monkeypatch):
        f = tmp_path / ".env.default"
        f.write_text(self._ENV_DEFAULT)
        monkeypatch.setattr(cv, "ENV_DEFAULT", f)
        assert cv.elastic_stack_current() == "9.5.3"

    def test_current_missing_pin_raises(self, tmp_path, monkeypatch):
        f = tmp_path / ".env.default"
        f.write_text("ELASTIC_PASSWORD=changeme\n")
        monkeypatch.setattr(cv, "ENV_DEFAULT", f)
        with pytest.raises(RuntimeError, match="not find"):
            cv.elastic_stack_current()

    def test_latest_strips_leading_v_from_release_tag(self, monkeypatch):
        monkeypatch.setattr(cv, "github_latest_release_tag", lambda repo: "v9.6.0")
        assert cv.elastic_stack_latest() == "9.6.0"

    def test_upgrade_replaces_pin_without_touching_other_lines(self, tmp_path, monkeypatch):
        f = tmp_path / ".env.default"
        f.write_text(self._ENV_DEFAULT)
        monkeypatch.setattr(cv, "ENV_DEFAULT", f)
        notes = cv.elastic_stack_upgrade("9.6.0")
        text = f.read_text()
        assert "ELASTIC_VERSION=9.6.0" in text
        assert "ELASTIC_PASSWORD=changeme" in text
        assert any("9.5.3 -> 9.6.0" in note for note in notes)

    def test_upgrade_same_version_still_succeeds(self, tmp_path, monkeypatch):
        f = tmp_path / ".env.default"
        f.write_text(self._ENV_DEFAULT)
        monkeypatch.setattr(cv, "ENV_DEFAULT", f)
        notes = cv.elastic_stack_upgrade("9.5.3")
        assert "ELASTIC_VERSION=9.5.3" in f.read_text()
        assert any("9.5.3 -> 9.5.3" in note for note in notes)
