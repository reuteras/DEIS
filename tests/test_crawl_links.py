"""Tests for deis/crawl_links.py, the link-extraction/classification step
`deis crawl-site` uses to walk a directory-listing leak site.

This is the security-relevant part of that feature: getting relative-URL
resolution or the same-origin/same-subtree restriction wrong would mean the
crawl silently wanders off the site the operator asked it to walk - the
same "quiet, not loud" failure shape item 42's TOR egress preflight exists
to catch on the download side.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def crawl_links():
    spec = importlib.util.spec_from_file_location("crawl_links", REPO_ROOT / "deis" / "crawl_links.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TestExtractLinks:
    def test_classifies_dirs_and_files_by_trailing_slash(self, crawl_links):
        html = """
        <a href="subfolder/">subfolder/</a>
        <a href="report.pdf">report.pdf</a>
        """
        result = crawl_links.extract_links(html, "https://example.onion/case/", "https://example.onion/case/")
        assert result["dirs"] == ["https://example.onion/case/subfolder/"]
        assert result["files"] == ["https://example.onion/case/report.pdf"]

    def test_resolves_relative_links_against_page_url(self, crawl_links):
        html = '<a href="../sibling/">sibling/</a>'
        result = crawl_links.extract_links(
            html, "https://example.onion/case/sub/", "https://example.onion/case/"
        )
        assert result["dirs"] == ["https://example.onion/case/sibling/"]

    def test_drops_links_outside_root_origin(self, crawl_links):
        html = """
        <a href="https://evil.example/steal/">steal/</a>
        <a href="//evil.example/steal2/">steal2/</a>
        """
        result = crawl_links.extract_links(html, "https://example.onion/case/", "https://example.onion/case/")
        assert result["dirs"] == []
        assert result["files"] == []

    def test_drops_links_outside_root_path_subtree(self, crawl_links):
        # A "Parent Directory" link that would walk above the crawl root -
        # same host, but a different, unrelated part of the site.
        html = '<a href="../../other-case/">other-case/</a>'
        result = crawl_links.extract_links(
            html, "https://example.onion/leaks/case1/", "https://example.onion/leaks/case1/"
        )
        assert result["dirs"] == []
        assert result["files"] == []

    def test_different_port_is_not_same_origin(self, crawl_links):
        html = '<a href="https://example.onion:8080/case/x">x</a>'
        result = crawl_links.extract_links(html, "https://example.onion/case/", "https://example.onion/case/")
        assert result["files"] == []

    def test_drops_sort_links_that_are_the_same_page(self, crawl_links):
        # Apache mod_autoindex column-sort links: same path, only the query
        # string differs - not a distinct resource to queue.
        html = '<a href="?C=N;O=D">Name</a><a href="report.pdf">report.pdf</a>'
        result = crawl_links.extract_links(html, "https://example.onion/case/", "https://example.onion/case/")
        assert result["files"] == ["https://example.onion/case/report.pdf"]

    def test_query_string_stripped_from_queued_file_url(self, crawl_links):
        html = '<a href="report.pdf?dl=1">report.pdf</a>'
        result = crawl_links.extract_links(html, "https://example.onion/case/", "https://example.onion/case/")
        assert result["files"] == ["https://example.onion/case/report.pdf"]

    def test_dedupes_and_sorts_results(self, crawl_links):
        html = """
        <a href="b.pdf">b</a>
        <a href="a.pdf">a</a>
        <a href="a.pdf">a again</a>
        """
        result = crawl_links.extract_links(html, "https://example.onion/case/", "https://example.onion/case/")
        assert result["files"] == [
            "https://example.onion/case/a.pdf",
            "https://example.onion/case/b.pdf",
        ]

    def test_ignores_non_anchor_tags(self, crawl_links):
        html = '<link rel="stylesheet" href="style.css"><a href="report.pdf">r</a>'
        result = crawl_links.extract_links(html, "https://example.onion/case/", "https://example.onion/case/")
        assert result["files"] == ["https://example.onion/case/report.pdf"]

    def test_no_links_returns_empty_lists(self, crawl_links):
        result = crawl_links.extract_links(
            "<html><body>nothing here</body></html>",
            "https://example.onion/case/",
            "https://example.onion/case/",
        )
        assert result == {"dirs": [], "files": []}
