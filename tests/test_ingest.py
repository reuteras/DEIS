"""Tests for ingest/ingest.py's pure helpers: hashing, the sha256-marker
dedup logic item 1 and item 41 depend on being correct, and the
extraction_status classification item 34 depends on.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class TestGetFilehash:
    def test_matches_a_known_sha256(self, ingest_module, tmp_path):
        f = tmp_path / "sample.txt"
        f.write_text("hello world")
        # sha256("hello world")
        expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        assert ingest_module.get_filehash(f) == expected

    def test_streams_large_content_correctly(self, ingest_module, tmp_path):
        import hashlib

        f = tmp_path / "big.bin"
        data = bytes(range(256)) * 5000  # bigger than the 4KB read chunk
        f.write_bytes(data)
        assert ingest_module.get_filehash(f) == hashlib.sha256(data).hexdigest()

    def test_returns_none_for_a_missing_file(self, ingest_module, tmp_path):
        assert ingest_module.get_filehash(tmp_path / "does-not-exist") is None


class TestHashLinkMarker:
    def test_does_not_exist_initially(self, ingest_module):
        assert ingest_module.hash_link_exists("a" * 64) is False

    def test_create_then_exists(self, ingest_module, tmp_path):
        target = tmp_path / "extracted" / "files" / "doc.txt"
        target.parent.mkdir(parents=True)
        target.write_text("content")

        created = ingest_module.create_hash_link("a" * 64, target)

        assert created is True
        assert ingest_module.hash_link_exists("a" * 64) is True

    def test_create_is_a_no_op_the_second_time(self, ingest_module, tmp_path):
        # This is the property item 1's crash-safety and item 41's dedup
        # both rely on: once marked, later attempts for the same hash must
        # not touch the marker again.
        target = tmp_path / "extracted" / "files" / "doc.txt"
        target.parent.mkdir(parents=True)
        target.write_text("content")
        ingest_module.create_hash_link("a" * 64, target)
        link = Path("extracted/sha256/" + "a" * 64)
        original_target = link.readlink()

        second_target = tmp_path / "extracted" / "files" / "other.txt"
        second_target.write_text("different content, same hash for this test")
        result = ingest_module.create_hash_link("a" * 64, second_target)

        assert result is False
        assert link.readlink() == original_target


class TestLoadSha256Set:
    def test_missing_file_yields_empty_set(self, ingest_module, tmp_path):
        assert ingest_module.load_sha256_set(str(tmp_path / "missing.txt")) == set()

    def test_parses_one_hash_per_line(self, ingest_module, tmp_path):
        f = tmp_path / "hashes.txt"
        f.write_text("aaa\nbbb\n\nccc\n")
        assert ingest_module.load_sha256_set(str(f)) == {"aaa", "bbb", "ccc"}


class TestExtractionStatus:
    def test_ok_when_in_neither_list(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = set()
        assert ingest_module.extraction_status("x") == "ok"

    def test_encrypted_when_in_still_encrypted(self, ingest_module):
        ingest_module.still_encrypted = {"x"}
        ingest_module.still_corrupt = set()
        assert ingest_module.extraction_status("x") == "encrypted"

    def test_corrupt_when_in_still_corrupt(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = {"x"}
        assert ingest_module.extraction_status("x") == "corrupt"

    def test_encrypted_takes_priority_if_in_both(self, ingest_module):
        # Should not happen in practice (unpack classifies a file as exactly
        # one outcome), but the classification order should still be stable
        # and deterministic rather than depend on dict/set iteration order.
        ingest_module.still_encrypted = {"x"}
        ingest_module.still_corrupt = {"x"}
        assert ingest_module.extraction_status("x") == "encrypted"


class TestProcessFilesDedup:
    """The item 41 fix: content seen once in a run must be queued for
    sending only once, no matter how many files on disk share it.

    process_files() dispatches prepare_file() through a ProcessPoolExecutor,
    which needs each worker subprocess to re-import the target module by
    name - not possible for ingest.py here, since it is loaded dynamically
    from a file path rather than being a real installed/importable package.
    Swapping in a ThreadPoolExecutor sidesteps that packaging-only problem:
    prepare_file() is plain synchronous I/O with no shared mutable state, so
    running it on threads instead of processes changes nothing about the
    dedup behaviour under test, which lives entirely in process_files()'s
    single-threaded consumer loop, not in prepare_file() itself.
    """

    def test_duplicate_content_is_sent_only_once(self, ingest_module, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_module, "ProcessPoolExecutor", ThreadPoolExecutor)
        files_dir = tmp_path / "extracted" / "files"
        files_dir.mkdir(parents=True)
        (files_dir / "a.txt").write_text("same content")
        (files_dir / "b.txt").write_text("same content")
        (files_dir / "c.txt").write_text("same content")

        sent_batches = []

        def fake_process_batch(items):
            sent_batches.append([item["sha256"] for item in items])
            results = []
            for item in items:
                ingest_module.create_hash_link(item["sha256"], item["fname"])
                results.append((ingest_module.INDEXED, item["sha256"], None))
            return results

        monkeypatch.setattr(ingest_module, "process_batch", fake_process_batch)

        ingest_module.process_files(files_dir)

        all_sent = [sha for batch in sent_batches for sha in batch]
        assert len(all_sent) == 1, f"expected exactly one send for identical content, got {all_sent}"

    def test_distinct_content_is_each_sent(self, ingest_module, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_module, "ProcessPoolExecutor", ThreadPoolExecutor)
        files_dir = tmp_path / "extracted" / "files"
        files_dir.mkdir(parents=True)
        (files_dir / "a.txt").write_text("content A")
        (files_dir / "b.txt").write_text("content B")

        sent_batches = []

        def fake_process_batch(items):
            sent_batches.append([item["sha256"] for item in items])
            results = []
            for item in items:
                ingest_module.create_hash_link(item["sha256"], item["fname"])
                results.append((ingest_module.INDEXED, item["sha256"], None))
            return results

        monkeypatch.setattr(ingest_module, "process_batch", fake_process_batch)

        ingest_module.process_files(files_dir)

        all_sent = [sha for batch in sent_batches for sha in batch]
        assert len(all_sent) == len(set(all_sent)) == 2
