#!/usr/bin/env python3
"""Single entry point for operating DEIS, for people who don't want to learn
docker compose profiles, Kibana's Dev Tools console, or which marker file
under status/ to check when something looks wrong. Wraps the existing
scripts and containers rather than replacing them - see
docs/IMPROVEMENTS.md's "CLI" section for the design.
"""

import argparse
import base64
import configparser
import csv
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.markup import escape as rich_escape
from rich.table import Table

# bin/ is only guaranteed to be on sys.path when this file is run directly as
# a script (python adds the script's own directory automatically); loaded
# dynamically the way tests/test_deis_cli.py does, it isn't - so this makes
# the sibling import work either way.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import geo
import pii
import simhash

# entities (item 32) and language are deliberately NOT imported up here
# alongside pii/simhash: unlike those, they pull in spaCy+two trained
# models (~1-2s to load, see bin/VENDORED.md) and py3langid (~0.4s to
# import) respectively - real load costs every other subcommand (status/
# search/report/...) has no reason to pay, or depend on being installed at
# all, just to run `deis --help`. Imported lazily inside cmd_entity_scan()
# and cmd_language_scan() instead.

REPO_ROOT = Path(__file__).resolve().parent.parent
ES_URL = "http://127.0.0.1:9200"
KIBANA_URL = "http://127.0.0.1:5601"
INDEX = "leakdata-index-000001"
RUNS_INDEX = "deis-ingest-runs"
# docker-compose.yml names this volume implicitly (project name "deis",
# derived from this directory's name, prefixed onto the "elasticsearch"
# service volume - same convention Justfile's docker-clean already
# hardcodes for `docker volume rm`) - not discovered dynamically, since
# nothing here ever runs from a differently-named checkout.
ES_VOLUME_NAME = "deis_elasticsearch"
# A pinned, specific Alpine release (not `latest`) purely to tar/untar a
# Docker volume from the host side in `deis archive`/`deis restore` (see
# _run_in_es_volume below) - Docker volumes aren't a host filesystem path
# on macOS/Windows (Docker Desktop's VM owns them), so this is the
# standard, portable way to reach into one regardless of platform. Alpine
# specifically: tiny (~3MB), official, and already used purely as
# ephemeral `--rm` tooling, never as a long-running service - the
# supply-chain exposure is a single `tar` invocation, not persistent code.
VOLUME_BACKUP_IMAGE = "alpine:3.22.5"
VIEW_URL = "http://127.0.0.1:8081/view"
ALLOWED_URL_SCHEMES = ("http://", "https://", "ftp://")
# Shared between cmd_pii_scan (what it writes) and cmd_pii_report (what it
# reads back) - see pii.detect_all()'s own result shape in bin/pii.py.
PII_FIELDS = ("personnummer", "emails", "phone_numbers", "ibans", "card_numbers")
# Same idea, for entities - see entities.detect_entities()'s result shape
# in bin/entities.py.
ENTITY_FIELDS = ("persons", "organizations", "locations")
# setup/export.ndjson's "Photo GPS locations" map (deis geo-scan's own
# dashboard) - a known, exactly-one saved object id, hardcoded the same
# way INDEX/RUNS_INDEX are rather than looked up by title. See
# _sync_geo_map_basemap()/maps_use_elastic_map_data().
GEO_MAP_ID = "41392fa6-a00a-4086-9e11-302d9a549998"
GEO_MAP_BASEMAP_LAYER = {
    "id": "9718a10c-b838-4973-82c0-db161533c93d",
    "label": "Basemap (Elastic Maps Service)",
    "minZoom": 0,
    "maxZoom": 24,
    "alpha": 1,
    "sourceDescriptor": {
        "type": "EMS_TMS",
        "isAutoSelect": True,
        "id": "9718a10c-b838-4973-82c0-db161533c93d-src",
    },
    "visible": True,
    "type": "EMS_VECTOR_TILE",
}

# Single source of truth for both build_parser() and the completion scripts
# below, so the two can't silently drift apart.
SUBCOMMANDS = (
    "init",
    "doctor",
    "build",
    "setup",
    "run",
    "status",
    "search",
    "report",
    "add-urls",
    "add-files",
    "pii-scan",
    "pii-report",
    "language-scan",
    "entity-scan",
    "entity-report",
    "dedupe-scan",
    "dedupe-report",
    "geo-scan",
    "geo-report",
    "archive",
    "restore",
    "clean",
    "reset",
    "completion",
)
RUN_ONLY_CHOICES = ("setup", "download", "extract", "ingest")

console = Console()


def read_env(path: Path | None = None) -> dict[str, str]:
    """Parses .env's KEY=VALUE lines - the only place this project reads
    that file today is docker compose itself, so there's nothing existing
    to reuse; skips blank lines and comments the same way deis.cfg's own
    reader does.

    `path` defaults to REPO_ROOT / ".env", resolved fresh on every call
    (not a plain default argument, `path: Path = REPO_ROOT / ".env"` -
    that expression is evaluated exactly once, at function-definition
    time, so it would silently keep pointing at wherever REPO_ROOT was
    when this module was first imported even after test code
    monkeypatches REPO_ROOT to a tmp_path later - confirmed live: this
    is exactly what broke _build_manifest's own test in CI, where no
    real .env exists to coincidentally mask it the way a developer's
    real one can locally).
    """
    if path is None:
        path = REPO_ROOT / ".env"
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def elastic_password() -> str | None:
    import os

    if password := os.environ.get("ELASTIC_PASSWORD"):
        return password
    return read_env().get("ELASTIC_PASSWORD")


def entities_max_chars(path: Path | None = None) -> int:
    """Reads [entities] max_chars from deis.cfg - the character cap applied
    to attachment.content before it's handed to spaCy in cmd_entity_scan
    (see bin/entities.py's module docstring for why this cost is real: a
    194,000-character document from this corpus measured ~7.5s for
    tok2vec+ner alone, and excluding the rest of spaCy's pipeline barely
    moved that number). 0 means uncapped - scan each document's full
    indexed text, accepting the cost. fallback=20000, not a hard
    requirement that the key exist: deis.cfg is gitignored and 'deis init'
    leaves an existing one alone, so an install predating this option
    still gets a sane capped default rather than an error or an
    unintentionally uncapped scan.

    `path` defaults to REPO_ROOT / "deis.cfg", resolved fresh on every
    call rather than a plain default argument - see read_env's own
    docstring for why that matters (the same class of bug, fixed there
    after it broke a test in CI).
    """
    if path is None:
        path = REPO_ROOT / "deis.cfg"
    config = configparser.RawConfigParser()
    config.read(path)
    return config.getint("entities", "max_chars", fallback=20000)


def maps_use_elastic_map_data(path: Path | None = None) -> bool:
    """Reads [maps] use_elastic_map_data from deis.cfg - whether
    cmd_run's setup step is allowed to add Elastic Maps Service's
    basemap tile layer to the "Photo locations" dashboard's map (see
    GEO_MAP_ID/_sync_geo_map_basemap). Defaults to False, not True: EMS
    is Elastic's own cloud service, so unlike entities_max_chars's
    "missing key = safe default that keeps existing behavior" reasoning,
    an absent key here means "not yet explicitly allowed" - fully
    functional without it, so there is no default-on behavior to
    preserve for an install predating this option.

    `path` defaults to REPO_ROOT / "deis.cfg", resolved fresh on every
    call - see entities_max_chars's own docstring for why.
    """
    if path is None:
        path = REPO_ROOT / "deis.cfg"
    config = configparser.RawConfigParser()
    config.read(path)
    return config.getboolean("maps", "use_elastic_map_data", fallback=False)


# The one set of exceptions every es_request()/es_bulk() caller (and every
# raw urlopen() call against Elasticsearch/Kibana) catches as "expected,
# report it and move on" rather than letting it crash: RuntimeError (this
# module's own "password isn't configured" checks), URLError (connection
# refused/DNS failure - but only while *sending* a request; see below),
# and TimeoutError. ConnectionError was missing here for a long time -
# confirmed live: `deis restore` crashed with a raw, uncaught
# http.client.RemoteDisconnected traceback while polling Elasticsearch
# right after `docker compose up -d elasticsearch`, because urllib only
# wraps OSError as URLError when raised while sending a request
# (AbstractHTTPHandler.do_open's own try/except), not when raised later
# while reading the response - which is exactly when a server whose port
# is open but whose HTTP layer isn't serving yet (a container mid-startup)
# drops the connection. RemoteDisconnected is a ConnectionResetError,
# which is a ConnectionError, so this closes that gap everywhere at once
# instead of only at the one call site that happened to crash first.
ES_REQUEST_ERRORS = (RuntimeError, urllib.error.URLError, TimeoutError, ConnectionError)


def es_request(path: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
    """Minimal GET/POST-with-basic-auth helper. Deliberately stdlib
    (urllib), not requests: this CLI always runs on the host, never inside
    a container, so there's no docker-vs-host URL detection to share with
    ingest.py, and a second HTTP client dependency isn't worth adding to
    bin/'s otherwise dependency-light footprint just for this.
    """
    password = elastic_password()
    if not password:
        raise RuntimeError("ELASTIC_PASSWORD is not set (check .env, or run 'deis init').")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(ES_URL + path, data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"elastic:{password}".encode()).decode("ascii"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def kibana_request(path: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
    """Same shape as es_request(), against KIBANA_URL instead - used only
    by _sync_geo_map_basemap() today. Adds the kbn-xsrf header Kibana's
    write endpoints require on every request that isn't a real browser
    navigation (a CSRF guard, not related to the elastic user's own
    auth) - a write without it is rejected regardless of credentials.
    """
    password = elastic_password()
    if not password:
        raise RuntimeError("ELASTIC_PASSWORD is not set (check .env, or run 'deis init').")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(KIBANA_URL + path, data=data, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"elastic:{password}".encode()).decode("ascii"))
    req.add_header("kbn-xsrf", "true")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def es_bulk(actions: list[dict]) -> dict:
    """POSTs a _bulk request. Separate from es_request(): _bulk needs
    newline-delimited JSON (one compact line per action/doc, no wrapping
    array or indentation) and a different content-type, not the single
    pretty-printable JSON body es_request() sends.
    """
    body = ("\n".join(json.dumps(line, separators=(",", ":")) for line in actions) + "\n").encode("utf-8")
    password = elastic_password()
    if not password:
        raise RuntimeError("ELASTIC_PASSWORD is not set (check .env, or run 'deis init').")
    req = urllib.request.Request(ES_URL + "/_bulk", data=body, method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"elastic:{password}".encode()).decode("ascii"))
    req.add_header("Content-Type", "application/x-ndjson")
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def bulk_failures(response: dict) -> list[str]:
    """Per-item failure reasons from a _bulk response, or [] if every item
    succeeded. _bulk answers 200 OK even when every single update in it
    failed (a mapping conflict, a version conflict, a missing document) -
    the only signal is the top-level "errors" flag and the per-item "error"
    objects. Both enrichment passes below check this: a scan that silently
    wrote nothing while printing a full results table is the wrong failure
    mode for a tool whose output gets treated as evidence.
    """
    if not response.get("errors"):
        return []
    reasons = []
    for item in response.get("items", []):
        for result in item.values():
            if error := result.get("error"):
                reasons.append(f"{result.get('_id', '?')}: {error.get('reason', error)}")
    return reasons


def is_valid_url(url: str) -> bool:
    """Same scheme allowlist deis/urls.sh already enforces before queueing
    a URL to aria2 - checked again here so a bad URL is rejected
    immediately with a reason, instead of only surfacing later in
    logs/download_errors.log once urls.sh gets to it.
    """
    return url.startswith(ALLOWED_URL_SCHEMES)


def marker_status() -> dict[str, str]:
    """One-shot equivalent of bin/progress.py's per-stage checks, without
    its live-updating loop (that's still `just progress`'s job) - this
    exists so `deis status` can add the funnel counts progress.py doesn't
    have, as a single snapshot rather than a second infinite loop.
    """
    status_dir = REPO_ROOT / "status"
    status = {}

    if (status_dir / "download_failed").exists():
        status["download"] = "failed"
    elif (status_dir / "downloaded").exists():
        status["download"] = "done"
    elif (status_dir / "running").exists():
        status["download"] = "running"
    else:
        status["download"] = "not running"

    # "extracting"/"ingesting" are real liveness signals, touched by
    # unpack/start.sh and ingest/start.sh right before they start the
    # actual work - same pattern as download's status/running. "unpack" and
    # "extract_done" alone only mean "ready for this stage", not "the
    # container for it is currently up and working", so without those
    # liveness markers this would have to guess "pending" instead of
    # knowing "running".
    if (status_dir / "extract_done").exists():
        status["extract"] = "done"
    elif (status_dir / "extracting").exists():
        status["extract"] = "running"
    elif (status_dir / "unpack").exists():
        status["extract"] = "pending"
    else:
        status["extract"] = "waiting"

    if (status_dir / "ingest_done").exists():
        status["ingest"] = "done"
    elif (status_dir / "ingesting").exists():
        status["ingest"] = "running"
    elif (status_dir / "extract_done").exists():
        status["ingest"] = "pending"
    else:
        status["ingest"] = "waiting"

    return status


def count_files(directory: Path, exclude: set[str]) -> int:
    if not directory.is_dir():
        return 0
    return sum(1 for f in directory.rglob("*") if f.is_file() and f.name not in exclude)


def latest_run_summary() -> dict | None:
    try:
        response = es_request(f"/{RUNS_INDEX}/_search?size=1&sort=@timestamp:desc")
    except ES_REQUEST_ERRORS:
        return None
    hits = response.get("hits", {}).get("hits", [])
    return hits[0]["_source"] if hits else None


def cmd_init(_args) -> int:
    env_default = REPO_ROOT / ".env.default"
    env_path = REPO_ROOT / ".env"
    cfg_default = REPO_ROOT / "deis.cfg.default"
    cfg_path = REPO_ROOT / "deis.cfg"

    if env_path.exists():
        console.print("[yellow].env already exists, leaving it alone.[/yellow]")
    else:
        content = env_default.read_text(encoding="utf-8")
        content = re.sub(r"=changeme$", lambda _: f"={secrets.token_hex(32)}", content, flags=re.MULTILINE)
        env_path.write_text(content, encoding="utf-8")
        # Written with the default umask (0644 on a stock macOS/Linux
        # account) otherwise - this file holds the Elasticsearch, Kibana and
        # Jupyter secrets this command just generated, so it should not be
        # readable by every other account on the machine.
        env_path.chmod(0o600)
        console.print("[green]Created .env with generated passwords (mode 0600).[/green]")

    if cfg_path.exists():
        console.print("[yellow]deis.cfg already exists, leaving it alone.[/yellow]")
    else:
        shutil.copyfile(cfg_default, cfg_path)
        console.print("[green]Created deis.cfg from deis.cfg.default.[/green]")

    try:
        info = json.loads(subprocess.check_output(["docker", "info", "--format", "{{json .}}"]))
        mem_gb = info.get("MemTotal", 0) / (1024**3)
        if mem_gb < 18:
            console.print(
                f"[yellow]Docker has {mem_gb:.1f} GB available; the default setup wants 18 GB. "
                "Lower ES_JAVA_OPTS in .env, or increase Docker's memory limit.[/yellow]"
            )
        else:
            console.print(f"[green]Docker memory: {mem_gb:.1f} GB - OK.[/green]")
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError, KeyError):
        console.print("[red]Could not check Docker's memory allocation - is Docker running?[/red]")

    return 0


def cmd_doctor(_args) -> int:
    ok = True

    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
        console.print("[green]Docker: reachable.[/green]")
    except (subprocess.CalledProcessError, FileNotFoundError):
        console.print("[red]Docker: not reachable. Is it running?[/red]")
        ok = False

    try:
        health = es_request("/_cluster/health")
        color = {"green": "green", "yellow": "yellow", "red": "red"}.get(health.get("status"), "red")
        console.print(f"[{color}]Elasticsearch: {health.get('status')}.[/{color}]")
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Elasticsearch: not reachable ({error}).[/red]")
        ok = False

    try:
        urllib.request.urlopen(KIBANA_URL, timeout=10)
        console.print("[green]Kibana: reachable.[/green]")
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Kibana: not reachable ({error}).[/red]")
        ok = False

    try:
        ps_output = subprocess.check_output(
            ["docker", "compose", "ps", "--format", "json", "--all"], cwd=REPO_ROOT, text=True
        )
        for line in ps_output.splitlines():
            container = json.loads(line)
            state = container.get("State", "")
            # setup/unpack/ingest are one-shot containers that exit 0 when
            # their work is done - only a non-zero exit or an active
            # restart loop indicates a real problem.
            concerning = state == "restarting" or (state == "exited" and container.get("ExitCode", 0) != 0)
            if concerning:
                console.print(f"[red]{container.get('Name')}: {state} - {container.get('Status')}[/red]")
                ok = False
    except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
        console.print("[yellow]Could not read container status via docker compose ps.[/yellow]")

    if not check_tor_egress():
        ok = False

    return 0 if ok else 1


def check_tor_egress() -> bool:
    """Runs the same preflight deis/urls.sh runs before queueing a batch
    (item 42), rather than a second implementation of it here. It has to
    execute inside the deis container: v2ray is bound to 127.0.0.1 *inside*
    the downloader container, so the proxy chain the check exercises is not
    reachable from the host at all. Returns True when nothing is wrong -
    including when the check could not be run, which is reported but is not
    itself a finding.
    """
    try:
        running = subprocess.check_output(
            ["docker", "compose", "ps", "--status", "running", "--format", "{{.Service}}"],
            cwd=REPO_ROOT,
            text=True,
        ).split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        console.print("[yellow]TOR egress: could not ask docker which containers are running.[/yellow]")
        return True

    if "deis" not in running or "downloader" not in running:
        console.print(
            "[yellow]TOR egress: not checked - the deis and downloader containers are not running. "
            "Start them ('deis run --only download') and re-run doctor; the same check also runs "
            "automatically before any URL is queued.[/yellow]"
        )
        return True

    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "deis", "/deis/bin/torcheck.sh"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (result.stdout + result.stderr).splitlines():
        if line.strip():
            console.print(f"  {line.strip()}")
    if result.returncode != 0:
        console.print("[red]TOR egress: FAILED - downloads meant for TOR would not go through TOR.[/red]")
        return False
    console.print("[green]TOR egress: OK.[/green]")
    return True


def cmd_build(_args) -> int:
    # A service with no 'profiles:' key (elasticsearch, kibana, web, notebook,
    # gotenberg) always builds regardless of --profile. 'deis' and 'setup'
    # are the only two profile names needed to reach every profile-gated
    # service too: downloader/controller/unpack/ingest are each tagged with
    # 'deis' in addition to their own stage-specific profile, so 'deis'
    # already covers all four - only setup's own profile is missing from it.
    command = ["docker", "compose", "--profile", "deis", "--profile", "setup", "build"]
    console.print(f"Running: {' '.join(command)}")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def _sync_geo_map_basemap() -> None:
    """Adds or removes GEO_MAP_BASEMAP_LAYER on the "Photo GPS locations"
    map to match deis.cfg's [maps] use_elastic_map_data (see
    maps_use_elastic_map_data). Idempotent either way - re-running `deis
    run --only setup` after flipping the setting converges the map to
    match, rather than only ever adding the layer once. Never fatal to
    setup as a whole: a Kibana hiccup here just means the map keeps
    whatever basemap state it already had, not a failed setup run over a
    non-essential dashboard layer.
    """
    try:
        current = kibana_request(f"/api/saved_objects/map/{GEO_MAP_ID}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            console.print(
                "[yellow]'Photo GPS locations' map not found in Kibana - skipping basemap setup "
                "(setup/export.ndjson's own import should have created it).[/yellow]"
            )
        else:
            console.print(f"[yellow]Could not read the map from Kibana to set its basemap: {error}[/yellow]")
        return
    except ES_REQUEST_ERRORS as error:
        console.print(f"[yellow]Could not reach Kibana to set the map's basemap: {error}[/yellow]")
        return

    layers = json.loads(current["attributes"]["layerListJSON"])
    has_basemap = any(layer.get("type") == "EMS_VECTOR_TILE" for layer in layers)
    wants_basemap = maps_use_elastic_map_data()
    if wants_basemap == has_basemap:
        return

    if wants_basemap:
        layers.insert(0, GEO_MAP_BASEMAP_LAYER)
    else:
        layers = [layer for layer in layers if layer.get("type") != "EMS_VECTOR_TILE"]
    current["attributes"]["layerListJSON"] = json.dumps(layers)

    try:
        kibana_request(
            f"/api/saved_objects/map/{GEO_MAP_ID}",
            method="PUT",
            body={"attributes": current["attributes"], "references": current["references"]},
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[yellow]Could not update the map's basemap layer: {error}[/yellow]")
        return

    if wants_basemap:
        console.print("[green]Enabled Elastic Maps Service's basemap on the 'Photo locations' map.[/green]")
    else:
        console.print(
            "[green]Disabled Elastic Maps Service's basemap on the 'Photo locations' map "
            "(deis.cfg's [maps] use_elastic_map_data is false).[/green]"
        )


def cmd_run(args) -> int:
    profile = {"setup": "setup", "download": "download", "extract": "unpack", "ingest": "ingest"}.get(args.only, "deis")
    command = ["docker", "compose", "--profile", profile, "up", "-d"]
    console.print(f"Running: {' '.join(command)}")
    result = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if result.returncode != 0 or profile != "setup":
        return result.returncode

    # The setup container is one-shot: it exits once Kibana/ES are
    # configured. "docker compose logs -f" streams its output and returns on
    # its own when the container exits, so this blocks until setup is done
    # instead of leaving the caller to poll or tail logs separately.
    console.print("Following setup container logs until it exits...")
    log_result = subprocess.run(["docker", "compose", "logs", "setup", "-f"], cwd=REPO_ROOT, check=False)
    if log_result.returncode == 0:
        # setup/export.ndjson's own "Photo GPS locations" map never
        # includes the EMS basemap layer itself (see that file) - applied
        # here instead, from the host, since deis.cfg isn't read inside
        # the setup container and this is the one place that already
        # knows setup just finished importing it.
        _sync_geo_map_basemap()
    return log_result.returncode


def cmd_status(_args) -> int:
    status = marker_status()
    table = Table(title="DEIS status")
    table.add_column("Stage")
    table.add_column("State")
    for stage in ("download", "extract", "ingest"):
        table.add_row(stage, status[stage])
    console.print(table)

    # files/ holds a tracked .gitignore (see unpack/start.sh's own
    # '! -name .*' exclusion of it) that isn't leak data.
    downloaded = count_files(REPO_ROOT / "files", exclude={".gitignore"})
    extracted = count_files(REPO_ROOT / "extracted" / "files", exclude=set())
    unique = count_files(REPO_ROOT / "extracted" / "sha256", exclude=set())

    counts = Table(title="Funnel")
    counts.add_column("What")
    counts.add_column("Count")
    counts.add_row("files in files/", str(downloaded))
    counts.add_row("files under extracted/files", str(extracted))
    counts.add_row("unique sha256", str(unique))

    try:
        doc_count = es_request(f"/{INDEX}/_count")["count"]
        counts.add_row("documents in Elasticsearch", str(doc_count))
    except (*ES_REQUEST_ERRORS, KeyError):
        counts.add_row("documents in Elasticsearch", "could not be read")

    if run := latest_run_summary():
        counts.add_row("failed in last ingest run", str(run.get("failed", "?")))

    console.print(counts)
    return 0


def _highlight_fragment(hit: dict) -> str:
    """The one highlighted fragment of attachment.content Elasticsearch
    found for this hit (see cmd_search's own "highlight" request) -
    always present when a hit came from a match against attachment.content
    (the only field cmd_search ever queries), so the fallback is just
    defensive, not an expected case.
    """
    return (hit.get("highlight", {}).get("attachment.content") or [""])[0]


def _rich_snippet(hit: dict) -> str:
    """Elasticsearch's own <em>/</em> highlight markers, turned into
    rich's console markup for a bolded terminal snippet. rich_escape
    first - a literal "[" in real document content would otherwise be
    misread as the start of one of rich's own style tags.
    """
    return rich_escape(_highlight_fragment(hit)).replace("<em>", "[bold yellow]").replace("</em>", "[/bold yellow]")


def _plain_snippet(hit: dict) -> str:
    """Same fragment as _rich_snippet, with the <em>/</em> markers
    stripped rather than converted - for CSV export, where HTML-ish
    markup isn't meaningful to whatever opens the file.
    """
    return _highlight_fragment(hit).replace("<em>", "").replace("</em>", "")


def _rich_link(url: str) -> str:
    """rich console markup for a clickable OSC-8 hyperlink whose visible
    text and target are the same URL. A plain URL string in a Table cell
    is only ever clickable by luck - a terminal auto-detecting it as a
    link - and rich's default column overflow ("ellipsis") actively
    breaks even that by cutting the visible text (and the URL it names)
    down to fit the column, e.g. "http://127.0.0.1:8081/view/1836e9…".
    Wrapping it in [link=...] instead makes the full target a style
    attribute independent of how much of the visible text a narrow
    column ends up showing - it stays clickable (in terminals that
    support OSC-8: iTerm2, kitty, WezTerm, Windows Terminal, VS Code's
    integrated terminal, ...) even if the cell itself has to fold or
    truncate for display.
    """
    return f"[link={url}]{url}[/link]"


def cmd_search_sha256(args) -> int:
    """Looks up document(s) directly by sha256, via _mget - every document
    is indexed with _id == its sha256 (see ingest.py's build_bulk_body), so
    this is an exact ID lookup, not a query, and finds a file regardless of
    whether Tika ever extracted any searchable text from it. Built for the
    status/*.txt files (still_encrypted.txt, still_multivolume.txt, etc.),
    which are exactly one sha256 per line - the plain use case is "what is
    this hash, and where did it come from" for a hash already in hand,
    rather than cmd_search's "find documents matching this term". Excludes
    the "data" source field (the document's own base64-encoded original
    bytes) since it's never wanted here and can be large enough to bloat a
    multi-hash lookup for nothing.

    -f/--file reads one sha256 per line from a status file, e.g. the
    exact shape status/still_multivolume.txt or status/decrypted.txt
    already are - the two flags combine (both contribute to the same
    lookup) rather than one overriding the other. dict.fromkeys()
    dedupes while keeping first-seen order, in case the same hash shows
    up in both --sha256 and the file.
    """
    shas = list(args.sha256 or [])
    if args.file:
        try:
            lines = args.file.read_text(encoding="utf-8").splitlines()
        except OSError as error:
            console.print(f"[red]Could not read {args.file}: {error}[/red]")
            return 1
        shas.extend(line.strip() for line in lines if line.strip())
    shas = list(dict.fromkeys(shas))
    if not shas:
        console.print("[red]No sha256 hashes given - use --sha256 <hash> and/or -f/--file <path>.[/red]")
        return 1

    try:
        response = es_request(f"/{INDEX}/_mget?_source_excludes=data", method="POST", body={"ids": shas})
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Lookup failed: {error}[/red]")
        return 1

    docs = response.get("docs", [])
    found = [doc for doc in docs if doc.get("found")]
    missing = [doc["_id"] for doc in docs if not doc.get("found")]

    rows = [
        (
            doc["_id"],
            doc.get("_source", {}).get("filename", ""),
            doc.get("_source", {}).get("extraction_status", ""),
            # Only present on a "decrypted" document (an individually
            # password-protected Office/PDF file unpack.sh's password
            # cracking recovered) - see ingest.py's
            # load_decrypted_passwords/build_bulk_body. Empty string for
            # everything else, same as the other columns above.
            doc.get("_source", {}).get("decrypt_password", ""),
            f"{VIEW_URL}/{doc['_id']}",
        )
        for doc in found
    ]

    if args.output:
        try:
            with args.output.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["sha256", "filename", "extraction_status", "decrypt_password", "link"])
                writer.writerows(rows)
        except OSError as error:
            console.print(f"[red]Could not write {args.output}: {error}[/red]")
            return 1
        console.print(f"Wrote {len(rows)} of {len(shas)} sha256(s) to {args.output}.")
    else:
        console.print(f"{len(rows)} of {len(shas)} sha256(s) found in the index.")
        table = Table()
        table.add_column("SHA256")
        table.add_column("Filename", overflow="ellipsis", max_width=60)
        table.add_column("Status")
        table.add_column("Password")
        # overflow="fold" (wrap instead of cut) plus _rich_link's OSC-8
        # markup: even if the column still has to wrap the URL across
        # lines, the link stays clickable end-to-end (see _rich_link) -
        # unlike the default "ellipsis" overflow, which used to both
        # visibly cut the URL short and, in terminals without OSC-8
        # support, leave a chopped-off address nothing could open.
        table.add_column("Link", overflow="fold")
        for sha, filename, status, password, link in rows:
            # Full hash only in the CSV/lookup args - once it's the row
            # key for a hash the operator already typed in, showing all
            # 64 hex chars here just steals column width from Link for
            # no reader benefit.
            table.add_row(f"{sha[:12]}…", filename, status, password, _rich_link(link))
        console.print(table)

    if missing:
        console.print(
            f"[yellow]Not indexed yet (not ingested, or its content was fully absorbed into a "
            f"resolved multi-volume archive): {', '.join(missing)}[/yellow]"
        )
    return 0


def cmd_search(args) -> int:
    """Highlighted snippets (item 36) via Elasticsearch's own "highlight"
    API against attachment.content - the only field this ever queries, so
    there's no ambiguity about which field to highlight. --output scrolls
    every match into a CSV instead of the capped interactive table
    (size 20, unscrolled) - same shape as pii-report/entity-report.
    Dispatches to cmd_search_sha256 instead when --sha256 and/or -f/--file
    is given: that's an exact ID lookup, not a term search. "term" is
    optional in argparse to allow that sha256-only invocation, so it's
    checked for here instead. `args.sha256 is not None` (rather than a
    plain truthiness check) matters because --sha256 alone (no hashes,
    paired with -f/--file for all of them - see cmd_search_sha256) parses
    to [], which is falsy but still means "sha256 lookup mode".
    """
    if args.sha256 is not None or args.file is not None:
        return cmd_search_sha256(args)
    if not args.term:
        console.print("[red]Provide a search term, or use --sha256 <hash> [<hash> ...] and/or -f/--file.[/red]")
        return 1

    query = {
        "query": {"match": {"attachment.content": args.term}},
        "highlight": {"fields": {"attachment.content": {"fragment_size": 150, "number_of_fragments": 1}}},
    }
    try:
        if args.output:
            response = es_request(f"/{INDEX}/_search?scroll=1m", method="POST", body={"size": 200, **query})
        else:
            response = es_request(f"/{INDEX}/_search", method="POST", body={"size": 20, **query})
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    total = response.get("hits", {}).get("total", {}).get("value", 0)

    if args.output:
        scroll_id = response.get("_scroll_id")
        written = 0
        try:
            with args.output.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["filename", "sha256", "snippet", "link"])
                while True:
                    hits = response.get("hits", {}).get("hits", [])
                    if not hits:
                        break
                    for hit in hits:
                        source = hit["_source"]
                        writer.writerow(
                            [
                                source.get("filename", ""),
                                source.get("sha256", ""),
                                _plain_snippet(hit),
                                f"{VIEW_URL}/{source.get('sha256', '')}",
                            ]
                        )
                        written += 1
                    response = es_request(
                        "/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id}
                    )
                    scroll_id = response.get("_scroll_id")
        except OSError as error:
            console.print(f"[red]Could not write {args.output}: {error}[/red]")
            return 1
        except ES_REQUEST_ERRORS as error:
            console.print(f"[red]Search failed: {error}[/red]")
            return 1
        finally:
            if scroll_id:
                try:
                    es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
                except ES_REQUEST_ERRORS:
                    pass
        console.print(f"Wrote {written} hit(s) for {args.term!r} to {args.output}.")
        return 0

    hits = response.get("hits", {}).get("hits", [])
    console.print(f"{total} hit(s) for {args.term!r}.")
    table = Table()
    table.add_column("Filename", overflow="ellipsis", max_width=60)
    table.add_column("Snippet")
    table.add_column("Link", overflow="fold")
    for hit in hits:
        source = hit["_source"]
        table.add_row(
            source.get("filename", ""), _rich_snippet(hit), _rich_link(f"{VIEW_URL}/{source.get('sha256', '')}")
        )
    console.print(table)
    return 0


def cmd_report(_args) -> int:
    console.print("DEIS report")
    console.print("-----------")

    if run := latest_run_summary():
        for key in (
            "files_looked_at",
            "unique_files",
            "duplicate_copies",
            "indexed_this_run",
            "already_indexed",
            "failed",
            "elasticsearch_document_count",
        ):
            console.print(f"  {key}: {run.get(key, '?')}")
    else:
        console.print("  No ingest run summary found yet - has ingest run at least once?")

    for label, filename in (
        ("still encrypted", "still_encrypted.txt"),
        ("still corrupt", "still_corrupt.txt"),
        ("rejected as unsafe", "still_unsafe.txt"),
        ("stuck multi-volume parts", "still_multivolume.txt"),
        ("recovered by password-cracking", "decrypted.txt"),
    ):
        path = REPO_ROOT / "status" / filename
        count = len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0
        console.print(f"  {label}: {count}")

    return 0


def _with_liveness_marker(marker_name: str, func):
    """Wraps a scan command so status/<marker_name> exists for exactly as
    long as it's running - the same real-liveness-marker pattern
    unpack/start.sh and ingest/start.sh already use (status/extracting,
    status/ingesting; see marker_status()'s own comment on why a liveness
    touch is needed instead of just inferring "running" from other files).
    web/app.py's /status mount is read-only, so this has to be written from
    here, on the host, not from the web container.
    """

    def wrapped(args):
        marker = REPO_ROOT / "status" / marker_name
        marker.touch()
        try:
            return func(args)
        finally:
            marker.unlink(missing_ok=True)

    return wrapped


def cmd_pii_scan(args) -> int:
    """A post-pass (item 31), not an ingest-time enrichment: it runs
    against attachment.content, which only exists once Elasticsearch's own
    ingest pipeline has already Tika-parsed a document - ingest.py itself
    only ever sees the original file's raw bytes, never the extracted
    text, so detection can't happen any earlier than this.
    """
    query = {"match_all": {}} if args.rescan else {"bool": {"must_not": {"exists": {"field": "pii.has_pii"}}}}
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={"size": 200, "_source": ["attachment.content"], "query": query},
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    totals = dict.fromkeys(PII_FIELDS, 0)
    scanned = 0
    with_pii = 0
    failures: list[str] = []

    try:
        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            actions = []
            for hit in hits:
                content = hit.get("_source", {}).get("attachment", {}).get("content", "") or ""
                result = pii.detect_all(content)
                scanned += 1
                if result["has_pii"]:
                    with_pii += 1
                for key in totals:
                    totals[key] += len(result[key])
                actions.append({"update": {"_index": INDEX, "_id": hit["_id"]}})
                actions.append({"doc": {"pii": result}})

            if actions:
                failures.extend(bulk_failures(es_bulk(actions)))

            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    console.print(f"Scanned {scanned} document(s), {with_pii} with at least one identifier found.")
    table = Table(title="Identifiers found")
    table.add_column("Type")
    table.add_column("Count")
    for key, count in totals.items():
        table.add_row(key, str(count))
    console.print(table)

    if failures:
        console.print(f"[red]{len(failures)} document(s) could not be updated - these results were NOT saved:[/red]")
        for reason in failures[:10]:
            console.print(f"  [red]{reason}[/red]")
        if len(failures) > 10:
            console.print(f"  [red]... and {len(failures) - 10} more.[/red]")
        return 1
    return 0


# Rows shown in a terminal table before telling the user to use --output
# instead - a real corpus can have thousands of matches (6128 in one seen
# while building this), which both floods the terminal and takes rich
# noticeably longer to render than reading the same hits already did.
PII_REPORT_TABLE_LIMIT = 200


def cmd_pii_report(args) -> int:
    """Lists what pii-scan already found, rather than finding it again -
    pii-scan's own summary table (see cmd_pii_scan above) only ever
    printed per-type counts, with no way to get the actual matches back
    out short of hand-writing an Elasticsearch query. Reads only
    (pii.has_pii: true), never touches pii-scan's own results.
    """
    query = {"term": {"pii.has_pii": True}}
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={
                "size": 200,
                "_source": ["filename", "sha256", *[f"pii.{field}" for field in PII_FIELDS]],
                "query": query,
            },
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    total = response.get("hits", {}).get("total", {}).get("value", 0)
    rows: list[list[str]] = []
    writer = None
    output_file = None

    try:
        if args.output:
            output_file = args.output.open("w", newline="", encoding="utf-8")
            writer = csv.writer(output_file)
            writer.writerow(["filename", "sha256", *PII_FIELDS])

        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            for hit in hits:
                source = hit.get("_source", {})
                pii_result = source.get("pii", {})
                row = [source.get("filename", ""), source.get("sha256", "")]
                row += ["; ".join(pii_result.get(field, []) or []) for field in PII_FIELDS]
                if writer:
                    writer.writerow(row)
                elif len(rows) < PII_REPORT_TABLE_LIMIT:
                    # The table is a quick-scan preview, not the real
                    # output (--output is) - a full path plus a full
                    # sha256 leaves rich almost no room for the fields
                    # that actually matter here, wrapping every row
                    # across a dozen lines. Basename only; each column
                    # below is also capped with overflow="ellipsis".
                    rows.append([Path(row[0]).name, row[1], *row[2:]])

            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except OSError as error:
        console.print(f"[red]Could not write {args.output}: {error}[/red]")
        return 1
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if output_file:
            output_file.close()
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    if writer:
        console.print(f"Wrote {total} document(s) with personal identifiers to {args.output}.")
        return 0

    console.print(f"{total} document(s) with personal identifiers found.")
    table = Table()
    table.add_column("Filename", overflow="ellipsis", max_width=40, no_wrap=True)
    table.add_column("SHA256", overflow="ellipsis", max_width=12, no_wrap=True)
    for column in PII_FIELDS:
        table.add_column(column, overflow="ellipsis", max_width=24, no_wrap=True)
    for row in rows:
        table.add_row(*row)
    console.print(table)
    if total > len(rows):
        console.print(
            f"[yellow]{total - len(rows)} more not shown - use --output <file> to export all of them.[/yellow]"
        )
    return 0


def cmd_language_scan(args) -> int:
    """A post-pass correction on top of the ingest-time stopword script
    (setup/entrypoint.sh's deis-detect-language): that script only tags a
    document english/swedish if it finds 3+ hits from a fixed list of ten
    common function words, so it only ever fires on flowing prose - a
    genuinely Swedish payroll table or bank-transfer report (all labels
    and numbers, no sentences) falls through to "unknown" even though a
    human reads it as Swedish instantly. Confirmed directly against real
    documents from this corpus. bin/language.py's statistical classifier
    picks up a language from a handful of real words without needing them
    arranged into sentences - see its own docstring.

    Default query only re-examines documents currently tagged "unknown" -
    the stopword script's own guess is trusted where it already made one,
    and this only fills the gap. --rescan reclassifies everything, in case
    a document the stopword script guessed wrong on is worth a second,
    more accurate opinion.
    """
    import language  # deliberately lazy, see the top-of-file comment

    query = {"match_all": {}} if args.rescan else {"term": {"language": "unknown"}}
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={"size": 200, "_source": ["attachment.content"], "query": query},
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    counts = {"english": 0, "swedish": 0, "unknown": 0}
    failures: list[str] = []

    try:
        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            actions = []
            for hit in hits:
                content = hit.get("_source", {}).get("attachment", {}).get("content", "") or ""
                detected = language.detect_language(content)
                counts[detected] += 1
                actions.append({"update": {"_index": INDEX, "_id": hit["_id"]}})
                actions.append({"doc": {"language": detected}})

            if actions:
                failures.extend(bulk_failures(es_bulk(actions)))

            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    scanned = sum(counts.values())
    console.print(
        f"Reclassified {scanned} document(s): {counts['english']} english, "
        f"{counts['swedish']} swedish, {counts['unknown']} still unknown."
    )

    if failures:
        console.print(f"[red]{len(failures)} document(s) could not be updated - these results were NOT saved:[/red]")
        for reason in failures[:10]:
            console.print(f"  [red]{reason}[/red]")
        if len(failures) > 10:
            console.print(f"  [red]... and {len(failures) - 10} more.[/red]")
        return 1

    # Unlike pii-scan/entity-scan, "language" is set on every document
    # from ingest time onward (setup/entrypoint.sh's stopword-based first
    # guess - see this function's own docstring), so field-presence can't
    # tell the web UI "has language-scan's more accurate pass run yet" the
    # way pii.has_pii/entities.has_entities do. Same fix as dedupe-scan's
    # own summary file: write the result of the last completed run here,
    # for the web UI to read back instead.
    summary_path = REPO_ROOT / "status" / "language_scan_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "@timestamp": datetime.now(UTC).isoformat(),
                "rescan": bool(args.rescan),
                "documents_scanned": scanned,
                "english": counts["english"],
                "swedish": counts["swedish"],
                "unknown": counts["unknown"],
            }
        ),
        encoding="utf-8",
    )
    return 0


def cmd_entity_scan(args) -> int:
    """A post-pass (item 32), same reasoning as cmd_pii_scan above: entity
    extraction needs attachment.content, which only exists once
    Elasticsearch's own ingest pipeline has already Tika-parsed a
    document. Uses the `language` every document is already tagged with
    (item 32's already-fixed language-detection half) to pick the right
    spaCy model per document - see entities.py for why "unknown" (common
    on numeric/tabular documents) is skipped rather than guessed.

    Truncates each document's content to [entities] max_chars (deis.cfg)
    before running spaCy - see entities_max_chars()'s docstring for why:
    large documents are genuinely slow to run NER against, and this cap
    bounds that cost. 0 disables it.
    """
    import entities  # deliberately lazy, see the top-of-file comment

    max_chars = entities_max_chars()
    query = {"match_all": {}} if args.rescan else {"bool": {"must_not": {"exists": {"field": "entities.has_entities"}}}}
    # 5m, not the 1m every other scroll-based command here uses: spaCy NER
    # is genuinely slow (a real 194,000-character document measured ~7.5s
    # for tok2vec+ner alone - see entities_max_chars's own docstring), and
    # a batch of 200 documents can take well over a minute to process
    # between one scroll continuation and the next. Found live: 1m expired
    # mid-batch and the next continuation 404'd ("scroll context not
    # found"), which - see below - crashed with a raw traceback instead of
    # a clean error, since nothing caught it either.
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=5m",
            method="POST",
            body={"size": 200, "_source": ["attachment.content", "language"], "query": query},
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    totals = dict.fromkeys(ENTITY_FIELDS, 0)
    scanned = 0
    with_entities = 0
    skipped_unknown_language = 0
    failures: list[str] = []

    try:
        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            actions = []
            for hit in hits:
                source = hit.get("_source", {})
                content = source.get("attachment", {}).get("content", "") or ""
                if max_chars > 0:
                    content = content[:max_chars]
                language = source.get("language", "unknown")
                result = entities.detect_entities(content, language)
                scanned += 1
                if language not in ("english", "swedish"):
                    skipped_unknown_language += 1
                if result["has_entities"]:
                    with_entities += 1
                for key in totals:
                    totals[key] += len(result[key])
                actions.append({"update": {"_index": INDEX, "_id": hit["_id"]}})
                actions.append({"doc": {"entities": result}})

            if actions:
                failures.extend(bulk_failures(es_bulk(actions)))

            response = es_request("/_search/scroll", method="POST", body={"scroll": "5m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    console.print(
        f"Scanned {scanned} document(s), {with_entities} with at least one entity found "
        f"({skipped_unknown_language} skipped - language unknown)."
    )
    table = Table(title="Entities found")
    table.add_column("Type")
    table.add_column("Count")
    for key, count in totals.items():
        table.add_row(key, str(count))
    console.print(table)

    if failures:
        console.print(f"[red]{len(failures)} document(s) could not be updated - these results were NOT saved:[/red]")
        for reason in failures[:10]:
            console.print(f"  [red]{reason}[/red]")
        if len(failures) > 10:
            console.print(f"  [red]... and {len(failures) - 10} more.[/red]")
        return 1
    return 0


def cmd_entity_report(args) -> int:
    """Lists what entity-scan already found, rather than finding it again -
    mirrors cmd_pii_report exactly, same reasoning: entity-scan's own
    summary table only ever printed per-type counts, with no way to get
    the actual matches back out short of hand-writing an Elasticsearch
    query. Reads only (entities.has_entities: true), never touches
    entity-scan's own results.
    """
    query = {"term": {"entities.has_entities": True}}
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={
                "size": 200,
                "_source": ["filename", "sha256", *[f"entities.{field}" for field in ENTITY_FIELDS]],
                "query": query,
            },
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    total = response.get("hits", {}).get("total", {}).get("value", 0)
    rows: list[list[str]] = []
    writer = None
    output_file = None

    try:
        if args.output:
            output_file = args.output.open("w", newline="", encoding="utf-8")
            writer = csv.writer(output_file)
            writer.writerow(["filename", "sha256", *ENTITY_FIELDS])

        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            for hit in hits:
                source = hit.get("_source", {})
                entities_result = source.get("entities", {})
                row = [source.get("filename", ""), source.get("sha256", "")]
                row += ["; ".join(entities_result.get(field, []) or []) for field in ENTITY_FIELDS]
                if writer:
                    writer.writerow(row)
                elif len(rows) < PII_REPORT_TABLE_LIMIT:
                    # Same reasoning as cmd_pii_report's own table: a full
                    # path/sha256 leaves rich almost no room for the
                    # fields that actually matter in a quick-scan preview.
                    rows.append([Path(row[0]).name, row[1], *row[2:]])

            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except OSError as error:
        console.print(f"[red]Could not write {args.output}: {error}[/red]")
        return 1
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if output_file:
            output_file.close()
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    if writer:
        console.print(f"Wrote {total} document(s) with named entities to {args.output}.")
        return 0

    console.print(f"{total} document(s) with named entities found.")
    table = Table()
    table.add_column("Filename", overflow="ellipsis", max_width=40, no_wrap=True)
    table.add_column("SHA256", overflow="ellipsis", max_width=12, no_wrap=True)
    for column in ENTITY_FIELDS:
        table.add_column(column, overflow="ellipsis", max_width=24, no_wrap=True)
    for row in rows:
        table.add_row(*row)
    console.print(table)
    if total > len(rows):
        console.print(
            f"[yellow]{total - len(rows)} more not shown - use --output <file> to export all of them.[/yellow]"
        )
    return 0


def cmd_geo_scan(args) -> int:
    """GPS EXIF extraction (see bin/geo.py's own docstring for the
    investigative motivation - a manual spot check found real coordinates
    on 40 of 138 JPEGs in this corpus). Unlike pii-scan/entity-scan, this
    can't read attachment.content - EXIF lives in the image's own raw
    bytes, which the ingest pipeline never keeps (the attachment
    processor's "remove_binary" drops them once Tika's done with them) -
    so this reads each candidate file back off disk instead, via the
    same extracted/sha256/<sha> symlink ingest.py itself creates
    (create_hash_link), rather than attachment.content_type.keyword's own
    "filename" field, which resolve_filepath() can remap to an unrelated
    sqlite-resolved name.

    Scoped to attachment.content_type.keyword: image/jpeg - the only
    format this corpus's spot check covered, and the only one
    bin/geo.py's extract_gps() understands (JPEG's Exif APP1 segment).
    PNG can carry Exif too (a rarer, newer addition to the format), but
    no evidence yet that this corpus's PNGs do - left as a known gap
    rather than guessed at.
    """
    query = {"term": {"attachment.content_type.keyword": "image/jpeg"}}
    if not args.rescan:
        query = {"bool": {"must": query, "must_not": {"exists": {"field": "location"}}}}
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={"size": 200, "_source": False, "query": query},
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    scanned = 0
    with_location = 0
    missing_on_disk = 0
    failures: list[str] = []

    try:
        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            actions = []
            for hit in hits:
                sha256 = hit["_id"]
                scanned += 1
                path = REPO_ROOT / "extracted" / "sha256" / sha256
                try:
                    content = path.read_bytes()
                except OSError:
                    missing_on_disk += 1
                    continue
                location = geo.extract_gps(content)
                if location is None:
                    continue
                with_location += 1
                actions.append({"update": {"_index": INDEX, "_id": sha256}})
                actions.append({"doc": {"location": location}})

            if actions:
                failures.extend(bulk_failures(es_bulk(actions)))

            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    console.print(f"Scanned {scanned} image(s), {with_location} with GPS coordinates found.")
    if missing_on_disk:
        console.print(
            f"[yellow]{missing_on_disk} image(s) skipped: not found under extracted/sha256/ (moved/removed "
            "since ingest, or ingested before that symlink existed).[/yellow]"
        )

    if failures:
        console.print(f"[red]{len(failures)} document(s) could not be updated - these results were NOT saved:[/red]")
        for reason in failures[:10]:
            console.print(f"  [red]{reason}[/red]")
        if len(failures) > 10:
            console.print(f"  [red]... and {len(failures) - 10} more.[/red]")
        return 1

    # Same reasoning as language-scan's own summary file: "location" is
    # absent on most documents by design (only images can carry it, and
    # most images never had a GPS fix), so unlike pii.has_pii/
    # entities.has_entities, a live tagged/total count against the whole
    # corpus would be misleading rather than genuine progress.
    summary_path = REPO_ROOT / "status" / "geo_scan_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "@timestamp": datetime.now(UTC).isoformat(),
                "rescan": bool(args.rescan),
                "images_scanned": scanned,
                "with_location": with_location,
            }
        ),
        encoding="utf-8",
    )
    return 0


def cmd_geo_report(args) -> int:
    """Lists what geo-scan already found, rather than finding it again -
    same reasoning as pii-report/entity-report: reads only (an `exists:
    location` query), never touches geo-scan's own results. Each row is
    enriched with the nearest city of at least 100,000 people (see
    bin/geo.py's nearest_city()/bin/cities.tsv) to that point - computed
    fresh here, on the ~40-in-this-corpus scale a report reads at, rather
    than stored back on the document: it's a display convenience for a
    human reading this table, not a fact about the document itself worth
    persisting or searching on in Kibana.
    """
    query = {"exists": {"field": "location"}}
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={"size": 200, "_source": ["filename", "sha256", "location"], "query": query},
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    total = response.get("hits", {}).get("total", {}).get("value", 0)
    rows: list[list[str]] = []
    writer = None
    output_file = None

    try:
        if args.output:
            output_file = args.output.open("w", newline="", encoding="utf-8")
            writer = csv.writer(output_file)
            writer.writerow(["filename", "sha256", "lat", "lon", "nearest_city", "country", "distance_km", "link"])

        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break

            for hit in hits:
                source = hit.get("_source", {})
                sha256 = source.get("sha256", hit["_id"])
                location = source.get("location", {})
                lat, lon = location.get("lat"), location.get("lon")
                city = geo.nearest_city(lat, lon) if lat is not None and lon is not None else None
                city_name = city["name"] if city else ""
                city_country = city["country"] if city else ""
                city_distance = f"{city['distance_km']:g}" if city else ""
                link = f"{VIEW_URL}/{sha256}"
                if writer:
                    writer.writerow(
                        [source.get("filename", ""), sha256, lat, lon, city_name, city_country, city_distance, link]
                    )
                elif len(rows) < PII_REPORT_TABLE_LIMIT:
                    # Filename only, not the full path - same reasoning as
                    # pii-report/entity-report's own table: a full
                    # extracted/files/<sha>/... path leaves rich almost no
                    # room for the columns that actually matter here.
                    nearest = f"{city_name} ({city_country}), {city_distance} km" if city else ""
                    rows.append([Path(source.get("filename", "")).name, nearest, _rich_link(link)])

            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except OSError as error:
        console.print(f"[red]Could not write {args.output}: {error}[/red]")
        return 1
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if output_file:
            output_file.close()
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    if writer:
        console.print(f"Wrote {total} document(s) with GPS coordinates to {args.output}.")
        return 0

    console.print(f"{total} document(s) with GPS coordinates found.")
    table = Table()
    table.add_column("Filename", overflow="ellipsis", max_width=40, no_wrap=True)
    table.add_column("Nearest city")
    table.add_column("Link", overflow="fold")
    for row in rows:
        table.add_row(*row)
    console.print(table)
    if total > len(rows):
        console.print(
            f"[yellow]{total - len(rows)} more not shown - use --output <file> to export all of them.[/yellow]"
        )
    return 0


def cmd_dedupe_scan(args) -> int:
    """Near-duplicate clustering (item 33) - a whole-corpus operation,
    unlike pii-scan/language detection which are independent per-document
    facts: whether a document belongs to a cluster depends on every other
    document too, so this always recomputes from scratch on every run
    rather than skipping already-tagged documents.
    """
    try:
        es_request(
            f"/{INDEX}/_update_by_query?conflicts=proceed",
            method="POST",
            body={
                "query": {"exists": {"field": "duplicate_cluster"}},
                "script": {"source": "ctx._source.remove('duplicate_cluster')"},
            },
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Could not clear stale cluster assignments: {error}[/red]")
        return 1

    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={"size": 200, "_source": ["attachment.content"], "query": {"match_all": {}}},
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    fingerprints: dict[str, int] = {}
    no_words = 0
    try:
        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                content = hit.get("_source", {}).get("attachment", {}).get("content", "") or ""
                # None means "no alphabetic words at all" (see
                # simhash.fingerprint) - a purely numeric/tabular document
                # has nothing to compare, and treating it as a fingerprint
                # would make every such document a perfect match for every
                # other one. Counted and reported, not silently dropped.
                if (value := simhash.fingerprint(content)) is None:
                    no_words += 1
                else:
                    fingerprints[hit["_id"]] = value
            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    clusters = simhash.cluster(fingerprints, max_distance=args.max_distance)

    actions = []
    for doc_id, representative in clusters.items():
        actions.append({"update": {"_index": INDEX, "_id": doc_id}})
        actions.append({"doc": {"duplicate_cluster": representative}})
    # Chunked in pairs (each update is two lines), so 400 keeps every
    # action line with its own document line.
    failures: list[str] = []
    for i in range(0, len(actions), 400):
        failures.extend(bulk_failures(es_bulk(actions[i : i + 400])))

    cluster_sizes: dict[str, int] = {}
    for representative in clusters.values():
        cluster_sizes[representative] = cluster_sizes.get(representative, 0) + 1

    console.print(
        f"Scanned {len(fingerprints)} document(s) with comparable text, "
        f"{len(clusters)} in {len(cluster_sizes)} near-duplicate cluster(s)."
    )
    if no_words:
        console.print(
            f"[yellow]{no_words} document(s) skipped: no alphabetic words to compare "
            "(numeric/tabular content, or nothing Tika could extract).[/yellow]"
        )
    if cluster_sizes:
        table = Table(title="Largest clusters")
        table.add_column("Representative sha256")
        table.add_column("Members")
        for representative, size in sorted(cluster_sizes.items(), key=lambda kv: -kv[1])[:10]:
            table.add_row(representative, str(size))
        console.print(table)

    if failures:
        console.print(f"[red]{len(failures)} document(s) could not be updated - clusters were NOT saved:[/red]")
        for reason in failures[:10]:
            console.print(f"  [red]{reason}[/red]")
        if len(failures) > 10:
            console.print(f"  [red]... and {len(failures) - 10} more.[/red]")
        return 1

    # Unlike pii-scan/entity-scan, a live document count can't show
    # dedupe-scan's progress while it runs (clusters aren't written until
    # the very end - see this function's own docstring), so the web UI
    # instead shows the result of the last completed run from this file.
    summary_path = REPO_ROOT / "status" / "dedupe_scan_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "@timestamp": datetime.now(UTC).isoformat(),
                "documents_scanned": len(fingerprints),
                "documents_clustered": len(clusters),
                "cluster_count": len(cluster_sizes),
                "documents_skipped_no_words": no_words,
            }
        ),
        encoding="utf-8",
    )
    return 0


def cmd_dedupe_report(args) -> int:
    """Lists what dedupe-scan already found, rather than finding it
    again - same reasoning as pii-report/entity-report, but aggregated
    by cluster rather than one row per document: cluster size is itself
    a signal (a large cluster is usually a mass-distributed template,
    low investigative value once read once; a document search/scan hit
    count is inflated by near-duplicate copies unless aggregated by
    cluster instead of counted per document). Default table shows one
    row per cluster, largest first; --output writes one row per
    *document* (every cluster member) for full detail.

    Unlike pii-report/entity-report, this can't stream rows as it
    scrolls - cluster size (and therefore sort order) isn't known until
    every member has been seen, so the full result is collected first.
    Fine at the scale dedupe-scan itself already targets (see
    simhash.cluster's own docstring: hundreds to low thousands of
    documents actually end up clustered, not the whole corpus).
    """
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={
                "size": 200,
                "_source": ["filename", "sha256", "duplicate_cluster"],
                "query": {"exists": {"field": "duplicate_cluster"}},
            },
        )
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    scroll_id = response.get("_scroll_id")
    clusters: dict[str, list[dict[str, str]]] = {}
    try:
        while True:
            hits = response.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                source = hit.get("_source", {})
                representative = source.get("duplicate_cluster", "")
                clusters.setdefault(representative, []).append(
                    {"filename": source.get("filename", ""), "sha256": source.get("sha256", "")}
                )
            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    except ES_REQUEST_ERRORS as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except ES_REQUEST_ERRORS:
                pass

    ordered = sorted(clusters.items(), key=lambda kv: -len(kv[1]))
    total_documents = sum(len(members) for members in clusters.values())

    if args.output:
        try:
            with args.output.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["duplicate_cluster", "cluster_size", "filename", "sha256"])
                for representative, members in ordered:
                    for member in members:
                        writer.writerow([representative, len(members), member["filename"], member["sha256"]])
        except OSError as error:
            console.print(f"[red]Could not write {args.output}: {error}[/red]")
            return 1
        console.print(f"Wrote {total_documents} document(s) across {len(clusters)} cluster(s) to {args.output}.")
        return 0

    console.print(f"{total_documents} document(s) across {len(clusters)} near-duplicate cluster(s).")
    table = Table()
    table.add_column("Representative filename", overflow="ellipsis", max_width=50, no_wrap=True)
    table.add_column("Representative sha256", overflow="ellipsis", max_width=12, no_wrap=True)
    table.add_column("Members")
    shown = ordered[:PII_REPORT_TABLE_LIMIT]
    for representative, members in shown:
        rep_filename = next((m["filename"] for m in members if m["sha256"] == representative), members[0]["filename"])
        table.add_row(Path(rep_filename).name, representative, str(len(members)))
    console.print(table)
    if len(ordered) > len(shown):
        console.print(
            f"[yellow]{len(ordered) - len(shown)} more cluster(s) not shown - "
            "use --output <file> to export all of them.[/yellow]"
        )
    return 0


def cmd_add_urls(args) -> int:
    target = args.target
    candidates = [target] if "://" in target else Path(target).read_text(encoding="utf-8").splitlines()

    urls_file = REPO_ROOT / "urls" / "urls.txt"
    urls_file.parent.mkdir(parents=True, exist_ok=True)
    existing = set(urls_file.read_text(encoding="utf-8").splitlines()) if urls_file.is_file() else set()

    queued = 0
    with urls_file.open("a", encoding="utf-8") as f:
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if candidate in existing:
                console.print(f"[yellow]Already queued, skipping: {candidate}[/yellow]")
                continue
            if not is_valid_url(candidate):
                console.print(f"[red]Skipping (unsupported scheme): {candidate}[/red]")
                continue
            f.write(candidate + "\n")
            existing.add(candidate)
            queued += 1
            console.print(f"[green]Queued: {candidate}[/green]")

    console.print(f"{queued} URL(s) queued.")
    return 0


def cmd_add_files(args) -> int:
    """Feeds already-downloaded files into the pipeline as if deis/done.sh
    had just moved them there, for when the original URLs are dead but the
    files themselves are available some other way (a colleague's copy, a
    different mirror, ...). Collision handling mirrors done.sh exactly, so
    a file added this way is indistinguishable from a normal download once
    it lands in files/.
    """
    roots = [Path(s) for s in args.source]
    missing = [root for root in roots if not root.exists()]
    if missing:
        for root in missing:
            console.print(f"[red]{root} does not exist.[/red]")
        return 1

    sources = []
    for root in roots:
        sources.extend([root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file()))
    if not sources:
        console.print(f"[yellow]No files found under {', '.join(str(r) for r in roots)}.[/yellow]")
        return 1

    files_dir = REPO_ROOT / "files"
    status_dir = REPO_ROOT / "status"
    files_dir.mkdir(exist_ok=True)
    status_dir.mkdir(exist_ok=True)

    copied = 0
    source_urls_file = status_dir / "source_urls.jsonl"
    with source_urls_file.open("a", encoding="utf-8") as urls_out:
        for src in sources:
            dest = files_dir / src.name
            if dest.exists():
                base, ext = src.stem, src.suffix
                n = 2
                while (files_dir / f"{base}-dup{n}{ext}").exists():
                    n += 1
                dest = files_dir / f"{base}-dup{n}{ext}"
                console.print(f"[yellow]{src.name} already exists in files/, copying as {dest.name} instead.[/yellow]")
            shutil.copyfile(src, dest)
            copied += 1

            # item 46's provenance tracking: no URL exists for a file added
            # this way (that's the whole reason to use add-files - the
            # original is dead or was never downloaded by this pipeline at
            # all), but recording it here with an empty url still lets
            # ingest.py's resolve_source_chain() tell "known local add"
            # apart from "no record at all" (an older corpus, or a file
            # that predates this feature) - see ingest.py's own docstring.
            # Streamed, not dest.read_bytes() - item 28 already flagged
            # reading a whole file into memory as a real bug elsewhere in
            # this project (bin/pathfix.py), and this needs to work on
            # arbitrarily large leak-dump files too.
            sha256_hash = hashlib.sha256()
            with dest.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    sha256_hash.update(chunk)
            urls_out.write(json.dumps({"sha256": sha256_hash.hexdigest(), "url": "", "filename": src.name}) + "\n")

    # Matches the state deis/done.sh leaves behind once a real download's
    # files have been moved into files/, so the rest of the pipeline (status
    # display, unpack's own trigger check) can't tell the difference.
    for marker in ("added_urls", "downloaded", "unpack"):
        (status_dir / marker).touch()

    console.print(f"[green]Copied {copied} file(s) into files/.[/green]")
    if (status_dir / "extract_done").exists():
        console.print(
            "[yellow]status/extract_done already exists from an earlier run - unpack.sh only extracts once, "
            "so it won't pick these up unless you remove status/extract_done (and status/ingest_done) first.[/yellow]"
        )
    else:
        console.print("Next: 'bin/deis run --only extract', then 'bin/deis run --only ingest'.")
    return 0


def cmd_clean(_args) -> int:
    return _confirm_and_run(["just", "clean"], "delete downloader state, log files, and controller's web page")


def cmd_reset(_args) -> int:
    return _confirm_and_run(
        ["just", "dist-clean"], "delete ALL downloaded, extracted, and Jupyter state - this deletes evidence"
    )


# Saved-object types actually worth preserving - Kibana's own internal/
# system types (config, telemetry, space, ...) are excluded deliberately:
# they're this Kibana instance's own state, not analysis content the
# operator built, and re-importing them into a different (freshly
# `deis init`'d) instance later could conflict rather than help. Confirmed
# live against the Saved Objects _export API (which requires an explicit
# type list - passing none is a 400, "Either `type` or `objects` are
# required") that this list matches what _find already reports exists.
#
# canvas-workspace deliberately NOT included, despite _find still
# reporting it as a registered type: Canvas was deprecated and its type
# marked non-exportable as of this project's pinned ELASTIC_VERSION
# (confirmed live - _export 400s with "Trying to export non-exportable
# type(s): canvas-workspace" even with zero Canvas objects present).
def _backup_es_volume(destination: Path) -> None:
    """Tars the entire ES_VOLUME_NAME Docker volume into destination/es-data.tar,
    via a throwaway container (VOLUME_BACKUP_IMAGE) that mounts the named
    volume - not a direct host-filesystem copy, since a Docker volume isn't
    reachable as a host path at all on macOS/Windows (Docker Desktop's VM
    owns it); this works identically on every platform Docker runs on.

    Elasticsearch itself must already be stopped when this runs (the
    caller's job - see cmd_archive) so the tar reads a consistent, fully
    flushed set of files, not ones mid-write.

    Captures everything in the volume, not just leakdata-*/deis-ingest-runs
    the way the old snapshot-based approach did: Kibana's own saved objects
    (dashboards, searches, index-patterns) and the security realm (elastic/
    kibana_system passwords) both live as ordinary indices inside the same
    Elasticsearch data directory (.kibana-*, .security-*) - a full volume
    copy carries them along for free, verified live, with no separate
    export/import or password-reset step needed on restore.
    """
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{ES_VOLUME_NAME}:/data",
            "-v",
            f"{destination}:/backup",
            VOLUME_BACKUP_IMAGE,
            "tar",
            "-cf",
            "/backup/es-data.tar",
            "-C",
            "/data",
            ".",
        ],
        check=True,
    )


def _restore_es_volume(source: Path) -> None:
    """Recreates ES_VOLUME_NAME from source/es-data.tar (see _backup_es_volume) -
    the volume is removed and recreated first, not extracted on top of
    whatever is already there, so a restore always ends with exactly the
    archive's own data, never a mix with a previous `deis init`'s fresh,
    empty cluster.

    Recreated via `docker compose create` (creates the elasticsearch
    container and its volume without starting it - no data is written by
    an unstarted container), not a plain `docker volume create`: a
    volume made outside Compose lacks the project/config-hash labels
    Compose itself stamps on, and the very next `docker compose up`
    warns "already exists but was not created by Docker Compose" -
    confirmed live, harmless but noisy every single restore.
    """
    subprocess.run(["docker", "volume", "rm", "-f", ES_VOLUME_NAME], check=False)
    subprocess.run(["docker", "compose", "create", "elasticsearch"], cwd=REPO_ROOT, check=True)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{ES_VOLUME_NAME}:/data",
            "-v",
            f"{source}:/backup",
            VOLUME_BACKUP_IMAGE,
            "tar",
            "-xf",
            "/backup/es-data.tar",
            "-C",
            "/data",
        ],
        check=True,
    )


def _compose_images(profiles: list[str]) -> list[str]:
    """The authoritative list of every image docker-compose.yml would use
    for the given profiles - the same primitive cmd_build's own profile
    selection already implicitly relies on, not a hand-maintained list
    that could silently drift from docker-compose.yml itself.
    """
    command = ["docker", "compose"]
    for profile in profiles:
        command += ["--profile", profile]
    command += ["config", "--images"]
    result = subprocess.run(command, cwd=REPO_ROOT, check=True, capture_output=True, text=True)
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def _save_container_logs(destination: Path) -> None:
    """One plain-text file per service under `destination` - a reference
    for later ("what did unpack.sh actually print during this run"), not
    something `deis restore` ever reads back. `docker compose logs` works
    against a stopped-but-not-removed container the same as a running
    one, so this captures one-shot services (setup/unpack/ingest) too,
    not just the long-running ones. A service with no container at all
    yet (never started) just gets no file - not an error.
    """
    destination.mkdir(parents=True, exist_ok=True)
    services = subprocess.run(
        ["docker", "compose", "--profile", "deis", "--profile", "setup", "config", "--services"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for service in services:
        result = subprocess.run(
            ["docker", "compose", "logs", "--no-color", "--timestamps", service],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.stdout.strip():
            (destination / f"{service}.log").write_text(result.stdout, encoding="utf-8")


def _image_present_locally(image: str) -> bool:
    """Works for both a tag reference (deis-elasticsearch:latest) and a
    digest reference (reuteras/container-notebook@sha256:...) - `docker
    images --format` alone doesn't cleanly match the latter, `docker
    image inspect` handles both the same way.
    """
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode == 0


def _build_manifest(images: list[str], missing_images: list[str], counts: dict) -> dict:
    """Pure - the recorded facts about one archive, used both to write
    manifest.json (cmd_archive) and to sanity-check a restore against it
    (see _manifest_mismatches below).
    """
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    return {
        "archived_at": datetime.now(UTC).isoformat(),
        "deis_commit": commit or "unknown",
        "elastic_version": read_env().get("ELASTIC_VERSION", "unknown"),
        "images": images,
        "missing_images": missing_images,
        "counts": counts,
    }


def _manifest_mismatches(manifest: dict, live_counts: dict) -> list[str]:
    """Compares a restored instance's live funnel counts against what the
    archive's own manifest recorded at archive time - pure, so it's
    testable without a live stack. Returns one human-readable line per
    mismatch, not an exception: worth surfacing loudly, not treating as
    fatal (the operator may have deliberately added more data since).
    """
    mismatches = []
    for key, archived_value in manifest.get("counts", {}).items():
        live_value = live_counts.get(key)
        if live_value != archived_value:
            mismatches.append(f"{key}: archived {archived_value}, now {live_value}")
    return mismatches


def _archive_commit_mismatch(target_commit: str, current_commit: str) -> bool:
    """True if the archive's manifest names a real, different commit than
    the current checkout - i.e. a mismatch worth acting on. Pure, so it's
    testable without a live git checkout. False for an "unknown"
    deis_commit (the archive was made outside a git checkout - nothing to
    switch to) or an empty current_commit (git itself unavailable here).
    """
    return bool(current_commit and target_commit and target_commit not in ("unknown", current_commit))


def cmd_archive(args) -> int:
    """Archives everything needed to reopen this case later, fully
    queryable in Kibana again, without depending on re-running the
    pipeline (source URLs may be dead by then, and re-extracting/
    re-ingesting a large corpus is slow) or on rebuilding/re-pulling the
    exact same container versions later (a floating base image or an
    upstream release change could silently produce a different result -
    same reasoning as unpack/VENDORED.md's and downloader/VENDORED.md's
    own pinning).

    Stops Elasticsearch and tars its entire Docker volume (see
    _backup_es_volume), rather than the ES snapshot API this used
    originally. Elastic's own docs call a volume copy unsupported for a
    live, multi-node cluster - this project is neither: a single stopped
    node's data directory is just files on disk with no in-flight writes
    or cross-node consistency to worry about, and switching to this
    approach was a deliberate reversal after the snapshot approach's own
    real bugs (a Kibana saved-objects export/import round-trip needing
    its own error handling and retry logic, and a security realm -
    kibana_system's password - that a snapshot restore doesn't carry,
    needing to be re-bootstrapped by hand). A full volume copy carries
    Kibana's saved objects and the security realm along for free, since
    both live as ordinary indices inside the same Elasticsearch data
    directory - confirmed live before switching, not assumed.

    Also saves every service's container logs as plain text
    (container-logs/<service>.log) - a reference for later, not something
    `deis restore` ever reads back.

    Also copies .env (ELASTIC_PASSWORD/JUPYTER_TOKEN/RPCSECRET) - a
    deliberate choice, not a default: the dump itself is far more
    sensitive than these local stack credentials, and treating the whole
    archive with the same care as the dump (rather than trying to
    separate out "just the secrets" for lighter handling) is what makes
    `deis restore` alone - no separate `deis init` first - the entire
    onboarding step on a fresh checkout.
    """
    destination: Path = args.destination
    destination.mkdir(parents=True, exist_ok=True)

    try:
        health = es_request("/_cluster/health")
        console.print(f"Elasticsearch reachable (status: {health.get('status', 'unknown')}).")
    except ES_REQUEST_ERRORS:
        console.print("[yellow]Elasticsearch is not reachable - archiving whatever is in its volume as-is.[/yellow]")

    console.print("[yellow]Stopping Elasticsearch briefly to copy its data directory...[/yellow]")
    subprocess.run(["docker", "compose", "stop", "elasticsearch"], cwd=REPO_ROOT, check=False)

    console.print("Backing up the Elasticsearch volume (this can take a while on a large corpus)...")
    _backup_es_volume(destination)

    console.print("Restarting Elasticsearch...")
    subprocess.run(["docker", "compose", "up", "-d", "elasticsearch"], cwd=REPO_ROOT, check=True)
    console.print("Waiting for Elasticsearch to become reachable again...")
    for _ in range(60):
        try:
            if es_request("/_cluster/health").get("status") in ("yellow", "green"):
                break
        except ES_REQUEST_ERRORS:
            pass
        time.sleep(5)
    else:
        console.print("[yellow]Elasticsearch did not come back up in time - continuing anyway.[/yellow]")

    console.print("Saving container logs...")
    _save_container_logs(destination / "container-logs")

    console.print("Saving container images (this can take a while)...")
    images = _compose_images(["deis", "setup"])
    to_save = [image for image in images if _image_present_locally(image)]
    missing = [image for image in images if image not in to_save]
    if missing:
        console.print(f"[yellow]Not present locally, skipping (run 'deis build' first?): {', '.join(missing)}[/yellow]")
    if to_save:
        subprocess.run(["docker", "save", *to_save, "-o", str(destination / "images.tar")], check=True)

    console.print("Archiving extracted/ and status/ (this can take a while)...")
    subprocess.run(["tar", "-cf", str(destination / "extracted.tar"), "extracted", "status"], cwd=REPO_ROOT, check=True)

    shutil.copyfile(REPO_ROOT / "deis.cfg", destination / "deis.cfg")
    # Included deliberately, not a safer-by-default exclusion: the dump
    # itself is far more sensitive than these local stack credentials
    # (ELASTIC_PASSWORD/JUPYTER_TOKEN/RPCSECRET, nothing external), and an
    # archive handled with the same care as the dump makes "just venv &&
    # deis restore <archive>" work as a single self-contained onboarding
    # step - no separate `deis init` (and its own freshly-generated,
    # different credentials) needed at all. copy2, not copyfile - .env is
    # chmod 0600 (cmd_init's own care, since it holds secrets); a plain
    # copyfile drops permission bits and leaves the copy at the
    # destination filesystem's default (often world-readable).
    shutil.copy2(REPO_ROOT / ".env", destination / ".env")

    counts = {
        "unique_sha256": count_files(REPO_ROOT / "extracted" / "sha256", exclude=set()),
        "elasticsearch_documents": es_request(f"/{INDEX}/_count").get("count", 0),
    }
    manifest = _build_manifest(images, missing, counts)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    console.print(f"[green]Archive complete: {destination}[/green]")
    return 0


def cmd_restore(args) -> int:
    """Restores an archive written by `deis archive` - see cmd_archive's
    own docstring for what is captured, including .env/deis.cfg, so this
    is the entire onboarding step on a fresh checkout: `just venv` (for
    bin/'s own dependencies) then `deis restore <archive>` alone, no
    separate `deis init` needed first - not obviously non-destructive
    against an instance that already has other state, though, so this
    uses the same confirmation gate cmd_clean/cmd_reset do.

    Also switches this checkout to the commit that wrote the archive
    (detached HEAD) when it differs and the tree is clean - the restored
    data's shape (mappings, source_chain, row-index fields) depends on
    that code version, so running later code against it is a real
    correctness risk, not just a cosmetic mismatch. Left alone (with a
    warning instead) on a dirty tree: switching commits out from under
    uncommitted changes isn't this feature's call to make.

    Brings up the full review/analysis surface at the end, not just
    Elasticsearch+Kibana - web/gotenberg/notebook too, so a restore
    actually leaves every viewer a fresh `deis init` + `deis run` would
    have running, not just a queryable index. Restoring the Elasticsearch
    volume itself (see _restore_es_volume) needs no separate Kibana
    saved-objects import or kibana_system password-reset step the way the
    old snapshot-based restore did - both travel with the volume, since
    both are ordinary Elasticsearch indices under the hood.
    """
    source: Path = args.source
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        console.print(f"[red]{manifest_path} not found - is {source} a real 'deis archive' output?[/red]")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    console.print(f"Archive from {manifest.get('archived_at', '?')}, DEIS commit {manifest.get('deis_commit', '?')}.")
    target_commit = manifest.get("deis_commit", "")
    current_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    switch_commit = _archive_commit_mismatch(target_commit, current_commit)
    # Only auto-switch on a clean tree - git checkout itself refuses to
    # clobber a conflicting uncommitted change, but a *non*-conflicting one
    # would silently ride along onto the archive's commit, which isn't
    # this feature's call to make. Checked once here (not inside the
    # switch itself, after confirmation) so the warning already reflects
    # what is about to happen.
    tree_is_dirty = False
    if switch_commit:
        tree_is_dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
            ).stdout.strip()
        )
        if tree_is_dirty:
            console.print(
                "[yellow]This checkout is at a different commit than when the archive was made, and has "
                "uncommitted changes - NOT switching automatically. The restored data's shape (mappings, "
                "source_chain, row-index fields) depends on the code version that wrote it; commit or stash "
                f"your changes and run `git checkout {target_commit}` yourself if that matters here.[/yellow]"
            )
        else:
            console.print(
                f"[yellow]This checkout is at a different commit than when the archive was made - will "
                f"switch to {target_commit} (detached HEAD) before restoring.[/yellow]"
            )

    console.print("[red]This will load container images and write into extracted/, status/, and Elasticsearch.[/red]")
    if input("Type 'yes' to continue: ").strip().lower() != "yes":
        console.print("Aborted.")
        return 1

    if switch_commit and not tree_is_dirty:
        console.print(f"Checking out DEIS commit {target_commit}...")
        checkout = subprocess.run(
            ["git", "checkout", target_commit], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
        if checkout.returncode != 0:
            console.print(
                f"[yellow]Could not check out {target_commit}: {checkout.stderr.strip()} - "
                "continuing on the current commit.[/yellow]"
            )
        else:
            console.print(
                f"[green]Checked out {target_commit} (detached HEAD - `git checkout <branch>` returns to "
                "your branch afterward). Run `just venv` first if this commit's dependencies differ.[/green]"
            )

    images_tar = source / "images.tar"
    if images_tar.is_file():
        console.print("Loading container images...")
        subprocess.run(["docker", "load", "-i", str(images_tar)], check=True)
    else:
        console.print("[yellow]No images.tar in the archive - skipping (nothing was saved at archive time).[/yellow]")

    console.print("Extracting extracted/ and status/...")
    subprocess.run(["tar", "-xf", str(source / "extracted.tar")], cwd=REPO_ROOT, check=True)

    cfg_path = REPO_ROOT / "deis.cfg"
    archived_cfg = source / "deis.cfg"
    if archived_cfg.is_file():
        if cfg_path.exists():
            backup_path = REPO_ROOT / "deis.cfg.bak"
            cfg_path.replace(backup_path)  # replace(), not rename(): overwrites an existing .bak too
            console.print(
                f"[yellow]Existing deis.cfg moved to {backup_path.name} - applying the archive's copy.[/yellow]"
            )
        shutil.copyfile(archived_cfg, cfg_path)

    env_path = REPO_ROOT / ".env"
    archived_env = source / ".env"
    if archived_env.is_file():
        if env_path.exists():
            backup_path = REPO_ROOT / ".env.bak"
            env_path.replace(backup_path)  # replace(), not rename(): overwrites an existing .bak too
            console.print(f"[yellow]Existing .env moved to {backup_path.name} - applying the archive's copy.[/yellow]")
        shutil.copy2(archived_env, env_path)
        env_path.chmod(0o600)  # belt-and-braces on top of copy2's own preserved mode

    es_data_tar = source / "es-data.tar"
    if not es_data_tar.is_file():
        console.print(f"[red]{es_data_tar} not found - cannot restore Elasticsearch data.[/red]")
        return 1
    console.print("Stopping Elasticsearch...")
    subprocess.run(["docker", "compose", "stop", "elasticsearch"], cwd=REPO_ROOT, check=False)
    console.print("Restoring the Elasticsearch volume (this can take a while on a large corpus)...")
    _restore_es_volume(source)

    console.print("Starting Elasticsearch, the web viewer, gotenberg, and the notebook...")
    # Not `docker compose --profile deis up -d`: that profile also includes
    # downloader/controller/unpack/ingest/deis, the pipeline-processing
    # containers - restore is reopening already-processed data, not running
    # the pipeline again, so only the review/analysis services are started.
    # Kibana isn't started yet - starting it only once Elasticsearch is
    # actually reachable avoids it going through its own doomed connection
    # retries against a still-starting Elasticsearch for no reason.
    subprocess.run(
        ["docker", "compose", "up", "-d", "elasticsearch", "web", "gotenberg", "notebook"], cwd=REPO_ROOT, check=True
    )
    console.print("Waiting for Elasticsearch to become reachable...")
    for _ in range(60):
        try:
            if es_request("/_cluster/health").get("status") in ("yellow", "green"):
                break
        except ES_REQUEST_ERRORS:
            pass
        time.sleep(5)
    else:
        console.print("[red]Elasticsearch did not become reachable in time.[/red]")
        return 1
    console.print("Restore complete.")

    console.print("Starting Kibana...")
    subprocess.run(["docker", "compose", "up", "-d", "kibana"], cwd=REPO_ROOT, check=True)

    live_counts = {
        "unique_sha256": count_files(REPO_ROOT / "extracted" / "sha256", exclude=set()),
        "elasticsearch_documents": es_request(f"/{INDEX}/_count").get("count", 0),
    }
    mismatches = _manifest_mismatches(manifest, live_counts)
    if mismatches:
        console.print("[yellow]Funnel counts differ from the archive's manifest:[/yellow]")
        for line in mismatches:
            console.print(f"  [yellow]{line}[/yellow]")
    else:
        console.print("[green]Funnel counts match the archive's manifest.[/green]")

    console.print("[green]Restore complete.[/green]")
    return 0


def _bash_completion_script() -> str:
    """A hand-written completion function, not argcomplete-generated: the
    subcommand set is small and fixed, so this avoids adding a third-party
    dependency just to complete ~9 fixed words. Registered for both the
    'deis' and 'bin/deis' command names, since the documented invocation is
    the latter but a user may also have added bin/ to PATH or aliased it.
    """
    subcommands = " ".join(SUBCOMMANDS)
    only_choices = " ".join(RUN_ONLY_CHOICES)
    return f"""\
_deis_completions() {{
    local cur prev
    COMPREPLY=()
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"

    if ((COMP_CWORD == 1)); then
        mapfile -t COMPREPLY < <(compgen -W "{subcommands}" -- "${{cur}}")
        return 0
    fi

    case "${{COMP_WORDS[1]}}" in
    run)
        if [[ "${{prev}}" == "--only" ]]; then
            mapfile -t COMPREPLY < <(compgen -W "{only_choices}" -- "${{cur}}")
        else
            mapfile -t COMPREPLY < <(compgen -W "--only" -- "${{cur}}")
        fi
        ;;
    completion)
        mapfile -t COMPREPLY < <(compgen -W "bash zsh" -- "${{cur}}")
        ;;
    add-urls)
        mapfile -t COMPREPLY < <(compgen -f -- "${{cur}}")
        ;;
    esac
}}
complete -F _deis_completions deis
complete -F _deis_completions bin/deis
"""


def _zsh_completion_script() -> str:
    subcommands = " ".join(SUBCOMMANDS)
    only_choices = " ".join(RUN_ONLY_CHOICES)
    return f"""\
#compdef deis bin/deis

_deis() {{
    local -a subcommands
    subcommands=({subcommands})

    if ((CURRENT == 2)); then
        _describe 'command' subcommands
        return
    fi

    case ${{words[2]}} in
    run)
        _arguments '--only=[which stage to run]:stage:({only_choices})'
        ;;
    completion)
        _values 'shell' bash zsh
        ;;
    add-urls)
        _files
        ;;
    esac
}}

_deis "$@"
"""


def cmd_completion(args) -> int:
    print(_bash_completion_script() if args.shell == "bash" else _zsh_completion_script())
    return 0


def _confirm_and_run(command: list[str], warning: str) -> int:
    console.print(f"[red]This will {warning}.[/red]")
    answer = input("Type 'yes' to continue: ")
    if answer.strip().lower() != "yes":
        console.print("Aborted.")
        return 1
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deis", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="bootstrap .env and deis.cfg").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="preflight checks and diagnosis").set_defaults(func=cmd_doctor)

    sub.add_parser("build", help="build every container image (docker compose build)").set_defaults(func=cmd_build)

    sub.add_parser("setup", help="alias for 'run --only setup'").set_defaults(func=cmd_run, only="setup")

    p_run = sub.add_parser("run", help="start the pipeline (or one stage of it)")
    p_run.add_argument("--only", choices=list(RUN_ONLY_CHOICES))
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="snapshot of pipeline state and funnel counts").set_defaults(func=cmd_status)

    p_search = sub.add_parser("search", help="search indexed content")
    p_search.add_argument("term", nargs="?", help="text to search for in document content")
    p_search.add_argument(
        "--sha256",
        nargs="*",
        metavar="HASH",
        help="look up document(s) directly by sha256 instead of a content search; ignores 'term' if both are "
        "given. Combine with -f/--file to read hashes from a file, e.g. --sha256 -f status/decrypted.txt",
    )
    p_search.add_argument(
        "-f",
        "--file",
        type=Path,
        metavar="PATH",
        help="read sha256 hashes to look up from this file, one per line (e.g. status/still_multivolume.txt, "
        "status/decrypted.txt); combines with any hashes passed directly to --sha256",
    )
    p_search.add_argument(
        "--output", type=Path, help="write every match (snippet, filename, sha256, link) to this CSV file"
    )
    p_search.set_defaults(func=cmd_search)

    sub.add_parser("report", help="what was found, what could not be processed").set_defaults(func=cmd_report)

    p_add = sub.add_parser("add-urls", help="queue a URL, or a file of URLs, for download")
    p_add.add_argument("target", help="a single URL, or a path to a file of URLs (one per line)")
    p_add.set_defaults(func=cmd_add_urls)

    p_add_files = sub.add_parser(
        "add-files", help="copy already-downloaded files into the pipeline, skipping the download stage"
    )
    p_add_files.add_argument(
        "source",
        nargs="+",
        help="one or more files, or directories (searched recursively), of already-downloaded files",
    )
    p_add_files.set_defaults(func=cmd_add_files)

    p_pii = sub.add_parser("pii-scan", help="detect personal identifiers in indexed content")
    p_pii.add_argument("--rescan", action="store_true", help="rescan every document, not just unscanned ones")
    p_pii.set_defaults(func=_with_liveness_marker("pii_scanning", cmd_pii_scan))

    p_pii_report = sub.add_parser("pii-report", help="list what pii-scan already found")
    p_pii_report.add_argument(
        "--output", type=Path, help="write every match to this CSV file instead of a capped terminal table"
    )
    p_pii_report.set_defaults(func=cmd_pii_report)

    p_language = sub.add_parser("language-scan", help="reclassify documents tagged with an unknown language")
    p_language.add_argument("--rescan", action="store_true", help="reclassify every document, not just unknown ones")
    p_language.set_defaults(func=_with_liveness_marker("language_scanning", cmd_language_scan))

    p_entity = sub.add_parser("entity-scan", help="extract named entities (people/orgs/locations) from indexed content")
    p_entity.add_argument("--rescan", action="store_true", help="rescan every document, not just unscanned ones")
    p_entity.set_defaults(func=_with_liveness_marker("entity_scanning", cmd_entity_scan))

    p_entity_report = sub.add_parser("entity-report", help="list what entity-scan already found")
    p_entity_report.add_argument(
        "--output", type=Path, help="write every match to this CSV file instead of a capped terminal table"
    )
    p_entity_report.set_defaults(func=cmd_entity_report)

    p_dedupe = sub.add_parser("dedupe-scan", help="cluster near-duplicate documents")
    p_dedupe.add_argument(
        "--max-distance",
        type=int,
        default=10,
        dest="max_distance",
        help="max Hamming distance (of 64 bits) to consider two documents near-duplicates (default: 10)",
    )
    p_dedupe.set_defaults(func=_with_liveness_marker("dedupe_scanning", cmd_dedupe_scan))

    p_dedupe_report = sub.add_parser("dedupe-report", help="list what dedupe-scan already found, by cluster")
    p_dedupe_report.add_argument(
        "--output", type=Path, help="write one row per cluster member to this CSV file instead of a capped table"
    )
    p_dedupe_report.set_defaults(func=cmd_dedupe_report)

    p_geo = sub.add_parser("geo-scan", help="extract GPS coordinates from JPEG EXIF data")
    p_geo.add_argument("--rescan", action="store_true", help="re-check every JPEG, not just ones without a location")
    p_geo.set_defaults(func=_with_liveness_marker("geo_scanning", cmd_geo_scan))

    p_geo_report = sub.add_parser("geo-report", help="list what geo-scan already found, with the nearest big city")
    p_geo_report.add_argument(
        "--output", type=Path, help="write every match (incl. raw lat/lon) to this CSV file instead of a capped table"
    )
    p_geo_report.set_defaults(func=cmd_geo_report)

    p_archive = sub.add_parser(
        "archive", help="archive Elasticsearch data, Kibana objects, extracted files, and images for later restore"
    )
    p_archive.add_argument("destination", type=Path, help="directory to write the archive into")
    p_archive.set_defaults(func=cmd_archive)

    p_restore = sub.add_parser("restore", help="restore an archive written by 'deis archive'")
    p_restore.add_argument("source", type=Path, help="the archive directory 'deis archive' wrote")
    p_restore.set_defaults(func=cmd_restore)

    sub.add_parser("clean", help="wrap 'just clean' behind a confirmation prompt").set_defaults(func=cmd_clean)
    sub.add_parser("reset", help="wrap 'just dist-clean' behind a confirmation prompt").set_defaults(func=cmd_reset)

    p_completion = sub.add_parser("completion", help="print a shell completion script")
    p_completion.add_argument("shell", choices=["bash", "zsh"])
    p_completion.set_defaults(func=cmd_completion)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
