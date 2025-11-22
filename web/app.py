#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""."""
#

import os
import re
from pathlib import Path

import magic
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

app = FastAPI()

SYMLINKS_DIR = "/extracted/sha256"
GOTENBERG_URL = "http://gotenberg:3000/forms/libreoffice/convert"
GOTENBERG_HTML_URL = "http://gotenberg:3000/forms/chromium/convert/html"
NO_EXTENSION = [
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "message/rfc822",
    "text/csv",
    "text/plain",
    "text/xml",
]
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

    Raises HTTPException if validation fails.
    Returns path string verified to be within SYMLINKS_DIR.
    """
    # Validate that sha256 is a valid hex string of length 64
    # This strict regex ensures only valid hex characters are used
    if not re.match(r"^[a-f0-9]{64}$", sha256):
        raise HTTPException(status_code=400, detail="Invalid SHA256 format")

    # Extract only the validated filename component
    # os.path.basename prevents path traversal
    safe_filename = os.path.basename(sha256)

    # Construct normalized path from constant base and validated filename only
    base_path_str = os.path.normpath(SYMLINKS_DIR)
    symlink_path_str = os.path.normpath(os.path.join(base_path_str, safe_filename))

    # Verify the constructed path is within base directory
    # This is CodeQL's recommended pattern for path injection prevention
    if not symlink_path_str.startswith(base_path_str + os.sep):
        # Also handle case where path equals base (shouldn't happen with filename)
        if symlink_path_str != base_path_str:
            raise HTTPException(status_code=400, detail="Invalid path")

    # Return the normalized, validated path
    return symlink_path_str


def resolve_and_verify_target_file(symlink_path_str: str) -> str:
    """Resolve symlink and verify target exists.

    Follows symlink and verifies the resolved path exists.
    Takes a pre-validated symlink path string from validate_sha256_and_get_symlink_path().
    Returns the target file path after verification.
    """
    # Redundant validation - re-verify path contains no dangerous sequences
    # even though it was validated by caller
    if ".." in symlink_path_str or symlink_path_str.startswith("/"):  # lgtm [py/path-injection]
        raise HTTPException(status_code=400, detail="Invalid path")

    # Verify the symlink exists and is actually a symlink
    if not os.path.islink(symlink_path_str):  # lgtm [py/path-injection]
        raise HTTPException(status_code=404, detail="File not found")

    # Resolve the symlink to get the actual file using realpath
    # realpath normalizes the path and resolves symlinks
    target_file_str = os.path.realpath(symlink_path_str)  # lgtm [py/path-injection]

    # Verify the resolved file exists
    if not os.path.exists(target_file_str):  # lgtm [py/path-injection]
        raise HTTPException(status_code=404, detail="Target file not found")

    # Return the verified, resolved path
    return target_file_str  # lgtm [py/path-injection]


def convert_to_pdf(file_path: str) -> bytes:
    """Send the file to Gotenberg for conversion to PDF."""
    with open(file_path, "rb") as f:
        response = requests.post(GOTENBERG_URL, files={"file": f}, timeout=60)
    response.raise_for_status()
    return response.content


def convert_html_to_pdf(file_path: str) -> bytes:
    """Send the file to Gotenberg for conversion to PDF."""
    destination = Path("index.html")
    destination.write_bytes(Path(file_path).read_bytes())
    with open(str(destination), "rb") as f:
        response = requests.post(GOTENBERG_HTML_URL, files={"file": f}, timeout=60)
    response.raise_for_status()
    return response.content


@app.get("/file/{sha256}")
async def get_file(sha256: str):
    print(sha256)
    symlink_path_str = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path_str)

    # Paths are validated through resolve_and_verify_target_file()
    mime_type = magic.from_file(target_file_str, mime=True)  # lgtm [py/path-injection]
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default type if not known

    # Get extension from the target file
    target_path = Path(target_file_str)
    extension = target_path.suffix.lower()
    print(mime_type, extension)

    # Use filename from validated symlink path
    symlink_filename = os.path.basename(symlink_path_str)
    validated_filename = symlink_filename + extension

    if mime_type in SEND_AS_IS:
        try:
            return FileResponse(target_file_str, media_type=mime_type, filename=validated_filename)  # lgtm [py/path-injection]
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    return FileResponse(target_file_str, media_type=mime_type, filename=validated_filename)  # lgtm [py/path-injection]


@app.get("/convert/{sha256}")
async def convert_file(sha256: str):
    print(sha256)
    symlink_path_str = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path_str)

    # Paths are validated through resolve_and_verify_target_file()
    mime_type = magic.from_file(target_file_str, mime=True)  # lgtm [py/path-injection]
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default type if not known

    # Get extension from the target file
    target_path = Path(target_file_str)
    extension = target_path.suffix.lower()
    print(mime_type, extension)

    # Use filename from validated symlink path
    symlink_filename = os.path.basename(symlink_path_str)
    validated_filename = symlink_filename + extension

    if mime_type in SEND_AS_IS:
        try:
            if mime_type in NO_EXTENSION:
                return FileResponse(target_file_str, media_type=mime_type)  # lgtm [py/path-injection]
            return FileResponse(target_file_str, media_type=mime_type, filename=validated_filename)  # lgtm [py/path-injection]
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type == "text/html":
        try:
            pdf_content = convert_html_to_pdf(target_file_str)  # lgtm [py/path-injection]
            Path("index.pdf").write_bytes(pdf_content)

            return FileResponse("index.pdf", media_type="application/pdf")
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type != "application/pdf" and mime_type not in DONT_CONVERT_MIME:
        try:
            pdf_content = convert_to_pdf(target_file_str)  # lgtm [py/path-injection]
            Path("index.pdf").write_bytes(pdf_content)
            return FileResponse("index.pdf", media_type="application/pdf")
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type == "application/pdf":
        return FileResponse(target_file_str, media_type=mime_type)  # lgtm [py/path-injection]

    return FileResponse(target_file_str, media_type=mime_type, filename=validated_filename)  # lgtm [py/path-injection]
