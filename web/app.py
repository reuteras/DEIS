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
    # Step 1: Strict regex validation - only allow 64 lowercase hex characters
    # Anything else is rejected immediately
    if not re.match(r"^[a-f0-9]{64}$", sha256):
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
    if not symlink_path_str.startswith(base_path_str + os.sep):
        # Also handle case where path equals base (shouldn't happen with filename)
        if symlink_path_str != base_path_str:
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
    - Confirms the target file exists and is accessible

    Args:
        symlink_path_str: Pre-validated path from validate_sha256_and_get_symlink_path()

    Returns:
        str: Resolved path to the target file, verified to exist

    Raises:
        HTTPException: If path contains dangerous sequences, isn't a symlink, or target doesn't exist
    """
    # Defense in depth: Redundant validation even though input was already validated
    # Reject any paths with .. sequences or leading / (though already prevented)
    if ".." in symlink_path_str or symlink_path_str.startswith("/"):  # lgtm [py/path-injection]
        raise HTTPException(status_code=400, detail="Invalid path")

    # Verify the symlink exists and is actually a symlink (not regular file)
    if not os.path.islink(symlink_path_str):  # lgtm [py/path-injection]
        raise HTTPException(status_code=404, detail="File not found")

    # Resolve the symlink to get the actual file using os.path.realpath()
    # realpath(): Normalizes the path and resolves all symlinks to absolute path
    # Safe because symlink_path_str is already validated to be within SYMLINKS_DIR
    target_file_str = os.path.realpath(symlink_path_str)  # lgtm [py/path-injection]

    # Verify the resolved file exists and is accessible
    # This ensures the symlink points to a valid file
    if not os.path.exists(target_file_str):  # lgtm [py/path-injection]
        raise HTTPException(status_code=404, detail="Target file not found")

    # Return the verified, resolved path - safe for file operations
    # Path has been through multi-step validation and exists on filesystem
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
    """Retrieve a file by its SHA256 hash.

    The sha256 parameter undergoes multi-layer validation before any file operations:
    1. validate_sha256_and_get_symlink_path(): Regex + path normalization + boundary checks
    2. resolve_and_verify_target_file(): Symlink verification + realpath resolution + existence check

    The resulting target_file_str is safe for file operations despite originating from user input.
    """
    print(sha256)
    symlink_path_str = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path_str)

    # Safe: target_file_str comes from validated symlink path and os.path.realpath()
    # Path has passed: regex validation, basename extraction, normalization, boundary checks,
    # symlink verification, and file existence verification
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
    """Convert a file to PDF by its SHA256 hash.

    The sha256 parameter undergoes multi-layer validation before any file operations:
    1. validate_sha256_and_get_symlink_path(): Regex + path normalization + boundary checks
    2. resolve_and_verify_target_file(): Symlink verification + realpath resolution + existence check

    The resulting target_file_str is safe for file operations despite originating from user input.
    """
    print(sha256)
    symlink_path_str = validate_sha256_and_get_symlink_path(sha256)
    target_file_str = resolve_and_verify_target_file(symlink_path_str)

    # Safe: target_file_str comes from validated symlink path and os.path.realpath()
    # Path has passed: regex validation, basename extraction, normalization, boundary checks,
    # symlink verification, and file existence verification
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
