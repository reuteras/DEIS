#!/usr/bin/env python3
"""Single entry point for operating DEIS, for people who don't want to learn
docker compose profiles, Kibana's Dev Tools console, or which of four marker
directories to check when something looks wrong. Wraps the existing scripts
and containers rather than replacing them - see docs/IMPROVEMENTS.md's "CLI"
section for the design.
"""

import argparse
import base64
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

REPO_ROOT = Path(__file__).resolve().parent.parent
ES_URL = "http://127.0.0.1:9200"
KIBANA_URL = "http://127.0.0.1:5601"
INDEX = "leakdata-index-000001"
RUNS_INDEX = "deis-ingest-runs"
VIEW_URL = "http://127.0.0.1:8081/view"
ALLOWED_URL_SCHEMES = ("http://", "https://", "ftp://")

# Single source of truth for both build_parser() and the completion scripts
# below, so the two can't silently drift apart.
SUBCOMMANDS = (
    "init",
    "doctor",
    "run",
    "status",
    "search",
    "report",
    "add-urls",
    "clean",
    "reset",
    "completion",
)
RUN_ONLY_CHOICES = ("download", "extract", "ingest")

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
    files = REPO_ROOT / "files"
    extracted = REPO_ROOT / "extracted"
    status = {}

    if (files / "download_failed").exists():
        status["download"] = "failed"
    elif (files / "downloaded").exists():
        status["download"] = "done"
    elif (files / "running").exists():
        status["download"] = "running"
    else:
        status["download"] = "not running"

    if (extracted / "files" / "done").exists():
        status["extract"] = "done"
    elif (files / "unpack").exists():
        status["extract"] = "running"
    else:
        status["extract"] = "waiting"

    if (extracted / "ingest_done").exists():
        status["ingest"] = "done"
    elif (extracted / "files" / "done").exists():
        status["ingest"] = "running"
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
        console.print("[green]Created .env with generated passwords.[/green]")

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

    console.print(
        "[yellow]Note: no TOR egress leak test yet (docs/IMPROVEMENTS.md item 42) - "
        "this does not confirm downloads are actually routed through TOR.[/yellow]"
    )

    return 0 if ok else 1


def cmd_run(args) -> int:
    profile = {"download": "download", "extract": "unpack", "ingest": "ingest"}.get(args.only, "deis")
    command = ["docker", "compose", "--profile", profile, "up", "-d"]
    console.print(f"Running: {' '.join(command)}")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def cmd_status(_args) -> int:
    status = marker_status()
    table = Table(title="DEIS status")
    table.add_column("Stage")
    table.add_column("State")
    for stage in ("download", "extract", "ingest"):
        table.add_row(stage, status[stage])
    console.print(table)

    downloaded = count_files(REPO_ROOT / "files", exclude=set())
    extracted = count_files(
        REPO_ROOT / "extracted" / "files",
        exclude={"done", "path.txt"},
    )
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
    ):
        path = REPO_ROOT / "extracted" / filename
        count = len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0
        console.print(f"  {label}: {count}")

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

    p_run = sub.add_parser("run", help="start the pipeline (or one stage of it)")
    p_run.add_argument("--only", choices=["download", "extract", "ingest"])
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="snapshot of pipeline state and funnel counts").set_defaults(func=cmd_status)

    p_search = sub.add_parser("search", help="search indexed content")
    p_search.add_argument("term")
    p_search.set_defaults(func=cmd_search)

    sub.add_parser("report", help="what was found, what could not be processed").set_defaults(func=cmd_report)

    p_add = sub.add_parser("add-urls", help="queue a URL, or a file of URLs, for download")
    p_add.add_argument("target", help="a single URL, or a path to a file of URLs (one per line)")
    p_add.set_defaults(func=cmd_add_urls)

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
