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
GOTENBERG_URL = "http://gotenberg:3000/forms/libreoffice/convert"
GOTENBERG_HTML_URL = "http://gotenberg:3000/forms/chromium/convert/html"
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
