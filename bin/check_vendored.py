#!/usr/bin/env python3
"""Checks (and can mechanically apply) updates for every version this
project pins by hand - most of them documented in a VENDORED.md (spaCy
and its two model wheels, msoffcrypto-tool, 7-Zip, v2ray-core, the
geonamescache snapshot behind bin/cities.tsv, and, check-only, the
vendored creatorrc.py/guard_country_resolver.py - see
creatorrc_upgrade), two pinned in notebook/Elastic.ipynb's own first
code cell (elasticsearch-py, wordcloud) with no separate VENDORED.md of
their own - a single self-explanatory pip install line needs no extra
provenance doc the way a hash-pinned binary does - and .env.default's
ELASTIC_VERSION (elastic-stack), which nothing else here or in
.github/dependabot.yml can reach (see elastic_stack_upgrade's own
comment for why). The notebook's pip cell also installs numpy/pandas/
ipywidgets unpinned (floating latest) - nothing to check there, since
there's no pinned version to compare against.

Deliberately NOT covered here: the ordinary uv-managed dependencies in
each component's pyproject.toml/uv.lock (`just update` already handles
those - `uv lock --upgrade` + re-exporting requirements.txt), and every
other Docker image reference - Dependabot's own "docker"/"docker-compose"
ecosystems handle those now (see .github/dependabot.yml): each
Dockerfile's own FROM line (alpine/python/debian/docker.elastic.co base
images) and docker-compose.yml's directly pre-built images (the notebook
service's reuteras/container-notebook, pinned by digest).

Two subcommands:
    list                read-only - current vs latest for every item
    upgrade <name>      mechanically fetches the latest artifact(s),
                        computes/verifies their sha256, and edits the
                        exact pinned line(s) in the real pin file (never
                        VENDORED.md's own free-form prose, which stays a
                        manual edit - see each upgrade function's return
                        value for exactly what to change by hand)

Deliberately stdlib-only (urllib, hashlib, re) - this is a small,
maintainer-facing tool, not worth a new dependency (see CLAUDE.md's
supply-chain-security stance) just to fetch a few JSON documents and
files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_AGENT = "DEIS-check-vendored/1.0 (+https://github.com/reuteras/DEIS)"

# Set by main() from --github-token (or $GITHUB_TOKEN/$GH_TOKEN, gh CLI's own
# convention, as a fallback) - never from a config file: this is a
# developer-only convenience for GitHub's 60 requests/hour unauthenticated
# rate limit (this script's four GitHub-based items alone are 5 API calls a
# run - en_core_web_sm/sv_core_news_sm each list up to 100 releases - so a
# handful of `list`/`upgrade` runs in one session exhausts it, confirmed
# live: every GitHub-based item started failing with "HTTP Error 403: rate
# limit exceeded" after repeated runs while building this script), not
# something deis.cfg (checked-in defaults, shared by every operator of a
# case) should carry a personal access token through.
GITHUB_TOKEN: str | None = None

BIN_PYPROJECT = REPO_ROOT / "bin" / "pyproject.toml"
BIN_VENDORED = REPO_ROOT / "bin" / "VENDORED.md"
UNPACK_DOCKERFILE = REPO_ROOT / "unpack" / "Dockerfile"
UNPACK_INSTALL_SH = REPO_ROOT / "unpack" / "install.sh"
UNPACK_VENDORED = REPO_ROOT / "unpack" / "VENDORED.md"
DOWNLOADER_INSTALL_SH = REPO_ROOT / "downloader" / "install-release.sh"
DOWNLOADER_VENDORED = REPO_ROOT / "downloader" / "VENDORED.md"
CREATORRC_VENDORED = REPO_ROOT / "downloader" / "creatorrc" / "VENDORED.md"
NOTEBOOK_IPYNB = REPO_ROOT / "notebook" / "Elastic.ipynb"
ENV_DEFAULT = REPO_ROOT / ".env.default"


# --------------------------------------------------------------------------
# Small stdlib HTTP helpers - every check/upgrade function below is built on
# just these three.


def _http_json(url: str) -> object:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    # Only ever sent to api.github.com - GITHUB_TOKEN is a GitHub credential,
    # and pypi.org (the only other _http_json caller) has no use for it and
    # no reason to see it.
    if GITHUB_TOKEN and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def _http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.read().decode("utf-8", errors="replace")


def _http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


# Every "check failed to reach the network" case a caller needs to treat as
# "report it, don't crash the whole list/upgrade run" - the same reasoning
# as bin/deis.py's own ES_REQUEST_ERRORS, just against arbitrary upstream
# hosts instead of just Elasticsearch/Kibana.
FETCH_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, KeyError, IndexError)


def pypi_latest_version(package: str) -> str:
    data = _http_json(f"https://pypi.org/pypi/{package}/json")
    return data["info"]["version"]  # type: ignore[index]


def github_latest_release_tag(owner_repo: str) -> str:
    data = _http_json(f"https://api.github.com/repos/{owner_repo}/releases/latest")
    return data["tag_name"]  # type: ignore[index]


def github_latest_matching_release_tag(owner_repo: str, prefix: str) -> str | None:
    """spacy-models has one release per model+version (e.g. "en_core_web_sm-3.8.0"),
    not one release per repo, so the plain "latest release" endpoint could just as
    easily answer with an unrelated model's release. Lists recent releases instead
    and picks the newest tag starting with `prefix`, comparing the numeric version
    suffix rather than string sort (so "3.10.0" sorts after "3.9.0").
    """
    data = _http_json(f"https://api.github.com/repos/{owner_repo}/releases?per_page=100")
    matching = [r["tag_name"] for r in data if r["tag_name"].startswith(prefix)]  # type: ignore[union-attr]
    if not matching:
        return None

    def version_key(tag: str) -> tuple[int, ...]:
        return tuple(int(part) for part in tag[len(prefix) :].split("."))

    return max(matching, key=version_key)


# --------------------------------------------------------------------------
# spaCy (bin/pyproject.toml, bin/VENDORED.md)


def spacy_current() -> str:
    match = re.search(r'"spacy==([\d.]+)"', BIN_PYPROJECT.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError("could not find a spacy== pin in bin/pyproject.toml")
    return match.group(1)


def spacy_latest() -> str:
    return pypi_latest_version("spacy")


def spacy_upgrade(new_version: str) -> list[str]:
    old = spacy_current()
    old_pin = f'"spacy=={old}"'
    text = BIN_PYPROJECT.read_text(encoding="utf-8")
    # Checked before replacing, not by comparing before/after text: a
    # same-version --force re-pin (old == new_version) would otherwise
    # leave the text byte-identical even on a real, successful
    # substitution, and be misread as "pin not found" - confirmed live,
    # this exact bug is why this comment (and the fix) exist.
    if old_pin not in text:
        raise RuntimeError("spacy== pin not found to replace in bin/pyproject.toml")
    new_text = text.replace(old_pin, f'"spacy=={new_version}"', 1)
    BIN_PYPROJECT.write_text(new_text, encoding="utf-8")
    return [
        f"bin/pyproject.toml: spacy=={old} -> {new_version}",
        (
            "Check the two model wheels (en_core_web_sm/sv_core_news_sm) still have a release "
            "compatible with this spacy version before running 'just update' - see "
            "https://github.com/explosion/spacy-models/releases"
        ),
        "Run 'just update' to refresh bin/uv.lock/bin/requirements.txt.",
        f"Update bin/VENDORED.md by hand: spaCy version {old} -> {new_version}, new 'Pinned on' date.",
    ]


def _model_current(model: str) -> str:
    pattern = rf"{re.escape(model)}-([\d.]+)/{re.escape(model)}-[\d.]+-py3-none-any\.whl"
    match = re.search(pattern, BIN_PYPROJECT.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError(f"could not find a {model} wheel URL in bin/pyproject.toml")
    return match.group(1)


def _model_latest(model: str) -> str | None:
    tag = github_latest_matching_release_tag("explosion/spacy-models", f"{model}-")
    return tag[len(model) + 1 :] if tag else None


def _model_upgrade(model: str, new_version: str) -> list[str]:
    old = _model_current(model)
    old_part = f"{model}-{old}/{model}-{old}-py3-none-any.whl"
    new_part = f"{model}-{new_version}/{model}-{new_version}-py3-none-any.whl"
    text = BIN_PYPROJECT.read_text(encoding="utf-8")
    if old_part not in text:
        raise RuntimeError(f"could not find the {model} wheel URL to replace in bin/pyproject.toml")
    BIN_PYPROJECT.write_text(text.replace(old_part, new_part, 1), encoding="utf-8")
    return [
        f"bin/pyproject.toml: {model} {old} -> {new_version}",
        (
            "Run 'just update' - this refreshes bin/uv.lock, which records the wheel's own sha256 "
            "from the actual download (the integrity guarantee VENDORED.md's spaCy entry relies on)."
        ),
        (
            f"Update bin/VENDORED.md by hand: {model} version {old} -> {new_version}, its sha256 from "
            "the refreshed bin/uv.lock, and a new 'Pinned on' date."
        ),
    ]


# --------------------------------------------------------------------------
# msoffcrypto-tool (unpack/Dockerfile, unpack/VENDORED.md)


def msoffcrypto_current() -> str:
    match = re.search(r'"msoffcrypto-tool==([\d.]+)"', UNPACK_DOCKERFILE.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError("could not find a msoffcrypto-tool== pin in unpack/Dockerfile")
    return match.group(1)


def msoffcrypto_latest() -> str:
    return pypi_latest_version("msoffcrypto-tool")


def msoffcrypto_upgrade(new_version: str) -> list[str]:
    old = msoffcrypto_current()
    old_pin = f'"msoffcrypto-tool=={old}"'
    text = UNPACK_DOCKERFILE.read_text(encoding="utf-8")
    # See spacy_upgrade's own comment: checked before replacing, not via a
    # before/after text comparison, or a same-version --force re-pin would
    # be misread as "pin not found" despite succeeding.
    if old_pin not in text:
        raise RuntimeError("msoffcrypto-tool== pin not found to replace in unpack/Dockerfile")
    new_text = text.replace(old_pin, f'"msoffcrypto-tool=={new_version}"', 1)
    UNPACK_DOCKERFILE.write_text(new_text, encoding="utf-8")
    return [
        f"unpack/Dockerfile: msoffcrypto-tool=={old} -> {new_version}",
        (
            "Read the changelog at https://github.com/nolze/msoffcrypto-tool/releases and check what "
            "its own dependencies (cryptography, olefile) pull in differently at the new version."
        ),
        f"Update unpack/VENDORED.md by hand: version {old} -> {new_version}, new 'Pinned on' date.",
    ]


# --------------------------------------------------------------------------
# 7-Zip (unpack/install.sh, unpack/VENDORED.md) - no API, scraped off the
# download page's own version-history table (newest listed first).


def _sevenzip_display(raw: str) -> str:
    """ "2603" -> "26.03" - VENDORED.md's own version format."""
    return f"{raw[:2]}.{raw[2:]}"


def sevenzip_current() -> str:
    match = re.search(r'VERSION="(\d+)"', UNPACK_INSTALL_SH.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError("could not find VERSION= in unpack/install.sh")
    return _sevenzip_display(match.group(1))


def sevenzip_latest() -> str | None:
    html = _http_text("https://www.7-zip.org/download.html")
    match = re.search(r"7z(\d{4})-linux-x64\.tar\.xz", html)
    return _sevenzip_display(match.group(1)) if match else None


def sevenzip_upgrade(new_version: str) -> list[str]:
    old = sevenzip_current()
    raw_old = old.replace(".", "")
    raw_new = new_version.replace(".", "")

    hashes = {}
    for arch in ("arm64", "x64"):
        archive = f"7z{raw_new}-linux-{arch}.tar.xz"
        hashes[arch] = hashlib.sha256(_http_bytes(f"https://www.7-zip.org/a/{archive}")).hexdigest()

    text = UNPACK_INSTALL_SH.read_text(encoding="utf-8")
    old_version_pin = f'VERSION="{raw_old}"'
    if old_version_pin not in text:
        raise RuntimeError("VERSION= pin not found to replace in unpack/install.sh")
    new_text = text.replace(old_version_pin, f'VERSION="{raw_new}"', 1)
    for arch in ("arm64", "x64"):
        new_text, count = re.subn(
            rf'(DOWNLOAD_ARCH="{arch}"\n\s*SHA256=")[0-9a-f]+(")',
            rf"\g<1>{hashes[arch]}\g<2>",
            new_text,
        )
        if count != 1:
            raise RuntimeError(f"could not replace the {arch} SHA256 line in unpack/install.sh")
    UNPACK_INSTALL_SH.write_text(new_text, encoding="utf-8")

    return [
        f"unpack/install.sh: 7-Zip {old} -> {new_version}",
        f"  sha256 7z{raw_new}-linux-arm64.tar.xz: {hashes['arm64']}",
        f"  sha256 7z{raw_new}-linux-x64.tar.xz: {hashes['x64']}",
        (
            "7-zip.org publishes no independent per-file checksum, so these hashes are only as "
            "trustworthy as this one download - cross-check independently before trusting them."
        ),
        "Update unpack/VENDORED.md by hand with the version/hashes above and a new 'Pinned on' date.",
    ]


# --------------------------------------------------------------------------
# v2ray-core (downloader/install-release.sh, downloader/VENDORED.md)


def v2ray_current() -> str:
    match = re.search(r"VERSION='([^']+)'", DOWNLOADER_INSTALL_SH.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError("could not find VERSION= in downloader/install-release.sh")
    return match.group(1)


def v2ray_latest() -> str:
    return github_latest_release_tag("v2fly/v2ray-core")


def v2ray_upgrade(new_version: str) -> list[str]:
    old = v2ray_current()
    machines = ("64", "arm64-v8a")
    hashes = {}
    for machine in machines:
        url = f"https://github.com/v2fly/v2ray-core/releases/download/{new_version}/v2ray-linux-{machine}.zip"
        hashes[machine] = hashlib.sha256(_http_bytes(url)).hexdigest()

    text = DOWNLOADER_INSTALL_SH.read_text(encoding="utf-8")
    old_version_pin = f"VERSION='{old}'"
    if old_version_pin not in text:
        raise RuntimeError("VERSION= pin not found to replace in downloader/install-release.sh")
    new_text = text.replace(old_version_pin, f"VERSION='{new_version}'", 1)
    for machine in machines:
        new_text, count = re.subn(
            rf"(\['{re.escape(machine)}'\]=')[0-9a-f]+(')",
            rf"\g<1>{hashes[machine]}\g<2>",
            new_text,
        )
        if count != 1:
            raise RuntimeError(f"could not replace the {machine!r} sha256 entry in downloader/install-release.sh")
    DOWNLOADER_INSTALL_SH.write_text(new_text, encoding="utf-8")

    return [
        f"downloader/install-release.sh: v2ray-core {old} -> {new_version}",
        f"  sha256 v2ray-linux-64.zip: {hashes['64']}",
        f"  sha256 v2ray-linux-arm64-v8a.zip: {hashes['arm64-v8a']}",
        (
            "Cross-check both against the release's own .dgst file's SHA2-256 line before trusting "
            "them (see downloader/VENDORED.md - a compromised release would control both the archive "
            "and its own .dgst, so this download alone isn't independent verification)."
        ),
        "Update downloader/VENDORED.md by hand with the version/hashes above and a new 'Pinned on' date.",
    ]


# --------------------------------------------------------------------------
# geonamescache snapshot behind bin/cities.tsv (bin/VENDORED.md) - not a
# version pin in a file anywhere; "current" is only recorded in
# VENDORED.md's own prose, and "upgrade" means regenerating the vendored
# data file, not editing a pin.


def geonamescache_current() -> str:
    match = re.search(r"`geonamescache` version used to generate: ([\d.]+)", BIN_VENDORED.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError("could not find the geonamescache version in bin/VENDORED.md")
    return match.group(1)


def geonamescache_latest() -> str:
    return pypi_latest_version("geonamescache")


def geonamescache_upgrade(new_version: str) -> list[str]:
    generator = (
        "import geonamescache\n"
        "gc = geonamescache.GeonamesCache()\n"
        "cities = [c for c in gc.get_cities().values() if c['population'] >= 100000]\n"
        "cities.sort(key=lambda c: c['name'])\n"
        "with open('bin/cities.tsv', 'w', encoding='utf-8') as f:\n"
        "    f.write('#name\\tlat\\tlon\\tcountry\\tpopulation\\n')\n"
        "    for c in cities:\n"
        "        name = c['name'].replace('\\t', ' ')\n"
        "        f.write(f\"{name}\\t{c['latitude']}\\t{c['longitude']}\\t{c['countrycode']}\\t{c['population']}\\n\")\n"
        "print(len(cities))\n"
    )
    result = subprocess.run(
        ["uv", "run", f"--with=geonamescache=={new_version}", "python3", "-c", generator],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    row_count = result.stdout.strip().splitlines()[-1]
    old = geonamescache_current()
    return [
        (
            f"bin/cities.tsv regenerated with geonamescache=={new_version} ({row_count} cities, "
            "population >= 100,000 - unchanged from the original cutoff)."
        ),
        f"Update bin/VENDORED.md by hand: geonamescache version {old} -> {new_version}, new 'Generated on' date.",
    ]


# --------------------------------------------------------------------------
# creatorrc (downloader/creatorrc/VENDORED.md) - check-only. Its own
# VENDORED.md is explicit that picking up an upstream change means
# diffing and reading the new file by hand before replacing anything, not
# an automated swap of someone else's code with no review - that policy
# is honored here by refusing to upgrade programmatically at all.


def creatorrc_current() -> str:
    match = re.search(
        r"Vendored from commit: `([0-9a-f]+)` \(creatorrc\.py\)", CREATORRC_VENDORED.read_text(encoding="utf-8")
    )
    if not match:
        raise RuntimeError("could not find creatorrc.py's vendored commit in VENDORED.md")
    return match.group(1)


def creatorrc_latest() -> str:
    data = _http_json("https://api.github.com/repos/hephaest0s/creatorrc/commits?path=creatorrc.py&per_page=1")
    return data[0]["sha"]  # type: ignore[index]


def creatorrc_upgrade(_new_version: str) -> list[str]:
    raise RuntimeError(
        "creatorrc is vendored third-party code, not a version-pinned release - its own "
        "VENDORED.md calls for diffing the new file against this one and reading it before "
        "replacing anything, not an automated swap. See downloader/creatorrc/VENDORED.md."
    )


# --------------------------------------------------------------------------
# elasticsearch-py / wordcloud (notebook/Elastic.ipynb's first code cell) -
# no separate VENDORED.md; the pin is the pip install line itself, and
# unlike a hash-pinned binary there's no separate provenance to record. A
# plain text substitution on the raw .ipynb (itself JSON) rather than a
# json.load/json.dump round-trip: nbformat has its own formatting
# conventions (key order, indent width) a naive re-dump wouldn't
# reproduce, which would turn a one-line version bump into a diff of the
# whole file. The pin appears exactly once, inside a JSON-escaped shell
# string ('\"elasticsearch==9.5.0\"'), confirmed against the real file
# before relying on it.


def _notebook_pip_current(package: str) -> str:
    pattern = rf'\\"{re.escape(package)}==([\d.]+)\\"'
    match = re.search(pattern, NOTEBOOK_IPYNB.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError(f"could not find a {package}== pin in notebook/Elastic.ipynb")
    return match.group(1)


def _notebook_pip_upgrade(package: str, new_version: str) -> list[str]:
    old = _notebook_pip_current(package)
    old_pin = f'\\"{package}=={old}\\"'
    text = NOTEBOOK_IPYNB.read_text(encoding="utf-8")
    if old_pin not in text:
        raise RuntimeError(f"{package}== pin not found to replace in notebook/Elastic.ipynb")
    new_text = text.replace(old_pin, f'\\"{package}=={new_version}\\"', 1)
    NOTEBOOK_IPYNB.write_text(new_text, encoding="utf-8")
    return [
        f"notebook/Elastic.ipynb: {package}=={old} -> {new_version}",
        (
            "This only edits the pip-install cell's source text - re-run that cell (or restart "
            "the kernel) in a running notebook to actually pick up the new version; nothing here "
            "touches the notebook container."
        ),
    ]


# --------------------------------------------------------------------------
# Elastic Stack (.env.default's ELASTIC_VERSION) - the build arg every
# docker.elastic.co-based Dockerfile (elasticsearch/, kibana/, setup/)
# takes its FROM tag from. Not something Dependabot's own "docker"
# ecosystem can resolve: the version lives in .env.default, not as a
# literal in any Dockerfile's own FROM line, so there's nothing there for
# it to version-bump (see .github/dependabot.yml's own comment on this).
# elastic/elasticsearch's GitHub releases happen to use the exact same
# version scheme as this project's ELASTIC_VERSION ("v9.5.3" -> "9.5.3"),
# confirmed live, so this reuses github_latest_release_tag() rather than
# dealing with docker.elastic.co's own registry auth flow for the same
# answer.


def elastic_stack_current() -> str:
    match = re.search(r"^ELASTIC_VERSION=([\d.]+)", ENV_DEFAULT.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise RuntimeError("could not find ELASTIC_VERSION= in .env.default")
    return match.group(1)


def elastic_stack_latest() -> str:
    return github_latest_release_tag("elastic/elasticsearch").lstrip("v")


def elastic_stack_upgrade(new_version: str) -> list[str]:
    old = elastic_stack_current()
    old_pin = f"ELASTIC_VERSION={old}"
    text = ENV_DEFAULT.read_text(encoding="utf-8")
    if old_pin not in text:
        raise RuntimeError("ELASTIC_VERSION= pin not found to replace in .env.default")
    new_text = text.replace(old_pin, f"ELASTIC_VERSION={new_version}", 1)
    ENV_DEFAULT.write_text(new_text, encoding="utf-8")
    return [
        f".env.default: ELASTIC_VERSION {old} -> {new_version}",
        (
            "Read the Elasticsearch/Kibana release notes for breaking changes before running "
            "this against real case data - a version bump can change index mapping defaults or "
            "deprecate settings setup/entrypoint.sh relies on."
        ),
        (
            "This only edits .env.default (the checked-in template) - .env itself is gitignored "
            "and 'deis init' leaves an existing one alone, so update the live .env by hand too if "
            "you want this to actually take effect."
        ),
    ]


# --------------------------------------------------------------------------


@dataclass
class VendoredItem:
    name: str
    vendored_md: Path
    current: Callable[[], str]
    latest: Callable[[], str | None]
    upgrade: Callable[[str], list[str]] | None


ITEMS: list[VendoredItem] = [
    VendoredItem("spacy", BIN_VENDORED, spacy_current, spacy_latest, spacy_upgrade),
    VendoredItem(
        "en_core_web_sm",
        BIN_VENDORED,
        lambda: _model_current("en_core_web_sm"),
        lambda: _model_latest("en_core_web_sm"),
        lambda v: _model_upgrade("en_core_web_sm", v),
    ),
    VendoredItem(
        "sv_core_news_sm",
        BIN_VENDORED,
        lambda: _model_current("sv_core_news_sm"),
        lambda: _model_latest("sv_core_news_sm"),
        lambda v: _model_upgrade("sv_core_news_sm", v),
    ),
    VendoredItem("geonamescache", BIN_VENDORED, geonamescache_current, geonamescache_latest, geonamescache_upgrade),
    VendoredItem("msoffcrypto-tool", UNPACK_VENDORED, msoffcrypto_current, msoffcrypto_latest, msoffcrypto_upgrade),
    VendoredItem("7-zip", UNPACK_VENDORED, sevenzip_current, sevenzip_latest, sevenzip_upgrade),
    VendoredItem("v2ray-core", DOWNLOADER_VENDORED, v2ray_current, v2ray_latest, v2ray_upgrade),
    VendoredItem("creatorrc", CREATORRC_VENDORED, creatorrc_current, creatorrc_latest, creatorrc_upgrade),
    VendoredItem(
        "elasticsearch-py",
        NOTEBOOK_IPYNB,
        lambda: _notebook_pip_current("elasticsearch"),
        lambda: pypi_latest_version("elasticsearch"),
        lambda v: _notebook_pip_upgrade("elasticsearch", v),
    ),
    VendoredItem(
        "wordcloud",
        NOTEBOOK_IPYNB,
        lambda: _notebook_pip_current("wordcloud"),
        lambda: pypi_latest_version("wordcloud"),
        lambda v: _notebook_pip_upgrade("wordcloud", v),
    ),
    VendoredItem("elastic-stack", ENV_DEFAULT, elastic_stack_current, elastic_stack_latest, elastic_stack_upgrade),
]


def cmd_list(_args: argparse.Namespace) -> int:
    rows: list[tuple[str, str, str, str]] = []
    any_problem = False
    for item in ITEMS:
        try:
            current = item.current()
        except Exception as error:  # noqa: BLE001 - one bad pin file must not stop the whole check
            rows.append((item.name, "?", "?", f"error reading current pin: {error}"))
            any_problem = True
            continue
        try:
            latest = item.latest()
        except FETCH_ERRORS as error:
            rows.append((item.name, current, "?", f"check failed: {error}"))
            any_problem = True
            continue
        if latest is None:
            rows.append((item.name, current, "?", "could not determine latest version"))
            any_problem = True
            continue
        status = "up to date" if latest == current else "update available"
        if status == "update available":
            any_problem = True
        rows.append((item.name, current, latest, status))

    name_w = max(len("NAME"), *(len(r[0]) for r in rows))
    current_w = max(len("CURRENT"), *(len(r[1]) for r in rows))
    latest_w = max(len("LATEST"), *(len(r[2]) for r in rows))
    header = f"{'NAME'.ljust(name_w)}  {'CURRENT'.ljust(current_w)}  {'LATEST'.ljust(latest_w)}  STATUS"
    print(header)
    print("-" * len(header))
    for name, current, latest, status in rows:
        print(f"{name.ljust(name_w)}  {current.ljust(current_w)}  {latest.ljust(latest_w)}  {status}")

    print()
    print("Not covered here: ordinary uv-managed dependencies (run 'just update' for those) and")
    print("Docker image references - Dependabot handles those now (see .github/dependabot.yml).")
    return 1 if any_problem else 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    item = next(i for i in ITEMS if i.name == args.name)  # argparse choices already validated this
    if item.upgrade is None:
        print(f"No automated upgrade for {item.name!r} - see {item.vendored_md.relative_to(REPO_ROOT)}.")
        return 1

    try:
        current = item.current()
    except Exception as error:  # noqa: BLE001
        print(f"Could not read the current pin for {item.name!r}: {error}", file=sys.stderr)
        return 1
    try:
        latest = item.latest()
    except FETCH_ERRORS as error:
        print(f"Could not check the latest version for {item.name!r}: {error}", file=sys.stderr)
        return 1
    if latest is None:
        print(f"Could not determine the latest version for {item.name!r}.", file=sys.stderr)
        return 1
    if latest == current and not args.force:
        print(f"{item.name} is already at {current} - nothing to do (pass --force to re-pin anyway).")
        return 0

    print(f"Upgrading {item.name}: {current} -> {latest}")
    try:
        notes = item.upgrade(latest)
    except Exception as error:  # noqa: BLE001 - report it cleanly, this is a CLI entry point
        print(f"Upgrade failed, nothing was changed for this item: {error}", file=sys.stderr)
        return 1

    print("Done. Remaining manual steps:")
    for note in notes:
        print(f"  - {note}")
    return 0


GITHUB_TOKEN_HELP = (
    "GitHub API token, to avoid the 60 requests/hour unauthenticated rate limit - this script's "
    "GitHub-based items alone are several calls a run, so a handful of runs in one session can "
    "exhaust it (confirmed live: every GitHub-based item started failing with 'HTTP Error 403: "
    "rate limit exceeded'). Falls back to $GITHUB_TOKEN or $GH_TOKEN if not given. Developer-only "
    "convenience, deliberately not a deis.cfg option - never stored anywhere. For a "
    '1Password-managed token: --github-token "$(op read op://<vault>/<item>/<field>)"'
)


def main() -> int:
    global GITHUB_TOKEN

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="check every vendored item's current vs latest available version")
    p_list.add_argument("--github-token", metavar="TOKEN", help=GITHUB_TOKEN_HELP)
    p_list.set_defaults(func=cmd_list)

    p_upgrade = sub.add_parser("upgrade", help="mechanically apply an available upgrade for one item")
    p_upgrade.add_argument("name", choices=[item.name for item in ITEMS], help="which item to upgrade")
    p_upgrade.add_argument("--force", action="store_true", help="re-pin even if already at the latest version")
    p_upgrade.add_argument("--github-token", metavar="TOKEN", help=GITHUB_TOKEN_HELP)
    p_upgrade.set_defaults(func=cmd_upgrade)

    args = parser.parse_args()
    GITHUB_TOKEN = args.github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
