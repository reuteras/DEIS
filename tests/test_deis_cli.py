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


class TestEntitiesMaxChars:
    def test_missing_file_falls_back_to_default(self, deis_module, tmp_path):
        assert deis_module.entities_max_chars(tmp_path / "missing.cfg") == 20000

    def test_missing_section_falls_back_to_default(self, deis_module, tmp_path):
        cfg_file = tmp_path / "deis.cfg"
        cfg_file.write_text("[ingest]\nmax_size=1\n")
        assert deis_module.entities_max_chars(cfg_file) == 20000

    def test_reads_configured_value(self, deis_module, tmp_path):
        cfg_file = tmp_path / "deis.cfg"
        cfg_file.write_text("[entities]\nmax_chars=5000\n")
        assert deis_module.entities_max_chars(cfg_file) == 5000

    def test_zero_means_uncapped(self, deis_module, tmp_path):
        cfg_file = tmp_path / "deis.cfg"
        cfg_file.write_text("[entities]\nmax_chars=0\n")
        assert deis_module.entities_max_chars(cfg_file) == 0


class TestMapsUseElasticMapData:
    def test_missing_file_defaults_to_false(self, deis_module, tmp_path):
        assert deis_module.maps_use_elastic_map_data(tmp_path / "missing.cfg") is False

    def test_missing_section_defaults_to_false(self, deis_module, tmp_path):
        cfg_file = tmp_path / "deis.cfg"
        cfg_file.write_text("[ingest]\nmax_size=1\n")
        assert deis_module.maps_use_elastic_map_data(cfg_file) is False

    def test_reads_true(self, deis_module, tmp_path):
        cfg_file = tmp_path / "deis.cfg"
        cfg_file.write_text("[maps]\nuse_elastic_map_data=true\n")
        assert deis_module.maps_use_elastic_map_data(cfg_file) is True

    def test_reads_false(self, deis_module, tmp_path):
        cfg_file = tmp_path / "deis.cfg"
        cfg_file.write_text("[maps]\nuse_elastic_map_data=false\n")
        assert deis_module.maps_use_elastic_map_data(cfg_file) is False


class TestMarkerStatus:
    def test_all_waiting_on_empty_tree(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        (tmp_path / "status").mkdir()
        status = deis_module.marker_status()
        assert status == {"download": "not running", "extract": "waiting", "ingest": "waiting"}

    def test_full_pipeline_done(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "downloaded").touch()
        (status_dir / "extract_done").touch()
        (status_dir / "ingest_done").touch()
        status = deis_module.marker_status()
        assert status == {"download": "done", "extract": "done", "ingest": "done"}

    def test_download_failed_takes_priority(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "downloaded").touch()
        (status_dir / "download_failed").touch()
        assert deis_module.marker_status()["download"] == "failed"

    def test_extract_pending_once_unpack_marker_exists(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "unpack").touch()
        assert deis_module.marker_status()["extract"] == "pending"

    def test_extract_running_once_extracting_marker_exists(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "unpack").touch()
        (status_dir / "extracting").touch()
        assert deis_module.marker_status()["extract"] == "running"

    def test_ingest_pending_once_extract_done(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "extract_done").touch()
        assert deis_module.marker_status()["ingest"] == "pending"

    def test_ingest_running_once_ingesting_marker_exists(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        status_dir = tmp_path / "status"
        status_dir.mkdir()
        (status_dir / "extract_done").touch()
        (status_dir / "ingesting").touch()
        assert deis_module.marker_status()["ingest"] == "running"


class TestWithLivenessMarker:
    def test_marker_exists_only_while_running(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        (tmp_path / "status").mkdir()
        marker = tmp_path / "status" / "pii_scanning"
        seen_during_call = {}

        def inner(_args):
            seen_during_call["exists"] = marker.exists()
            return 0

        wrapped = deis_module._with_liveness_marker("pii_scanning", inner)
        assert wrapped(None) == 0
        assert seen_during_call["exists"] is True
        assert not marker.exists()

    def test_marker_removed_even_on_exception(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        (tmp_path / "status").mkdir()
        marker = tmp_path / "status" / "entity_scanning"

        def inner(_args):
            raise RuntimeError("boom")

        wrapped = deis_module._with_liveness_marker("entity_scanning", inner)
        with pytest.raises(RuntimeError):
            wrapped(None)
        assert not marker.exists()


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


class TestBulkFailures:
    """_bulk answers 200 OK even when every item in it failed, so the only
    signal is the response body - see bin/deis.py's bulk_failures().
    """

    def test_no_errors_flag_is_success(self, deis_module):
        assert deis_module.bulk_failures({"took": 3, "errors": False, "items": []}) == []

    def test_reports_each_failed_item(self, deis_module):
        response = {
            "errors": True,
            "items": [
                {"update": {"_id": "aaa", "status": 200}},
                {"update": {"_id": "bbb", "status": 409, "error": {"reason": "version conflict"}}},
                {"update": {"_id": "ccc", "status": 400, "error": {"reason": "mapper_parsing_exception"}}},
            ],
        }
        failures = deis_module.bulk_failures(response)
        assert failures == ["bbb: version conflict", "ccc: mapper_parsing_exception"]

    def test_missing_items_key_does_not_crash(self, deis_module):
        assert deis_module.bulk_failures({"errors": True}) == []


class TestSearchSnippets:
    """cmd_search's highlighted-snippet helpers (item 36) - see
    _highlight_fragment/_rich_snippet/_plain_snippet.
    """

    def test_highlight_fragment_reads_the_only_highlighted_field(self, deis_module):
        hit = {"highlight": {"attachment.content": ["some <em>match</em> here"]}}
        assert deis_module._highlight_fragment(hit) == "some <em>match</em> here"

    def test_highlight_fragment_missing_is_empty_not_an_error(self, deis_module):
        assert deis_module._highlight_fragment({}) == ""

    def test_rich_snippet_converts_em_tags_to_rich_markup(self, deis_module):
        hit = {"highlight": {"attachment.content": ["some <em>match</em> here"]}}
        assert deis_module._rich_snippet(hit) == "some [bold yellow]match[/bold yellow] here"

    def test_rich_snippet_escapes_literal_brackets_first(self, deis_module):
        # A real document containing a literal "[" must not be misread as
        # the start of one of rich's own style tags.
        hit = {"highlight": {"attachment.content": ["price <em>list</em> [draft]"]}}
        assert deis_module._rich_snippet(hit) == r"price [bold yellow]list[/bold yellow] \[draft]"

    def test_plain_snippet_strips_em_tags_entirely(self, deis_module):
        hit = {"highlight": {"attachment.content": ["some <em>match</em> here"]}}
        assert deis_module._plain_snippet(hit) == "some match here"


class TestBuildManifest:
    """_build_manifest (deis archive/deis restore, case handoff) - pure
    apart from a best-effort `git rev-parse HEAD` and reading .env for
    ELASTIC_VERSION.
    """

    def test_includes_every_passed_in_fact(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        (tmp_path / ".env").write_text("ELASTIC_VERSION=9.5.3\n")

        manifest = deis_module._build_manifest(
            images=["deis-elasticsearch:latest"],
            missing_images=["deis-kibana:latest"],
            counts={"unique_sha256": 100, "elasticsearch_documents": 100},
        )

        assert manifest["images"] == ["deis-elasticsearch:latest"]
        assert manifest["missing_images"] == ["deis-kibana:latest"]
        assert manifest["elastic_version"] == "9.5.3"
        assert manifest["counts"] == {"unique_sha256": 100, "elasticsearch_documents": 100}
        assert "archived_at" in manifest

    def test_git_commit_is_unknown_outside_a_repo(self, deis_module, tmp_path, monkeypatch):
        # tmp_path is never itself a git repo - a real, deterministic way
        # to exercise the "git rev-parse failed" fallback without mocking
        # subprocess.
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        (tmp_path / ".env").write_text("ELASTIC_VERSION=9.5.3\n")

        manifest = deis_module._build_manifest([], [], {})

        assert manifest["deis_commit"] == "unknown"


class TestManifestMismatches:
    """_manifest_mismatches - a restored instance's live funnel counts vs.
    what the archive's own manifest recorded, see cmd_restore.
    """

    def test_no_mismatches_when_counts_match(self, deis_module):
        manifest = {"counts": {"unique_sha256": 100, "elasticsearch_documents": 100}}
        live = {"unique_sha256": 100, "elasticsearch_documents": 100}
        assert deis_module._manifest_mismatches(manifest, live) == []

    def test_reports_each_differing_count(self, deis_module):
        manifest = {"counts": {"unique_sha256": 100, "elasticsearch_documents": 100}}
        live = {"unique_sha256": 95, "elasticsearch_documents": 100}
        assert deis_module._manifest_mismatches(manifest, live) == ["unique_sha256: archived 100, now 95"]

    def test_missing_manifest_counts_key_is_empty_not_an_error(self, deis_module):
        assert deis_module._manifest_mismatches({}, {"unique_sha256": 1}) == []


class TestArchiveCommitMismatch:
    """_archive_commit_mismatch - whether cmd_restore should switch this
    checkout to the archive's own commit.
    """

    def test_true_when_commits_differ(self, deis_module):
        assert deis_module._archive_commit_mismatch("abc123", "def456") is True

    def test_false_when_commits_match(self, deis_module):
        assert deis_module._archive_commit_mismatch("abc123", "abc123") is False

    def test_false_when_archive_commit_unknown(self, deis_module):
        assert deis_module._archive_commit_mismatch("unknown", "def456") is False

    def test_false_when_current_commit_empty(self, deis_module):
        assert deis_module._archive_commit_mismatch("abc123", "") is False

    def test_false_when_archive_commit_missing(self, deis_module):
        assert deis_module._archive_commit_mismatch("", "def456") is False


class TestInitEnvPermissions:
    def test_generated_env_is_not_world_readable(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        (tmp_path / ".env.default").write_text("ELASTIC_PASSWORD=changeme\nKIBANA_PASSWORD=changeme\n")
        (tmp_path / "deis.cfg.default").write_text("[unpack]\nunpack=true\n")

        deis_module.cmd_init(argparse.Namespace())

        env_path = tmp_path / ".env"
        assert env_path.stat().st_mode & 0o077 == 0
        # And the placeholder really was replaced, with a distinct secret per line.
        values = deis_module.read_env(env_path)
        assert "changeme" not in values.values()
        assert values["ELASTIC_PASSWORD"] != values["KIBANA_PASSWORD"]


class TestAddFiles:
    def test_copies_directory_and_sets_markers(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        source = tmp_path / "colleagues-copy"
        source.mkdir()
        (source / "a.txt").write_text("a")
        (source / "sub").mkdir()
        (source / "sub" / "b.txt").write_text("b")

        rc = deis_module.cmd_add_files(argparse.Namespace(source=[str(source)]))

        assert rc == 0
        assert (tmp_path / "files" / "a.txt").read_text() == "a"
        assert (tmp_path / "files" / "b.txt").read_text() == "b"
        for marker in ("added_urls", "downloaded", "unpack"):
            assert (tmp_path / "status" / marker).exists()

    def test_name_collision_gets_dup_suffix(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        (tmp_path / "files").mkdir()
        (tmp_path / "files" / "a.txt").write_text("existing")
        source = tmp_path / "colleagues-copy"
        source.mkdir()
        (source / "a.txt").write_text("new")

        rc = deis_module.cmd_add_files(argparse.Namespace(source=[str(source)]))

        assert rc == 0
        assert (tmp_path / "files" / "a.txt").read_text() == "existing"
        assert (tmp_path / "files" / "a-dup2.txt").read_text() == "new"

    def test_missing_source_fails(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        rc = deis_module.cmd_add_files(argparse.Namespace(source=[str(tmp_path / "does-not-exist")]))
        assert rc == 1

    def test_empty_source_fails(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        source = tmp_path / "empty"
        source.mkdir()
        rc = deis_module.cmd_add_files(argparse.Namespace(source=[str(source)]))
        assert rc == 1

    def test_multiple_sources_are_all_copied(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        first = tmp_path / "first.rar"
        first.write_text("a")
        second = tmp_path / "second.rar"
        second.write_text("b")

        rc = deis_module.cmd_add_files(argparse.Namespace(source=[str(first), str(second)]))

        assert rc == 0
        assert (tmp_path / "files" / "first.rar").read_text() == "a"
        assert (tmp_path / "files" / "second.rar").read_text() == "b"

    def test_one_missing_among_multiple_sources_fails(self, deis_module, tmp_path, monkeypatch):
        monkeypatch.setattr(deis_module, "REPO_ROOT", tmp_path)
        present = tmp_path / "present.rar"
        present.write_text("a")
        missing = tmp_path / "missing.rar"

        rc = deis_module.cmd_add_files(argparse.Namespace(source=[str(present), str(missing)]))

        assert rc == 1
        assert not (tmp_path / "files").exists()


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
