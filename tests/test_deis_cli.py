"""Tests for bin/deis.py's pure logic: URL scheme validation, .env parsing,
and marker-file status/funnel-count computation. The parts that need a live
stack (doctor's Elasticsearch/Kibana health, search, run) are verified
manually against the running stack instead - see docs/IMPROVEMENTS.md's CLI
write-up.
"""

import argparse
import importlib.util
import shutil
import subprocess
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


class TestCompletionScripts:
    """SUBCOMMANDS/RUN_ONLY_CHOICES are the single source of truth for both
    build_parser() and the completion scripts - these tests catch the two
    drifting apart, and (where the shell is available) that the generated
    scripts are at least syntactically valid.
    """

    def test_subcommands_match_argparse(self, deis_module):
        parser = deis_module.build_parser()
        subparsers_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        assert set(subparsers_action.choices) == set(deis_module.SUBCOMMANDS)

    def test_bash_script_lists_every_subcommand(self, deis_module):
        script = deis_module._bash_completion_script()
        for subcommand in deis_module.SUBCOMMANDS:
            assert subcommand in script

    def test_zsh_script_lists_every_subcommand(self, deis_module):
        script = deis_module._zsh_completion_script()
        for subcommand in deis_module.SUBCOMMANDS:
            assert subcommand in script

    def test_bash_script_lists_run_only_choices(self, deis_module):
        script = deis_module._bash_completion_script()
        for choice in deis_module.RUN_ONLY_CHOICES:
            assert choice in script

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
    def test_bash_script_is_syntactically_valid(self, deis_module, tmp_path):
        script_path = tmp_path / "completion.bash"
        script_path.write_text(deis_module._bash_completion_script())
        subprocess.run(["bash", "-n", str(script_path)], check=True)

    @pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not available")
    def test_zsh_script_is_syntactically_valid(self, deis_module, tmp_path):
        script_path = tmp_path / "completion.zsh"
        script_path.write_text(deis_module._zsh_completion_script())
        subprocess.run(["zsh", "-n", str(script_path)], check=True)

    @pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
    def test_bash_completion_offers_subcommands_and_run_choices(self, deis_module, tmp_path):
        script_path = tmp_path / "completion.bash"
        script_path.write_text(deis_module._bash_completion_script())
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"""
                source {script_path}
                COMP_WORDS=(deis "")
                COMP_CWORD=1
                _deis_completions
                echo "TOP:${{COMPREPLY[*]}}"
                COMP_WORDS=(deis run --only "")
                COMP_CWORD=3
                _deis_completions
                echo "ONLY:${{COMPREPLY[*]}}"
            """,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "TOP:" + " ".join(deis_module.SUBCOMMANDS) in result.stdout
        assert "ONLY:" + " ".join(deis_module.RUN_ONLY_CHOICES) in result.stdout
