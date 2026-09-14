#!/usr/bin/env python3
"""Extracts and classifies the links on one downloaded directory-listing page
(an Apache/nginx "Index of /" or similar) for deis/crawl.sh.

Kept as a separate stdlib-only script rather than bash regex because getting
relative-link resolution right (../, protocol-relative //host/..., query
strings, fragments) is exactly what urllib.parse.urljoin/urlparse already do
correctly, and this is the one piece of deis crawl-site where a resolution
bug would mean silently wandering off the site the operator asked to crawl.

A link is kept only if, once resolved to an absolute URL, it is both:
    - same-origin as the crawl's root URL (scheme+host+port), and
    - under the root URL's own path (its directory subtree), not just its host.
Both restrictions exist for the same reason: a "Parent Directory" link (or a
stray absolute link elsewhere on the same host) must not let the crawl walk
into an unrelated part of the site the operator never asked about.

Classification of a kept link is by trailing slash - the near-universal
autoindex convention: a directory listing always links to a subdirectory
with a trailing "/", so anything without one is treated as a file to queue
for download instead of a page to recurse into.

A link is also dropped if, ignoring its query string and fragment, it
resolves to the same path as the page it was found on - Apache mod_autoindex
column-sort links (e.g. "?C=N;O=D") are exactly this shape and are not
distinct resources.
"""

from __future__ import annotations

import json
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urlunparse


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value)


def _origin(parsed) -> tuple[str, str, int | None]:
    return (parsed.scheme, parsed.hostname or "", parsed.port)


def _path_only(url: str) -> str:
    """The URL with its query string and fragment stripped, for comparing
    "same page, different sort order" links against the page's own URL.
    """
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def extract_links(html: str, page_url: str, root_url: str) -> dict[str, list[str]]:
    """Returns {"dirs": [...], "files": [...]}, both sorted and deduplicated,
    restricted to root_url's origin and path subtree.
    """
    parser = _LinkExtractor()
    parser.feed(html)

    root_parsed = urlparse(root_url)
    root_origin = _origin(root_parsed)
    root_path = root_parsed.path if root_parsed.path.endswith("/") else root_parsed.path + "/"
    own_path = _path_only(page_url)

    dirs: set[str] = set()
    files: set[str] = set()

    for href in parser.hrefs:
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)

        if _origin(parsed) != root_origin:
            continue
        if not parsed.path.startswith(root_path):
            continue
        if _path_only(absolute) == own_path:
            continue

        # Drop query string and fragment for the URL we'd actually queue -
        # a listing link has no meaningful query, and keeping one would
        # create spurious duplicate file entries for the same resource.
        clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))

        if parsed.path.endswith("/"):
            dirs.add(clean)
        else:
            files.add(clean)

    return {"dirs": sorted(dirs), "files": sorted(files)}


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: crawl_links.py <html-file> <page-url> <root-url>", file=sys.stderr)
        return 2

    html_path, page_url, root_url = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(html_path, encoding="utf-8", errors="replace") as f:
        html = f.read()

    print(json.dumps(extract_links(html, page_url, root_url)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
