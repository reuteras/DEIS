"""Tests for unpack/mail.py (item 50): mbox splitting and attachment
extraction, including the hostile-filename handling."""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_mail", REPO_ROOT / "unpack" / "mail.py")
mail = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mail
spec.loader.exec_module(mail)


def _message(subject: str, attachments: list[tuple[str | None, bytes]] = (), forwarded: bytes | None = None) -> bytes:
    parts = [b"--B\nContent-Type: text/plain\n\nbody of " + subject.encode() + b"\n"]
    for name, payload in attachments:
        header = b"Content-Type: application/octet-stream\n"
        header += b"Content-Disposition: attachment" + (b'; filename="' + name.encode() + b'"' if name else b"") + b"\n"
        parts.append(
            b"--B\n"
            + header
            + b"Content-Transfer-Encoding: base64\n\n"
            + __import__("base64").b64encode(payload)
            + b"\n"
        )
    if forwarded is not None:
        parts.append(b"--B\nContent-Type: message/rfc822\nContent-Disposition: attachment\n\n" + forwarded + b"\n")
    head = (
        b"From: a@example.com\nTo: b@example.com\nSubject: "
        + subject.encode()
        + b"\nDate: Mon, 1 Jan 2024 10:00:00 +0000\n"
        b'MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="B"\n\n'
    )
    return head + b"".join(parts) + b"--B--\n"


class TestSafeFilename:
    def test_plain_name_kept(self):
        assert mail.safe_filename("report.pdf", 1) == "report.pdf"

    def test_path_traversal_reduced_to_basename(self):
        assert mail.safe_filename("../../etc/passwd", 1) == "passwd"
        assert mail.safe_filename("..\\..\\windows\\evil.exe", 1) == "evil.exe"

    def test_control_characters_replaced(self):
        assert mail.safe_filename("a\x00b\nc.txt", 1) == "a_b_c.txt"

    def test_empty_or_dots_get_generated_name(self):
        assert mail.safe_filename(None, 3) == "attachment-3.bin"
        assert mail.safe_filename("..", 4) == "attachment-4.bin"
        assert mail.safe_filename("   ", 5) == "attachment-5.bin"

    def test_encoded_word_name_is_decoded(self):
        assert mail.safe_filename("=?utf-8?q?l=C3=B6n.pdf?=", 1) == "lön.pdf"


class TestUniquePath:
    def test_collision_gets_dup_suffix(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        assert mail.unique_path(tmp_path, "a.txt") == tmp_path / "a-dup2.txt"
        (tmp_path / "a-dup2.txt").write_text("x")
        assert mail.unique_path(tmp_path, "a.txt") == tmp_path / "a-dup3.txt"


class TestSplitMbox:
    def test_one_eml_per_message(self, tmp_path):
        box = tmp_path / "inbox.mbox"
        box.write_bytes(
            b"From a@example.com Mon Jan  1 10:00:00 2024\n" + _message("one") + b"\n"
            b"From a@example.com Mon Jan  1 11:00:00 2024\n" + _message("two") + b"\n"
        )
        out = tmp_path / "inbox.mbox.messages"
        assert mail.split_mbox(box, out) == 2
        files = sorted(p.name for p in out.iterdir())
        assert files == ["000001.eml", "000002.eml"]
        assert b"Subject: two" in (out / "000002.eml").read_bytes()

    def test_cli_prints_count(self, tmp_path, capsys):
        box = tmp_path / "x.mbox"
        box.write_bytes(b"From a@b.c Mon Jan  1 10:00:00 2024\n" + _message("only") + b"\n")
        assert mail.main(["mail.py", "split-mbox", str(box), str(tmp_path / "out")]) == 0
        assert capsys.readouterr().out.strip() == "1"


class TestExtractAttachments:
    def test_attachments_written_with_safe_names(self, tmp_path):
        source = tmp_path / "m.eml"
        source.write_bytes(_message("x", [("report.pdf", b"%PDF"), ("../../evil.exe", b"MZ"), (None, b"raw")]))
        out = tmp_path / "m.eml.attachments"
        assert mail.extract_attachments(source, out) == 3
        assert sorted(p.name for p in out.iterdir()) == ["attachment-3.bin", "evil.exe", "report.pdf"]
        assert (out / "report.pdf").read_bytes() == b"%PDF"

    def test_duplicate_names_do_not_overwrite(self, tmp_path):
        source = tmp_path / "m.eml"
        source.write_bytes(_message("x", [("a.txt", b"first"), ("a.txt", b"second")]))
        out = tmp_path / "out"
        assert mail.extract_attachments(source, out) == 2
        assert (out / "a.txt").read_bytes() == b"first"
        assert (out / "a-dup2.txt").read_bytes() == b"second"

    def test_forwarded_message_becomes_an_eml(self, tmp_path):
        inner = _message("inner", [("inner.txt", b"deep")])
        source = tmp_path / "m.eml"
        source.write_bytes(_message("outer", forwarded=inner))
        out = tmp_path / "out"
        count = mail.extract_attachments(source, out)
        names = sorted(p.name for p in out.iterdir())
        assert "attachment-1.eml" in names
        assert "inner.txt" in names  # walk() descends into the forwarded message too
        assert count == 2
        assert b"Subject: inner" in (out / "attachment-1.eml").read_bytes()

    def test_no_attachments_writes_nothing(self, tmp_path):
        source = tmp_path / "m.eml"
        source.write_bytes(_message("plain"))
        out = tmp_path / "out"
        assert mail.extract_attachments(source, out) == 0
        assert not out.exists()

    def test_cli_unreadable_input_is_an_error(self, tmp_path, capsys):
        assert mail.main(["mail.py", "attachments", str(tmp_path / "missing"), str(tmp_path / "o")]) == 1
        assert "failed" in capsys.readouterr().err
