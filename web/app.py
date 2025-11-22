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


def validate_sha256_and_get_symlink_path(sha256: str) -> Path:
    """Validate SHA256 hash and safely construct symlink path.

    Raises HTTPException if validation fails.
    Returns normalized absolute Path object verified to be within SYMLINKS_DIR.
    """
    # Validate that sha256 is a valid hex string of length 64
    if not re.match(r"^[a-f0-9]{64}$", sha256):
        raise HTTPException(status_code=400, detail="Invalid SHA256 format")

    # Use only the validated filename component to prevent path injection
    safe_filename = os.path.basename(sha256)

    # Construct the path using pathlib.Path with the validated filename
    base_path = Path(SYMLINKS_DIR).resolve()
    symlink_path = base_path / safe_filename

    # Verify the path is within SYMLINKS_DIR (redundant but explicit for static analysis)
    try:
        symlink_path.relative_to(base_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Return the normalized, safe Path object
    return symlink_path


def resolve_and_verify_target_file(symlink_path: Path) -> str:
    """Resolve symlink and verify target exists.

    Follows symlink and verifies the resolved path exists.
    Returns the path as a string after verification.
    """
    if not symlink_path.exists() or not symlink_path.is_symlink():
        raise HTTPException(status_code=404, detail="File not found")

    # Resolve the symlink to get the actual file
    # Use realpath which is more explicit about symlink resolution
    target_file_str = os.path.realpath(str(symlink_path))

    # Verify the resolved file exists
    if not os.path.exists(target_file_str):
        raise HTTPException(status_code=404, detail="Target file not found")

    # Return the verified path
    return target_file_str


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
    symlink_path = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path)

    mime_type = magic.from_file(target_file_str, mime=True)
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default type if not known

    # Get extension from the target file
    target_path = Path(target_file_str)
    extension = target_path.suffix.lower()
    print(mime_type, extension)

    # Use validated filename component from path instead of user input
    validated_filename = symlink_path.name + extension

    if mime_type in SEND_AS_IS:
        try:
            return FileResponse(target_file_str, media_type=mime_type, filename=validated_filename)
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    return FileResponse(target_file_str, media_type=mime_type, filename=validated_filename)


@app.get("/convert/{sha256}")
async def convert_file(sha256: str):
    print(sha256)
    symlink_path = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path)

    mime_type = magic.from_file(target_file_str, mime=True)
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default type if not known

    # Get extension from the target file
    target_path = Path(target_file_str)
    extension = target_path.suffix.lower()
    print(mime_type, extension)

    # Use validated filename component from path instead of user input
    validated_filename = symlink_path.name + extension

    if mime_type in SEND_AS_IS:
        try:
            if mime_type in NO_EXTENSION:
                return FileResponse(target_file_str, media_type=mime_type)
            return FileResponse(target_file_str, media_type=mime_type, filename=validated_filename)
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type == "text/html":
        try:
            pdf_content = convert_html_to_pdf(target_file_str)
            Path("index.pdf").write_bytes(pdf_content)

            return FileResponse("index.pdf", media_type="application/pdf")
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type != "application/pdf" and mime_type not in DONT_CONVERT_MIME:
        try:
            pdf_content = convert_to_pdf(target_file_str)
            Path("index.pdf").write_bytes(pdf_content)
            return FileResponse("index.pdf", media_type="application/pdf")
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type == "application/pdf":
        return FileResponse(target_file_str, media_type=mime_type)

    return FileResponse(target_file_str, media_type=mime_type, filename=validated_filename)
