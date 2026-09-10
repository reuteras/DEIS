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
import urllib.error
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.table import Table

# bin/ is only guaranteed to be on sys.path when this file is run directly as
# a script (python adds the script's own directory automatically); loaded
# dynamically the way tests/test_deis_cli.py does, it isn't - so this makes
# the sibling import work either way.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pii
import simhash

# entities (item 32) is deliberately NOT imported up here alongside pii/
# simhash: unlike those, it pulls in spaCy and two trained models (see
# bin/VENDORED.md) - a real, ~1-2s-to-load dependency chain that every
# other subcommand (status/search/report/...) has no reason to pay the
# cost of, or depend on being installed at all, just to run `deis
# --help`. Imported lazily inside cmd_entity_scan() instead.

REPO_ROOT = Path(__file__).resolve().parent.parent
ES_URL = "http://127.0.0.1:9200"
KIBANA_URL = "http://127.0.0.1:5601"
INDEX = "leakdata-index-000001"
RUNS_INDEX = "deis-ingest-runs"
VIEW_URL = "http://127.0.0.1:8081/view"
ALLOWED_URL_SCHEMES = ("http://", "https://", "ftp://")
# Shared between cmd_pii_scan (what it writes) and cmd_pii_report (what it
# reads back) - see pii.detect_all()'s own result shape in bin/pii.py.
PII_FIELDS = ("personnummer", "emails", "phone_numbers", "ibans", "card_numbers")
# Same idea, for entities - see entities.detect_entities()'s result shape
# in bin/entities.py.
ENTITY_FIELDS = ("persons", "organizations", "locations")

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
    "entity-scan",
    "entity-report",
    "dedupe-scan",
    "clean",
    "reset",
    "completion",
)
RUN_ONLY_CHOICES = ("setup", "download", "extract", "ingest")

console = Console()


def read_env(path: Path = REPO_ROOT / ".env") -> dict[str, str]:
    """Parses .env's KEY=VALUE lines - the only place this project reads
    that file today is docker compose itself, so there's nothing existing
    to reuse; skips blank lines and comments the same way deis.cfg's own
    reader does.
    """
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


def entities_max_chars(path: Path = REPO_ROOT / "deis.cfg") -> int:
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
    """
    config = configparser.RawConfigParser()
    config.read(path)
    return config.getint("entities", "max_chars", fallback=20000)


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
    except (RuntimeError, urllib.error.URLError, TimeoutError):
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
    except (RuntimeError, urllib.error.URLError, TimeoutError) as error:
        console.print(f"[red]Elasticsearch: not reachable ({error}).[/red]")
        ok = False

    try:
        urllib.request.urlopen(KIBANA_URL, timeout=10)
        console.print("[green]Kibana: reachable.[/green]")
    except (urllib.error.URLError, TimeoutError) as error:
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
    return subprocess.run(["docker", "compose", "logs", "setup", "-f"], cwd=REPO_ROOT, check=False).returncode


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
    except (RuntimeError, urllib.error.URLError, TimeoutError, KeyError):
        counts.add_row("documents in Elasticsearch", "could not be read")

    if run := latest_run_summary():
        counts.add_row("failed in last ingest run", str(run.get("failed", "?")))

    console.print(counts)
    return 0


def cmd_search(args) -> int:
    try:
        response = es_request(
            f"/{INDEX}/_search",
            method="POST",
            body={"size": 20, "query": {"match": {"attachment.content": args.term}}},
        )
    except (RuntimeError, urllib.error.URLError, TimeoutError) as error:
        console.print(f"[red]Search failed: {error}[/red]")
        return 1

    hits = response.get("hits", {}).get("hits", [])
    console.print(f"{response.get('hits', {}).get('total', {}).get('value', 0)} hit(s) for {args.term!r}.")
    table = Table()
    table.add_column("Filename")
    table.add_column("Link")
    for hit in hits:
        source = hit["_source"]
        table.add_row(source.get("filename", ""), f"{VIEW_URL}/{source.get('sha256', '')}")
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
    except (RuntimeError, urllib.error.URLError, TimeoutError) as error:
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
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except (RuntimeError, urllib.error.URLError, TimeoutError):
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
    except (RuntimeError, urllib.error.URLError, TimeoutError) as error:
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
    finally:
        if output_file:
            output_file.close()
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except (RuntimeError, urllib.error.URLError, TimeoutError):
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
    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={"size": 200, "_source": ["attachment.content", "language"], "query": query},
        )
    except (RuntimeError, urllib.error.URLError, TimeoutError) as error:
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

            response = es_request("/_search/scroll", method="POST", body={"scroll": "1m", "scroll_id": scroll_id})
            scroll_id = response.get("_scroll_id")
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except (RuntimeError, urllib.error.URLError, TimeoutError):
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
    except (RuntimeError, urllib.error.URLError, TimeoutError) as error:
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
    finally:
        if output_file:
            output_file.close()
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except (RuntimeError, urllib.error.URLError, TimeoutError):
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
    except (RuntimeError, urllib.error.URLError, TimeoutError) as error:
        console.print(f"[red]Could not clear stale cluster assignments: {error}[/red]")
        return 1

    try:
        response = es_request(
            f"/{INDEX}/_search?scroll=1m",
            method="POST",
            body={"size": 200, "_source": ["attachment.content"], "query": {"match_all": {}}},
        )
    except (RuntimeError, urllib.error.URLError, TimeoutError) as error:
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
    finally:
        if scroll_id:
            try:
                es_request("/_search/scroll", method="DELETE", body={"scroll_id": [scroll_id]})
            except (RuntimeError, urllib.error.URLError, TimeoutError):
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
    p_search.add_argument("term")
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
    p_pii.set_defaults(func=cmd_pii_scan)

    p_pii_report = sub.add_parser("pii-report", help="list what pii-scan already found")
    p_pii_report.add_argument(
        "--output", type=Path, help="write every match to this CSV file instead of a capped terminal table"
    )
    p_pii_report.set_defaults(func=cmd_pii_report)

    p_entity = sub.add_parser("entity-scan", help="extract named entities (people/orgs/locations) from indexed content")
    p_entity.add_argument("--rescan", action="store_true", help="rescan every document, not just unscanned ones")
    p_entity.set_defaults(func=cmd_entity_scan)

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
    p_dedupe.set_defaults(func=cmd_dedupe_scan)

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
