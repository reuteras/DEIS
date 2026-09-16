#!/usr/bin/env python3
"""Mail-format helpers for unpack/start.sh (docs/IMPROVEMENTS.md item 50),
stdlib only - the unpack image already has python3 for the OOXML check.

    mail.py split-mbox <mbox> <outdir>     one NNNNNN.eml per message
    mail.py attachments <message> <outdir> each attached part as a file

Both print the number of files written on stdout and exit non-zero only
when the input could not be read at all; a message the stdlib parser
cannot fully make sense of still yields whatever it could (the parser is
deliberately lenient - real mail archives are full of malformed
headers). Written files land in a sidecar directory next to the source,
which start.sh then queues for the next extraction round, so an attached
archive is opened and an attached spreadsheet gets its rows indexed,
exactly like a file that came out of a zip.

Attachment filenames come from the leak dump and are treated as hostile:
only the basename is ever used, path separators and control characters
are replaced, an empty or dot-only name gets a generated one, and a
collision inside one message gets a -dupN suffix rather than overwriting.
"""

import email
import email.header
import email.policy
import mailbox
import re
import sys
from pathlib import Path

_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f/\\]")
_MAX_NAME = 200


def _decode_header_value(value: str) -> str:
    pieces = []
    for piece, charset in email.header.decode_header(value):
        if isinstance(piece, bytes):
            try:
                pieces.append(piece.decode(charset or "utf-8", "replace"))
            except LookupError:
                pieces.append(piece.decode("utf-8", "replace"))
        else:
            pieces.append(piece)
    return "".join(pieces)


def safe_filename(name: str | None, index: int, fallback_ext: str = ".bin") -> str:
    """A filesystem-safe basename for an attachment: the part's own name
    when it has a usable one, else attachment-<index><ext>."""
    if name:
        name = _decode_header_value(name)
        name = Path(name.replace("\\", "/")).name  # basename only, either separator
        name = _UNSAFE_CHARS.sub("_", name).strip().strip(".")
        if name and name not in ("", ".", ".."):
            return name[:_MAX_NAME]
    return f"attachment-{index}{fallback_ext}"


def unique_path(directory: Path, name: str) -> Path:
    """directory/name, or directory/<stem>-dupN<suffix> if that exists -
    the same collision rule unpack/start.sh's dispose_of_original uses."""
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while (directory / f"{stem}-dup{n}{suffix}").exists():
        n += 1
    return directory / f"{stem}-dup{n}{suffix}"


def split_mbox(source: Path, outdir: Path) -> int:
    box = mailbox.mbox(str(source), create=False)
    outdir.mkdir(parents=True, exist_ok=True)
    written = 0
    for index, message in enumerate(box, start=1):
        try:
            data = message.as_bytes()
        except Exception as error:  # noqa: BLE001 - one broken message must not lose the rest
            print(f"mail.py split-mbox: skipped message {index} of {source}: {error!r}", file=sys.stderr)
            continue
        (outdir / f"{index:06d}.eml").write_bytes(data)
        written += 1
    return written


def extract_attachments(source: Path, outdir: Path) -> int:
    message = email.message_from_bytes(source.read_bytes(), policy=email.policy.compat32)
    written = 0
    index = 0
    for part in message.walk():
        if part.get_content_type() == "message/rfc822":
            # A forwarded/attached message: walk() descends into it too,
            # so its own attachments are still picked up below; this
            # writes the whole message out as an .eml of its own, the
            # extension ingest's own detection also honours.
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                index += 1
                if written == 0:
                    outdir.mkdir(parents=True, exist_ok=True)
                unique_path(outdir, safe_filename(part.get_filename(), index, ".eml")).write_bytes(inner[0].as_bytes())
                written += 1
            continue
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = str(part.get("Content-Disposition", "")).lower()
        if not filename and not disposition.startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        index += 1
        if written == 0:
            outdir.mkdir(parents=True, exist_ok=True)
        unique_path(outdir, safe_filename(filename, index)).write_bytes(payload)
        written += 1
    return written


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in ("split-mbox", "attachments"):
        print(__doc__, file=sys.stderr)
        return 2
    source, outdir = Path(argv[2]), Path(argv[3])
    try:
        if argv[1] == "split-mbox":
            count = split_mbox(source, outdir)
        else:
            count = extract_attachments(source, outdir)
    except Exception as error:  # noqa: BLE001 - report, never traceback into unpack.log
        print(f"mail.py {argv[1]} failed for {source}: {error!r}", file=sys.stderr)
        return 1
    print(count)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
