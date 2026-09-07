#!/usr/bin/env python3
"""."""

import html
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

import magic
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

app = FastAPI()

SYMLINKS_DIR = "/extracted/sha256"
EXTRACTED_ROOT = "/extracted"
FILES_DIR = "/files"
STATUS_DIR = "/status"
GOTENBERG_URL = "http://gotenberg:3000/forms/libreoffice/convert"
GOTENBERG_HTML_URL = "http://gotenberg:3000/forms/chromium/convert/html"
ELASTIC_INDEX = "leakdata-index-000001"
ELASTIC_RUNS_INDEX = "deis-ingest-runs"
ELASTIC_URL = "http://elasticsearch:9200"
KIBANA_LINK = "http://127.0.0.1:5601/"
DOWNLOAD_STATUS_LINK = "http://127.0.0.1:8080/"
JUPYTER_LINK = "http://127.0.0.1:8888/"
STAGE_COLORS = {
    "done": "#2e7d32",
    "running": "#f9a825",
    "failed": "#c62828",
    "not running": "#c62828",
    "waiting": "#9e9e9e",
}
Disposition = Literal["inline", "attachment"]
SEND_AS_IS = [
    "application/octet-stream",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "message/rfc822",
    "text/csv",
    "text/plain",
    "text/xml",
]
DONT_CONVERT_MIME = [
    "application/x-matlab-data",
    "application/quickbooks",
    "application/encrypted",
    "application/x-wine-extension-ini",
    "inode/x-empty",
    "application/x-ole-storage",
    "application/x-fpt",
    "application/x-ms-shortcut",
]


def pipeline_status(status_dir: str = STATUS_DIR) -> dict[str, str]:
    """Same three-stage status bin/deis.py's marker_status() computes for
    `deis status` - filesystem markers only, no docker socket. Deliberately
    does not attempt progress.py's "Setup"/"Adding URLs" checks: those need
    `docker volume ls`/`docker ps`, which would mean mounting the Docker
    socket into this always-on, browser-facing container - turning any
    future bug here (this file has already had two CodeQL path-traversal
    findings) into a full host compromise instead of just leak-data
    exposure. Not worth it for two status lines an operator can already get
    from `deis status` on the host.
    """
    status_path = Path(status_dir)
    status = {}

    if (status_path / "download_failed").exists():
        status["download"] = "failed"
    elif (status_path / "downloaded").exists():
        status["download"] = "done"
    elif (status_path / "running").exists():
        status["download"] = "running"
    else:
        status["download"] = "not running"

    if (status_path / "extract_done").exists():
        status["extract"] = "done"
    elif (status_path / "unpack").exists():
        status["extract"] = "running"
    else:
        status["extract"] = "waiting"

    if (status_path / "ingest_done").exists():
        status["ingest"] = "done"
    elif (status_path / "extract_done").exists():
        status["ingest"] = "running"
    else:
        status["ingest"] = "waiting"

    return status


def count_files(directory: str, exclude: frozenset[str] = frozenset()) -> int:
    """Same funnel count bin/deis.py's cmd_status computes on the host,
    including its exclude param - files/ holds a tracked .gitignore (see
    unpack/start.sh's own '! -name .*' exclusion of it) that isn't leak data
    and shouldn't inflate the count.
    """
    path = Path(directory)
    if not path.is_dir():
        return 0
    return sum(1 for f in path.rglob("*") if f.is_file() and f.name not in exclude)


def count_lines(path: str) -> int:
    """Non-blank line count of a status/*.txt file (still_encrypted.txt etc),
    or 0 if unpack hasn't written it yet.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return 0
    return sum(1 for line in file_path.read_text(encoding="utf-8").splitlines() if line.strip())


def elastic_document_count() -> int | None:
    """Read-only /_count against the leak index, or None if Elasticsearch
    isn't reachable or ELASTIC_PASSWORD isn't set. Never calls /_refresh
    first (unlike ingest.py's own version of this) - this page can
    auto-reload every few seconds, and a stale-by-a-few-seconds count is a
    fair trade against forcing a refresh on the cluster every page load.
    """
    password = os.environ.get("ELASTIC_PASSWORD")
    if not password:
        return None
    try:
        response = requests.get(f"{ELASTIC_URL}/{ELASTIC_INDEX}/_count", auth=("elastic", password), timeout=10)
        if response.status_code != 200:
            return None
        return response.json().get("count")
    except requests.exceptions.RequestException:
        return None


def latest_run_summary() -> dict | None:
    """Same query bin/deis.py's latest_run_summary() runs from the host, for
    the per-run breakdown ingest.py writes to ELASTIC_RUNS_INDEX at the end
    of every run (see ingest.py's index_run_summary()).
    """
    password = os.environ.get("ELASTIC_PASSWORD")
    if not password:
        return None
    try:
        response = requests.get(
            f"{ELASTIC_URL}/{ELASTIC_RUNS_INDEX}/_search?size=1&sort=@timestamp:desc",
            auth=("elastic", password),
            timeout=10,
        )
        if response.status_code != 200:
            return None
        hits = response.json().get("hits", {}).get("hits", [])
        return hits[0]["_source"] if hits else None
    except requests.exceptions.RequestException:
        return None


def render_index_html() -> str:
    """The landing page: pipeline stage status, funnel counts, and links to
    the other containers an end user (not an operator) actually needs -
    Kibana, JupyterLab, and the download status page. The in-browser
    equivalent of bin/progress.py, extended with what `deis status`/
    `deis report` already compute on the host (see pipeline_status()'s
    docstring for why the two docker-dependent progress.py checks aren't
    reproduced here).
    """
    status = pipeline_status()
    downloaded = count_files(FILES_DIR, exclude=frozenset({".gitignore"}))
    extracted = count_files("/extracted/files")
    unique = count_files(SYMLINKS_DIR)
    still_encrypted = count_lines(f"{STATUS_DIR}/still_encrypted.txt")
    still_corrupt = count_lines(f"{STATUS_DIR}/still_corrupt.txt")
    still_unsafe = count_lines(f"{STATUS_DIR}/still_unsafe.txt")
    doc_count = elastic_document_count()
    run = latest_run_summary()

    def stage_row(label: str, key: str) -> str:
        state = status[key]
        color = STAGE_COLORS.get(state, "#9e9e9e")
        return (
            f"<tr><td>{html.escape(label)}</td>"
            f'<td style="color:{color}; font-weight:600;">{html.escape(state.upper())}</td></tr>'
        )

    doc_count_display = str(doc_count) if doc_count is not None else "could not be read"

    still_rows = ""
    for label, count in (
        ("Still encrypted", still_encrypted),
        ("Still corrupt", still_corrupt),
        ("Rejected as unsafe", still_unsafe),
    ):
        if count:
            still_rows += f'<tr><td>{html.escape(label)}</td><td style="color:#c62828;">{count}</td></tr>'

    run_section = ""
    if run:
        run_rows = "".join(
            f"<tr><td>{html.escape(str(key))}</td><td>{html.escape(str(run.get(key, '?')))}</td></tr>"
            for key in (
                "@timestamp",
                "indexed_this_run",
                "already_indexed",
                "failed",
            )
        )
        run_section = f"""
<h2>Latest ingest run</h2>
<table>{run_rows}</table>
"""

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>DEIS</title>
<style>
body {{ font-family: sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem; }}
h1 {{ font-size: 1.4rem; }}
h2 {{ font-size: 1.05rem; margin-top: 2rem; }}
table {{ border-collapse: collapse; width: 100%; }}
td {{ padding: 0.3rem 0.5rem; border-bottom: 1px solid #ddd; }}
td:first-child {{ color: #555; }}
ul {{ padding-left: 1.2rem; }}
.note {{ color: #777; font-size: 0.85rem; margin-top: 2rem; }}
</style>
</head>
<body>
<h1>DEIS</h1>

<h2>Pipeline status</h2>
<table>
{stage_row("Download", "download")}
{stage_row("Extract", "extract")}
{stage_row("Ingest", "ingest")}
</table>

<h2>Funnel</h2>
<table>
<tr><td>Files in files/</td><td>{downloaded}</td></tr>
<tr><td>Files under extracted/files</td><td>{extracted}</td></tr>
<tr><td>Unique sha256</td><td>{unique}</td></tr>
<tr><td>Documents in Elasticsearch</td><td>{doc_count_display}</td></tr>
{still_rows}
</table>
{run_section}
<h2>Search and review</h2>
<ul>
<li><a href="{KIBANA_LINK}" target="_blank">Kibana</a> - search and dashboards</li>
<li><a href="{JUPYTER_LINK}" target="_blank">JupyterLab</a> - notebook (token is in .env)</li>
<li><a href="{DOWNLOAD_STATUS_LINK}" target="_blank">Download status</a> - only reachable while the
download stage is running</li>
</ul>

<p class="note">Refreshes every 30 seconds. Counts may lag the pipeline by up to that long.</p>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    """Landing page - see render_index_html()."""
    return render_index_html()


def validate_sha256_and_get_symlink_path(sha256: str) -> str:
    """Validate SHA256 hash and safely construct symlink path.

    Applies multi-layer validation to safely use user input in file paths:
    1. Regex validation: Ensures input is exactly 64 lowercase hex characters
    2. Filename extraction: Uses os.path.basename() to prevent path traversal
    3. Path normalization: os.path.normpath() resolves any .. or . sequences
    4. Boundary verification: startswith() check ensures result is within SYMLINKS_DIR

    This pattern matches CodeQL's recommended approach for safe path handling.

    Args:
        sha256: User-provided SHA256 hash string from URL parameter

    Raises:
        HTTPException: If validation fails

    Returns:
        Path string verified to be within SYMLINKS_DIR and safe for file operations
    """
    # Step 1: Strict regex validation - only allow 64 lowercase hex characters.
    # fullmatch(), not match() with a ^...$ anchor: re.match's $ also matches
    # just before a single trailing newline, so "<64 hex chars>\n" would
    # otherwise pass this check (found by tests/test_web_app.py). basename()
    # and normpath() below don't treat \n as a separator and don't strip it,
    # so this never escaped SYMLINKS_DIR - it just fails harmlessly further
    # down (no real file has a newline in its name) - but the whole point of
    # this line is exact validation, so anything that isn't should be
    # rejected here, not depend on later checks.
    if not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise HTTPException(status_code=400, detail="Invalid SHA256 format")

    # Step 2: Extract only the validated filename component using os.path.basename()
    # This prevents path traversal even if previous validation was bypassed
    safe_filename = os.path.basename(sha256)

    # Step 3: Construct normalized path from constant base and validated filename only
    # os.path.normpath() resolves .. and . sequences to absolute path
    base_path_str = os.path.normpath(SYMLINKS_DIR)
    symlink_path_str = os.path.normpath(os.path.join(base_path_str, safe_filename))

    # Step 4: Verify the constructed path is within base directory
    # Uses startswith() check as recommended by CodeQL for path injection prevention
    # This ensures no symlink or normalization can escape SYMLINKS_DIR
    if not symlink_path_str.startswith(base_path_str + os.sep) and symlink_path_str != base_path_str:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Return the normalized, validated path - safe for all file operations
    return symlink_path_str


def resolve_and_verify_target_file(symlink_path_str: str) -> str:
    """Resolve symlink and verify target exists.

    Takes a pre-validated symlink path from validate_sha256_and_get_symlink_path()
    and safely resolves it to the target file. Additional verification ensures
    the symlink and target file both exist.

    The symlink_path_str parameter is already validated to:
    - Contain only valid hex characters (regex)
    - Exist within SYMLINKS_DIR (boundary check)
    - Have no path traversal sequences (normalization)

    This function performs additional checks before using the path:
    - Verifies it points to an actual symlink (not a regular file)
    - Resolves the symlink using os.path.realpath()
    - Confirms the resolved target still lands under EXTRACTED_ROOT
    - Confirms the target file exists and is accessible

    Args:
        symlink_path_str: Pre-validated path from validate_sha256_and_get_symlink_path()

    Returns:
        str: Resolved path to the target file, verified to exist

    Raises:
        HTTPException: If path is outside SYMLINKS_DIR, isn't a symlink,
            resolves outside EXTRACTED_ROOT, or target doesn't exist
    """
    # Defense in depth: re-verify the boundary check from
    # validate_sha256_and_get_symlink_path() in this function's own scope,
    # so it stays safe to call on its own and every path operation below
    # is preceded by a normalize+startswith barrier in the same function.
    symlink_path_str = os.path.normpath(symlink_path_str)
    symlinks_dir_str = os.path.normpath(SYMLINKS_DIR)
    if not symlink_path_str.startswith(symlinks_dir_str + os.sep):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Verify the symlink exists and is actually a symlink (not regular file)
    if not os.path.islink(symlink_path_str):
        raise HTTPException(status_code=404, detail="File not found")

    # Resolve the symlink to get the actual file. The symlinks under
    # SYMLINKS_DIR legitimately point elsewhere under EXTRACTED_ROOT (see
    # web/startup.sh and ingest/ingest.py), so realpath() is expected to
    # leave SYMLINKS_DIR - re-verify the boundary against EXTRACTED_ROOT
    # instead so a symlink can't resolve outside the extracted tree.
    target_file_str = os.path.normpath(os.path.realpath(symlink_path_str))
    extracted_root_str = os.path.normpath(EXTRACTED_ROOT)
    if not target_file_str.startswith(extracted_root_str + os.sep):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Verify the resolved file exists and is accessible
    # This ensures the symlink points to a valid file
    if not os.path.exists(target_file_str):
        raise HTTPException(status_code=404, detail="Target file not found")

    # Return the verified, resolved path - safe for file operations
    # Path has been through multi-step validation and exists on filesystem
    return target_file_str


def convert_to_pdf(file_path: str) -> bytes:
    """Send the file to Gotenberg for conversion to PDF."""
    with open(file_path, "rb") as f:
        response = requests.post(GOTENBERG_URL, files={"file": f}, timeout=60)
    response.raise_for_status()
    return response.content


def convert_html_to_pdf(file_path: str) -> bytes:
    """Send the file to Gotenberg for conversion to PDF."""
    with tempfile.NamedTemporaryFile(suffix=".html") as tmp:
        tmp.write(Path(file_path).read_bytes())
        tmp.flush()
        with open(tmp.name, "rb") as f:
            # Gotenberg's chromium module requires the uploaded HTML file to be
            # named index.html; the tuple form lets us send that name in the
            # multipart request independent of the temp file's real path.
            response = requests.post(GOTENBERG_HTML_URL, files={"file": ("index.html", f, "text/html")}, timeout=60)
    response.raise_for_status()
    return response.content


def pdf_response(pdf_content: bytes, filename: str, disposition: Disposition) -> FileResponse:
    """Write converted PDF bytes to a temp file and stream it back.

    Using a per-request temp file (instead of a fixed "index.pdf" in the cwd)
    avoids both the permission error of writing into the app's non-writable
    working directory and concurrent requests overwriting each other's output.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_content)
    return FileResponse(
        tmp.name,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type=disposition,
        background=BackgroundTask(os.remove, tmp.name),
    )


@app.get("/view/{sha256}", response_class=HTMLResponse)
async def view_file(sha256: str):
    """Landing page offering a preview-vs-download choice for a document.

    Kibana's URL field formatter can only point a field at one fixed URL
    template, so this is what the sha256 column in Discover/dashboards
    actually links to now (instead of straight to /file/, which only ever
    downloaded) - one click gets you a choice instead of a forced download.
    """
    symlink_path_str = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path_str)
    # Both escaped before embedding, even though validate_sha256_and_get_symlink_path()
    # already restricts sha256 to 64 lowercase hex characters: staying safe here
    # should not depend on a guard living in a different function.
    safe_sha256 = html.escape(sha256)
    display_name = html.escape(Path(target_file_str).name)

    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>{display_name}</title></head>
<body style="font-family: sans-serif; max-width: 60rem; margin: 3rem auto; padding: 0 1rem;">
<h1 style="font-size: 1.1rem; word-break: break-word;">{display_name}</h1>
<p>
<a href="/convert/{safe_sha256}" target="_blank" style="margin-right: 1.5rem;">Full-screen preview</a>
<a href="/file/{safe_sha256}?disposition=attachment">Download original</a>
</p>
<iframe src="/convert/{safe_sha256}" title="Document preview"
    style="width: 100%; height: 80vh; border: 1px solid #ccc; margin-top: 1rem;"></iframe>
</body>
</html>
"""


@app.get("/file/{sha256}")
async def get_file(sha256: str, disposition: Disposition = "attachment"):
    """Retrieve a file by its SHA256 hash.

    The sha256 parameter undergoes multi-layer validation before any file operations:
    1. validate_sha256_and_get_symlink_path(): Regex + path normalization + boundary checks
    2. resolve_and_verify_target_file(): Symlink verification + realpath resolution + existence check

    The resulting target_file_str is safe for file operations despite originating from user input.

    disposition picks whether the browser downloads the file (the default,
    matching this endpoint's prior behavior) or displays it inline.
    """
    symlink_path_str = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path_str)

    # Safe: target_file_str comes from validated symlink path and os.path.realpath()
    # Path has passed: regex validation, basename extraction, normalization, boundary checks,
    # symlink verification, and file existence verification
    mime_type = magic.from_file(target_file_str, mime=True)
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default type if not known

    # Get extension from the target file
    target_path = Path(target_file_str)
    extension = target_path.suffix.lower()

    # Use filename from validated symlink path
    symlink_filename = os.path.basename(symlink_path_str)
    validated_filename = symlink_filename + extension

    return FileResponse(
        target_file_str, media_type=mime_type, filename=validated_filename, content_disposition_type=disposition
    )


@app.get("/convert/{sha256}")
async def convert_file(sha256: str, disposition: Disposition = "inline"):
    """Convert a file to PDF by its SHA256 hash.

    The sha256 parameter undergoes multi-layer validation before any file operations:
    1. validate_sha256_and_get_symlink_path(): Regex + path normalization + boundary checks
    2. resolve_and_verify_target_file(): Symlink verification + realpath resolution + existence check

    The resulting target_file_str is safe for file operations despite originating from user input.

    disposition picks whether the browser displays the (converted) file inline
    (the default, matching this endpoint's prior use as a preview iframe
    source) or downloads it.
    """
    symlink_path_str = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path_str)

    # Safe: target_file_str comes from validated symlink path and os.path.realpath()
    # Path has passed: regex validation, basename extraction, normalization, boundary checks,
    # symlink verification, and file existence verification
    mime_type = magic.from_file(target_file_str, mime=True)
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default type if not known

    # Get extension from the target file
    target_path = Path(target_file_str)
    extension = target_path.suffix.lower()

    # Use filename from validated symlink path
    symlink_filename = os.path.basename(symlink_path_str)
    validated_filename = symlink_filename + extension
    pdf_filename = f"{symlink_filename}.pdf"

    if mime_type in SEND_AS_IS:
        return FileResponse(
            target_file_str, media_type=mime_type, filename=validated_filename, content_disposition_type=disposition
        )

    if mime_type == "text/html":
        try:
            pdf_content = convert_html_to_pdf(target_file_str)
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e
        return pdf_response(pdf_content, pdf_filename, disposition)

    if mime_type != "application/pdf" and mime_type not in DONT_CONVERT_MIME:
        try:
            pdf_content = convert_to_pdf(target_file_str)
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e
        return pdf_response(pdf_content, pdf_filename, disposition)

    return FileResponse(
        target_file_str, media_type=mime_type, filename=validated_filename, content_disposition_type=disposition
    )
