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

    # Verify the path is within SYMLINKS_DIR
    try:
        symlink_path.relative_to(base_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid path")

    # Return the resolved, safe Path object
    return symlink_path


def convert_to_pdf(file_path: str):
    """Send the file to Gotenberg for conversion to PDF."""
    with open(file_path, "rb") as f:
        response = requests.post(GOTENBERG_URL, files={"file": f}, timeout=60)
    response.raise_for_status()
    return response.content


def convert_html_to_pdf(file_path: str):
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

    if symlink_path.exists() and symlink_path.is_symlink():
        target_file = symlink_path.resolve()
    else:
        raise HTTPException(status_code=404, detail="File not found")

    mime_type = magic.from_file(str(target_file), mime=True)
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default type if not known
    extension = target_file.suffix.lower()
    print(mime_type, extension)

    if mime_type in SEND_AS_IS:
        try:
            return FileResponse(str(target_file), media_type=mime_type, filename=sha256 + extension)
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    return FileResponse(str(target_file), media_type=mime_type, filename=sha256 + extension)


@app.get("/convert/{sha256}")
async def convert_file(sha256: str):
    print(sha256)
    symlink_path = validate_sha256_and_get_symlink_path(sha256)

    if symlink_path.exists() and symlink_path.is_symlink():
        target_file = symlink_path.resolve()
    else:
        raise HTTPException(status_code=404, detail="File not found")

    mime_type = magic.from_file(str(target_file), mime=True)
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default type if not known
    extension = target_file.suffix.lower()
    print(mime_type, extension)

    target_file_str = str(target_file)

    if mime_type in SEND_AS_IS:
        try:
            if mime_type in NO_EXTENSION:
                return FileResponse(target_file_str, media_type=mime_type)
            return FileResponse(target_file_str, media_type=mime_type, filename=sha256 + extension)
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

    return FileResponse(target_file_str, media_type=mime_type, filename=sha256 + extension)
