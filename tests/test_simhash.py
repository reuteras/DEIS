"""Tests for bin/simhash.py's near-duplicate detection (item 33)."""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_simhash", REPO_ROOT / "bin" / "simhash.py")
simhash = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = simhash
spec.loader.exec_module(simhash)

LETTER_V1 = """
Dear Team,

Please find attached the quarterly report for your review. Let us know if
you have any questions about the figures before Friday's meeting. We look
forward to discussing the results with everyone on the call.

Best regards,
Finance Team
"""

# Same letter, sent to someone else - one name changed, otherwise identical.
LETTER_V2 = LETTER_V1.replace("Dear Team,", "Dear Client,")

# The same letter with a few more edits - still overwhelmingly the same text.
LETTER_V3 = LETTER_V1.replace("Friday's meeting", "next week's meeting")
LETTER_V3 = LETTER_V3.replace("Finance Team", "Finance Department")

UNRELATED_TEXT = """
This document contains completely different subject matter entirely. It
discusses topics such as gardening techniques, weather patterns across
different seasons, and various recipes for baking bread at home using
traditional methods passed down through generations of family cooking.
"""


class TestFingerprint:
    def test_empty_text_returns_zero(self):
        assert simhash.fingerprint("") == 0

    def test_deterministic(self):
        assert simhash.fingerprint(LETTER_V1) == simhash.fingerprint(LETTER_V1)

    def test_near_identical_texts_have_small_hamming_distance(self):
        fp1 = simhash.fingerprint(LETTER_V1)
        fp2 = simhash.fingerprint(LETTER_V2)
        assert simhash.hamming_distance(fp1, fp2) <= 8  # measured ~6 bits for this fixture

    def test_lightly_edited_text_still_has_small_hamming_distance(self):
        fp1 = simhash.fingerprint(LETTER_V1)
        fp3 = simhash.fingerprint(LETTER_V3)
        assert simhash.hamming_distance(fp1, fp3) <= 12  # measured ~8 bits for this fixture

    def test_unrelated_texts_have_large_hamming_distance(self):
        fp1 = simhash.fingerprint(LETTER_V1)
        fp_unrelated = simhash.fingerprint(UNRELATED_TEXT)
        assert simhash.hamming_distance(fp1, fp_unrelated) > 20  # measured ~41 bits for this fixture

    def test_short_text_below_shingle_size_does_not_crash(self):
        # Fewer words than SHINGLE_SIZE - falls back to a single shingle
        # rather than yielding nothing.
        result = simhash.fingerprint("hello world")
        assert isinstance(result, int)


class TestHammingDistance:
    def test_identical_fingerprints_have_zero_distance(self):
        assert simhash.hamming_distance(0b1010, 0b1010) == 0

    def test_completely_different_bits(self):
        assert simhash.hamming_distance(0b0000, 0b1111) == 4

    def test_symmetric(self):
        a, b = 0b1100, 0b0110
        assert simhash.hamming_distance(a, b) == simhash.hamming_distance(b, a)


class TestCluster:
    def test_near_duplicates_end_up_in_the_same_cluster(self):
        fingerprints = {
            "a": simhash.fingerprint(LETTER_V1),
            "b": simhash.fingerprint(LETTER_V2),
            "c": simhash.fingerprint(LETTER_V3),
        }
        result = simhash.cluster(fingerprints, max_distance=10)
        assert result["a"] == result["b"] == result["c"]

    def test_unrelated_documents_are_not_clustered_together(self):
        fingerprints = {
            "a": simhash.fingerprint(LETTER_V1),
            "b": simhash.fingerprint(UNRELATED_TEXT),
        }
        result = simhash.cluster(fingerprints, max_distance=10)
        assert result == {}

    def test_singleton_with_no_near_duplicate_is_omitted(self):
        fingerprints = {
            "a": simhash.fingerprint(LETTER_V1),
            "b": simhash.fingerprint(UNRELATED_TEXT),
            "c": simhash.fingerprint(LETTER_V2),
        }
        result = simhash.cluster(fingerprints, max_distance=10)
        assert "b" not in result
        assert result["a"] == result["c"]

    def test_empty_input(self):
        assert simhash.cluster({}) == {}

    def test_transitive_chain_forms_one_cluster(self):
        # a-b close, b-c close, a-c not directly close enough on their own -
        # union-find should still merge all three via b.
        fingerprints = {"a": 0b0000, "b": 0b0011, "c": 0b1111}
        result = simhash.cluster(fingerprints, max_distance=2)
        assert result["a"] == result["b"] == result["c"]
