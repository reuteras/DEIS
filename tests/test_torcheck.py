"""Tests for the download stage's TOR routing decision (deis/lib.sh) and the
preflight leak test built on it (deis/torcheck.sh, item 42).

url_needs_tor() is the security-critical function in the pipeline: it decides
both whether a URL is fetched through TOR and whether a broken proxy chain is
grounds for refusing the batch. Getting it wrong in the permissive direction
leaks the operator's real address, so the cases below lean on the ways a URL
can look like a hidden service without being one.

Driven through real bash subprocesses rather than reimplemented in Python -
the shell is what actually runs in the container.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB = REPO_ROOT / "deis" / "lib.sh"
TORCHECK = REPO_ROOT / "deis" / "torcheck.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def run_lib(snippet: str, env: dict[str, str] | None = None) -> str:
    """Runs a snippet with deis/lib.sh sourced, returning its stdout."""
    result = subprocess.run(
        ["bash", "-c", f'source "{LIB}"\n{snippet}'],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", **(env or {})},
    )
    return result.stdout.strip()


class TestUrlHost:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("http://example.com/file.zip", "example.com"),
            ("https://example.com:8443/file.zip", "example.com"),
            ("ftp://user:pass@example.com/file.zip", "example.com"),
            ("http://abcdef1234567890.onion/dump.rar", "abcdef1234567890.onion"),
            ("https://user:p@ss@example.com:443/x", "example.com"),
        ],
    )
    def test_extracts_host(self, url, expected):
        assert run_lib(f'url_host "{url}"') == expected


class TestUrlNeedsTor:
    @pytest.mark.parametrize(
        "url",
        [
            "http://abcdef1234567890.onion/dump.rar",
            "https://sub.domain.abcdef.onion/x.zip",
            "http://abcdef.onion:8080/x.zip",
            "http://user:pass@abcdef.onion/x.zip",
        ],
    )
    def test_real_onion_needs_tor(self, url):
        assert run_lib(f'url_needs_tor "{url}" && echo yes || echo no') == "yes"

    @pytest.mark.parametrize(
        "url",
        [
            # The whole reason host parsing exists: ".onion" in the path,
            # the query string, or a lookalike host must not route a
            # clearnet request through TOR - and, more importantly, must not
            # make the preflight think TOR is in play when it is not.
            "https://example.com/downloads/.onion/dump.rar",
            "https://example.com/x.zip?ref=abc.onion",
            "https://notreally.onion.example.com/x.zip",
            "https://example.com/onion.zip",
            "http://example.com/file.zip",
        ],
    )
    def test_clearnet_lookalikes_do_not_need_tor(self, url):
        assert run_lib(f'url_needs_tor "{url}" && echo yes || echo no') == "no"

    def test_force_tor_opts_everything_in(self):
        answer = run_lib(
            'url_needs_tor "http://example.com/file.zip" && echo yes || echo no',
            env={"FORCE_TOR": "true"},
        )
        assert answer == "yes"

    def test_force_tor_false_is_not_treated_as_set(self):
        answer = run_lib(
            'url_needs_tor "http://example.com/file.zip" && echo yes || echo no',
            env={"FORCE_TOR": "false"},
        )
        assert answer == "no"


class TestUrlIsBittorrent:
    @pytest.mark.parametrize(
        "url",
        [
            "magnet:?xt=urn:btih:abc",
            "http://example.com/file.torrent",
            "https://example.onion/leak.torrent",
            "http://example.com/file.torrent?dl=1",
            "HTTP://EXAMPLE.COM/FILE.TORRENT",
        ],
    )
    def test_recognizes_bittorrent_urls(self, url):
        assert run_lib(f'url_is_bittorrent "{url}" && echo yes || echo no') == "yes"

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/file.zip",
            "https://example.com/not-a-torrent",
            "http://example.com/file.torrent.txt",
            "ftp://example.com/x.zip",
        ],
    )
    def test_rejects_non_bittorrent_urls(self, url):
        assert run_lib(f'url_is_bittorrent "{url}" && echo yes || echo no') == "no"


class TestUrlBtRefused:
    """aria2 has no proxy support for BitTorrent traffic at all (see
    url_bt_refused's own docstring in lib.sh) - a magnet/.torrent URL is
    refused whenever this pipeline's TOR policy would otherwise apply to
    it, rather than silently downloading it unprotected.
    """

    def test_magnet_allowed_by_default(self):
        assert run_lib('url_bt_refused "magnet:?xt=urn:btih:abc" && echo yes || echo no') == "no"

    def test_magnet_refused_under_force_tor(self):
        answer = run_lib(
            'url_bt_refused "magnet:?xt=urn:btih:abc" && echo yes || echo no',
            env={"FORCE_TOR": "true"},
        )
        assert answer == "yes"

    def test_clearnet_torrent_file_allowed(self):
        assert run_lib('url_bt_refused "https://example.com/leak.torrent" && echo yes || echo no') == "no"

    def test_onion_hosted_torrent_file_refused(self):
        answer = run_lib('url_bt_refused "https://abcdef.onion/leak.torrent" && echo yes || echo no')
        assert answer == "yes"

    def test_onion_hosted_torrent_file_refused_even_without_force_tor(self):
        answer = run_lib(
            'url_bt_refused "https://abcdef.onion/leak.torrent" && echo yes || echo no',
            env={"FORCE_TOR": "false"},
        )
        assert answer == "yes"


class TestTorcheckScript:
    def test_is_syntactically_valid(self):
        subprocess.run(["bash", "-n", str(TORCHECK)], check=True)

    @pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not available")
    def test_shellcheck_clean(self):
        subprocess.run(["shellcheck", str(TORCHECK), str(LIB)], check=True)

    def test_probe_is_written_outside_the_swept_download_tree(self):
        """done.sh sweeps /downloader/data into /files, where anything it
        finds is indexed as evidence. The probe must therefore land in one
        of the directories done.sh prunes - alongside deis/crawl.sh's own
        .crawl (its fetched listing pages are not leak data either).
        """
        torcheck = TORCHECK.read_text(encoding="utf-8")
        done = (REPO_ROOT / "deis" / "done.sh").read_text(encoding="utf-8")
        # Overridable for host-side testing, but the default - which is what
        # runs in the container - must be a pruned directory.
        assert 'PROBE_DIR_LOCAL="${PROBE_DIR_LOCAL:-/downloader/data/.torcheck}"' in torcheck
        assert "-name .torcheck" in done
        assert "-name .crawl" in done
        assert done.count("-prune") == 2, "both find calls in done.sh must prune these directories"

    def test_probe_overrides_infinite_retries(self):
        """aria2.conf sets max-tries=0 (retry forever), which is right for a
        real download over a flaky circuit but would make an unreachable
        probe hang instead of failing the preflight.
        """
        assert '"max-tries": "1"' in TORCHECK.read_text(encoding="utf-8")

    def test_routing_decision_is_not_duplicated(self):
        """torcheck.sh must decide "does this need TOR" with the same
        function addurl.sh routes with, or the preflight would be checking
        something other than what the downloader does.
        """
        for script in ("torcheck.sh", "addurl.sh"):
            text = (REPO_ROOT / "deis" / script).read_text(encoding="utf-8")
            assert "source" in text and "lib.sh" in text, f"{script} must source lib.sh"
            assert "url_needs_tor" in text, f"{script} must use the shared routing decision"
            assert "*.onion" not in text, f"{script} must not re-implement the .onion test"

    def test_bt_refusal_is_not_duplicated(self):
        """Same idea as the routing decision above, for the "BitTorrent
        can never be proxied" refusal: it must live once in lib.sh
        (url_bt_refused), not be reimplemented ad hoc in urls.sh.
        """
        text = (REPO_ROOT / "deis" / "urls.sh").read_text(encoding="utf-8")
        assert "source" in text and "lib.sh" in text, "urls.sh must source lib.sh"
        assert "url_is_bittorrent" in text and "url_bt_refused" in text, "urls.sh must use the shared BitTorrent checks"
