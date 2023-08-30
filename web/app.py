#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""."""
#

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path, PurePosixPath
import os
import magic
import requests

app = FastAPI()

SYMLINKS_DIR = '/extracted/sha256'
GOTENBERG_URL = 'http://gotenberg:3000/forms/libreoffice/convert'
GOTENBERG_HTML_URL = 'http://gotenberg:3000/forms/chromium/convert/html'
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


def convert_to_pdf(file_path: str):
    """Send the file to Gotenberg for conversion to PDF."""
    with open(file_path, "rb") as f:
        response = requests.post(GOTENBERG_URL, files={"file": f}, timeout=60)
    response.raise_for_status()
    return response.content

def  convert_html_to_pdf(file_path: str):
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
    symlink_path = os.path.join(SYMLINKS_DIR, sha256)

    if os.path.exists(symlink_path) and os.path.islink(symlink_path):
        target_file = os.readlink(symlink_path)
    else:
        raise HTTPException(status_code=404, detail="File not found")

    mime_type = magic.from_file(target_file, mime=True)
    if mime_type is None:
        mime_type = 'application/octet-stream'  # Default type if not known
    extension = Path(str(PurePosixPath(symlink_path))).resolve().suffix.lower()
    print(mime_type, extension)

    if mime_type in SEND_AS_IS:
        try:
            return FileResponse(target_file, media_type=mime_type, filename=sha256+extension)
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    return FileResponse(target_file, media_type=mime_type, filename=sha256+extension)

@app.get("/convert/{sha256}")
async def convert_file(sha256: str):
    print(sha256)
    symlink_path = os.path.join(SYMLINKS_DIR, sha256)

    if os.path.exists(symlink_path) and os.path.islink(symlink_path):
        target_file = os.readlink(symlink_path)
    else:
        raise HTTPException(status_code=404, detail="File not found")

    mime_type = magic.from_file(target_file, mime=True)
    if mime_type is None:
        mime_type = 'application/octet-stream'  # Default type if not known
    extension = Path(str(PurePosixPath(symlink_path))).resolve().suffix.lower()
    print(mime_type, extension)

    if mime_type in SEND_AS_IS:
        try:
            if mime_type in NO_EXTENSION:
                return FileResponse(target_file, media_type=mime_type)
            return FileResponse(target_file, media_type=mime_type, filename=sha256+extension)
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type == "text/html":
        try:
            pdf_content = convert_html_to_pdf(target_file)
            Path("index.pdf").write_bytes(pdf_content)

            return FileResponse("index.pdf", media_type="application/pdf")
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type != "application/pdf" and mime_type not in DONT_CONVERT_MIME:
        try:
            pdf_content = convert_to_pdf(target_file)
            Path("index.pdf").write_bytes(pdf_content)
            return FileResponse("index.pdf", media_type="application/pdf")
        except requests.RequestException as e:
            raise HTTPException(status_code=500, detail=f"Conversion Error: {e}") from e

    if mime_type == "application/pdf":
        return FileResponse(target_file, media_type=mime_type)

    return FileResponse(target_file, media_type=mime_type, filename=sha256+extension)

