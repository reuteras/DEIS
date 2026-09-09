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


class TestRowHashLinkMarker:
    """Mirrors TestHashLinkMarker exactly - same crash-safety/idempotency
    property, just for item 21's separate rows marker.
    """

    def test_does_not_exist_initially(self, ingest_module):
        assert ingest_module.row_hash_link_exists("a" * 64) is False

    def test_create_then_exists(self, ingest_module, tmp_path):
        target = tmp_path / "extracted" / "files" / "doc.csv"
        target.parent.mkdir(parents=True)
        target.write_text("a,b\n1,2\n")

        created = ingest_module.create_row_hash_link("a" * 64, target)

        assert created is True
        assert ingest_module.row_hash_link_exists("a" * 64) is True

    def test_create_is_a_no_op_the_second_time(self, ingest_module, tmp_path):
        target = tmp_path / "extracted" / "files" / "doc.csv"
        target.parent.mkdir(parents=True)
        target.write_text("a,b\n1,2\n")
        ingest_module.create_row_hash_link("a" * 64, target)
        link = Path("extracted/sha256_rows/" + "a" * 64)
        original_target = link.readlink()

        second_target = tmp_path / "extracted" / "files" / "other.csv"
        second_target.write_text("different content, same hash for this test")
        result = ingest_module.create_row_hash_link("a" * 64, second_target)

        assert result is False
        assert link.readlink() == original_target

    def test_independent_from_the_blob_marker(self, ingest_module, tmp_path):
        # The whole point of a second marker: a file already blob-indexed
        # (create_hash_link) must still report its rows as not-yet-done
        # until create_row_hash_link is separately called.
        target = tmp_path / "extracted" / "files" / "doc.csv"
        target.parent.mkdir(parents=True)
        target.write_text("a,b\n1,2\n")
        ingest_module.create_hash_link("a" * 64, target)

        assert ingest_module.hash_link_exists("a" * 64) is True
        assert ingest_module.row_hash_link_exists("a" * 64) is False


class TestParseCsvRows:
    def test_comma_delimited_with_header(self, ingest_module):
        content = b"name,age\nAlice,30\nBob,25\n"
        rows, meta = ingest_module.parse_csv_rows(content, max_rows=100)
        assert rows == [{"name": "Alice", "age": "30"}, {"name": "Bob", "age": "25"}]
        assert meta["truncated"] is False

    def test_semicolon_delimited_with_header(self, ingest_module):
        # Swedish locale exports commonly use ';' since ',' is the decimal
        # separator.
        content = "namn;stad\nAnna;Stockholm\nBjörn;Malmö\n".encode()
        rows, meta = ingest_module.parse_csv_rows(content, max_rows=100)
        assert rows == [{"namn": "Anna", "stad": "Stockholm"}, {"namn": "Björn", "stad": "Malmö"}]
        assert meta["truncated"] is False

    def test_header_less_input_is_misread_as_a_header_documented_tradeoff(self, ingest_module):
        # The first row is always treated as a header (see parse_csv_rows'
        # docstring for why csv.Sniffer.has_header() isn't trusted here) -
        # a genuinely header-less file has its first data row consumed as
        # column names instead of appearing as its own row.
        content = b"1,2,3\n4,5,6\n"
        rows, meta = ingest_module.parse_csv_rows(content, max_rows=100)
        assert rows == [{"1": "4", "2": "5", "3": "6"}]
        assert meta["truncated"] is False

    def test_utf8_bom_is_stripped(self, ingest_module):
        content = b"\xef\xbb\xbfname\nAlice\n"
        rows, _meta = ingest_module.parse_csv_rows(content, max_rows=100)
        assert rows == [{"name": "Alice"}]

    def test_cp1252_swedish_bytes_decode(self, ingest_module):
        # 'å' is 0xe5 in cp1252 but invalid as a standalone UTF-8 byte, so
        # this only succeeds once the cp1252 fallback kicks in.
        content = "stad\nMalmå\n".encode("cp1252")
        rows, _meta = ingest_module.parse_csv_rows(content, max_rows=100)
        assert rows == [{"stad": "Malmå"}]

    def test_truncates_past_max_rows(self, ingest_module):
        content = b"n\n" + b"".join(f"{i}\n".encode() for i in range(10))
        rows, meta = ingest_module.parse_csv_rows(content, max_rows=5)
        assert len(rows) == 5
        assert meta["truncated"] is True

    def test_garbage_bytes_never_raise(self, ingest_module):
        # latin-1 guarantees a successful decode of literally any byte
        # sequence, so this exercises "never raises", not "always empty" -
        # garbage bytes may still parse into meaningless rows (there's no
        # reliable way to detect "this isn't really CSV-shaped" short of
        # the parse itself succeeding); the only real invariant is no
        # exception ever escapes.
        content = bytes(range(256)) * 3
        rows, meta = ingest_module.parse_csv_rows(content, max_rows=100)
        assert isinstance(rows, list)
        assert isinstance(meta, dict)

    def test_empty_bytes_yields_no_rows_no_crash(self, ingest_module):
        rows, meta = ingest_module.parse_csv_rows(b"", max_rows=100)
        assert rows == []
        assert "error" in meta


class TestPrepareFileCsvBranching:
    def test_non_csv_file_unaffected(self, ingest_module, tmp_path):
        # Regression guard: item 21 must be purely additive for every
        # other file type.
        f = tmp_path / "extracted" / "files" / "doc.txt"
        f.parent.mkdir(parents=True)
        f.write_text("hello")

        result = ingest_module.prepare_file(f)

        assert result["status"] == "ready"
        assert result["rows"] is None
        assert result["row_meta"] is None

    def test_new_csv_file_is_ready_with_rows(self, ingest_module, tmp_path):
        f = tmp_path / "extracted" / "files" / "doc.csv"
        f.parent.mkdir(parents=True)
        f.write_text("a,b\n1,2\n")

        result = ingest_module.prepare_file(f)

        assert result["status"] == "ready"
        assert result["rows"] == [{"a": "1", "b": "2"}]

    def test_blob_indexed_csv_is_ready_rows_only(self, ingest_module, tmp_path):
        # The backfill case: already blob-indexed (marker present), rows
        # marker still missing.
        f = tmp_path / "extracted" / "files" / "doc.csv"
        f.parent.mkdir(parents=True)
        f.write_text("a,b\n1,2\n")
        sha256 = ingest_module.get_filehash(f)
        ingest_module.create_hash_link(sha256, f)

        result = ingest_module.prepare_file(f)

        assert result["status"] == "ready_rows_only"
        assert result["rows"] == [{"a": "1", "b": "2"}]

    def test_fully_done_csv_is_present(self, ingest_module, tmp_path):
        f = tmp_path / "extracted" / "files" / "doc.csv"
        f.parent.mkdir(parents=True)
        f.write_text("a,b\n1,2\n")
        sha256 = ingest_module.get_filehash(f)
        ingest_module.create_hash_link(sha256, f)
        ingest_module.create_row_hash_link(sha256, f)

        result = ingest_module.prepare_file(f)

        assert result["status"] == ingest_module.PRESENT

    def test_csv_rows_disabled_behaves_like_a_plain_file(self, ingest_module, tmp_path):
        ingest_module.csv_rows_enabled = False
        f = tmp_path / "extracted" / "files" / "doc.csv"
        f.parent.mkdir(parents=True)
        f.write_text("a,b\n1,2\n")

        result = ingest_module.prepare_file(f)

        assert result["status"] == "ready"
        assert result["rows"] is None


class TestLoadSha256Set:
    def test_missing_file_yields_empty_set(self, ingest_module, tmp_path):
        assert ingest_module.load_sha256_set(str(tmp_path / "missing.txt")) == set()

    def test_parses_one_hash_per_line(self, ingest_module, tmp_path):
        f = tmp_path / "hashes.txt"
        f.write_text("aaa\nbbb\n\nccc\n")
        assert ingest_module.load_sha256_set(str(f)) == {"aaa", "bbb", "ccc"}


class TestExtractionStatus:
    def test_ok_when_in_no_list(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = set()
        ingest_module.still_unsafe = set()
        ingest_module.still_multivolume = set()
        assert ingest_module.extraction_status("x") == "ok"

    def test_encrypted_when_in_still_encrypted(self, ingest_module):
        ingest_module.still_encrypted = {"x"}
        ingest_module.still_corrupt = set()
        ingest_module.still_unsafe = set()
        ingest_module.still_multivolume = set()
        assert ingest_module.extraction_status("x") == "encrypted"

    def test_corrupt_when_in_still_corrupt(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = {"x"}
        ingest_module.still_unsafe = set()
        ingest_module.still_multivolume = set()
        assert ingest_module.extraction_status("x") == "corrupt"

    def test_unsafe_when_in_still_unsafe(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = set()
        ingest_module.still_unsafe = {"x"}
        ingest_module.still_multivolume = set()
        assert ingest_module.extraction_status("x") == "unsafe"

    def test_multivolume_when_in_still_multivolume(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = set()
        ingest_module.still_unsafe = set()
        ingest_module.still_multivolume = {"x"}
        assert ingest_module.extraction_status("x") == "multivolume"

    def test_decrypted_when_in_decrypted(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = set()
        ingest_module.still_unsafe = set()
        ingest_module.still_multivolume = set()
        ingest_module.decrypted = {"x"}
        assert ingest_module.extraction_status("x") == "decrypted"

    def test_encrypted_takes_priority_over_corrupt_and_unsafe(self, ingest_module):
        # Should not happen in practice (unpack classifies a file as exactly
        # one outcome), but the classification order should still be stable
        # and deterministic rather than depend on dict/set iteration order.
        ingest_module.still_encrypted = {"x"}
        ingest_module.still_corrupt = {"x"}
        ingest_module.still_unsafe = {"x"}
        ingest_module.still_multivolume = {"x"}
        ingest_module.decrypted = {"x"}
        assert ingest_module.extraction_status("x") == "encrypted"

    def test_corrupt_takes_priority_over_unsafe_and_multivolume(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = {"x"}
        ingest_module.still_unsafe = {"x"}
        ingest_module.still_multivolume = {"x"}
        assert ingest_module.extraction_status("x") == "corrupt"

    def test_unsafe_takes_priority_over_multivolume(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = set()
        ingest_module.still_unsafe = {"x"}
        ingest_module.still_multivolume = {"x"}
        assert ingest_module.extraction_status("x") == "unsafe"

    def test_multivolume_takes_priority_over_decrypted(self, ingest_module):
        ingest_module.still_encrypted = set()
        ingest_module.still_corrupt = set()
        ingest_module.still_unsafe = set()
        ingest_module.still_multivolume = {"x"}
        ingest_module.decrypted = {"x"}
        assert ingest_module.extraction_status("x") == "multivolume"


class TestIndexRunSummary:
    """index_run_summary() (item 25's remaining piece): a run's reconciliation
    counts, best-effort indexed into RUNS_INDEX so they're visible in Kibana,
    not just the container's own stdout.
    """

    def test_posts_counts_with_auth_to_runs_index(self, ingest_module, monkeypatch):
        calls = []

        class FakeResponse:
            status_code = 201

        def fake_post(url, json=None, auth=None, timeout=None):
            calls.append({"url": url, "json": json, "auth": auth})
            return FakeResponse()

        monkeypatch.setattr(ingest_module.requests, "post", fake_post)

        ingest_module.index_run_summary({"files_looked_at": 5})

        assert len(calls) == 1
        assert calls[0]["url"].endswith(f"/{ingest_module.RUNS_INDEX}/_doc")
        assert calls[0]["json"] == {"files_looked_at": 5}
        assert calls[0]["auth"] == ingest_module.elastic_auth()

    def test_does_not_raise_when_elastic_is_unreachable(self, ingest_module, monkeypatch):
        def fake_post(*args, **kwargs):
            raise ingest_module.requests.exceptions.ConnectionError("no route to host")

        monkeypatch.setattr(ingest_module.requests, "post", fake_post)

        # Must not raise: a run that otherwise fully succeeded shouldn't be
        # reported as failed just because this one extra write couldn't land.
        ingest_module.index_run_summary({"files_looked_at": 5})


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


class TestProcessFilesRowsOnlyCounting:
    """Regression test for a real bug found during this feature's own live
    verification run: a file whose blob was already indexed but still
    needed CSV rows ("ready_rows_only") was silently missing from
    process_files()'s results entirely - not counted as INDEXED, PRESENT,
    or FAILED - undercounting "files looked at"/"unique files" in the run
    summary by exactly the number of such files (427 on the real corpus),
    even though nothing was actually lost from Elasticsearch (the blob
    really was already there; only the run's own accounting was wrong).
    """

    def test_ready_rows_only_file_is_recorded_as_present(self, ingest_module, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest_module, "ProcessPoolExecutor", ThreadPoolExecutor)
        files_dir = tmp_path / "extracted" / "files"
        files_dir.mkdir(parents=True)
        csv_file = files_dir / "data.csv"
        csv_file.write_text("a,b\n1,2\n")
        sha256 = ingest_module.get_filehash(csv_file)
        ingest_module.create_hash_link(sha256, csv_file)  # blob already indexed

        def fake_process_rows_batch(items):
            results = []
            for item in items:
                ingest_module.create_row_hash_link(item["sha256"], item["fname"])
                results.append((ingest_module.INDEXED, item["sha256"], None))
            return results

        def fail_process_batch(_items):
            raise AssertionError("process_batch must not be called for a ready_rows_only file")

        captured = {}

        def fake_print_summary(results, row_results, _directory):
            captured["results"] = results
            captured["row_results"] = row_results
            return 0

        monkeypatch.setattr(ingest_module, "process_rows_batch", fake_process_rows_batch)
        monkeypatch.setattr(ingest_module, "process_batch", fail_process_batch)
        monkeypatch.setattr(ingest_module, "print_summary", fake_print_summary)

        ingest_module.process_files(files_dir)

        assert (ingest_module.PRESENT, sha256, None) in captured["results"]
        assert (ingest_module.INDEXED, sha256, None) in captured["row_results"]
