"""Tests for bin/language.py's py3langid-based classification (the
language-scan follow-up to item 32). Uses the real, pinned py3langid model
rather than mocking it - this module has no logic worth testing
independently of what the classifier actually produces.

The Swedish samples below are real content from this project's own leak
corpus (payroll/bank-transfer report PDFs) - the exact kind of tabular,
mostly-numeric-with-a-few-real-words document the ingest-time stopword
script (setup/entrypoint.sh's deis-detect-language) tags "unknown" because
it never finds 3+ of its ten fixed function words in running prose. This
is the failure mode language-scan exists to fix, so it is what these
tests check against, not synthetic sentences.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_language", REPO_ROOT / "bin" / "language.py")
language = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = language
spec.loader.exec_module(language)


class TestDetectLanguage:
    def test_english_prose(self):
        assert language.detect_language("The quick brown fox jumps over the lazy dog this afternoon.") == "english"

    def test_swedish_payroll_table_that_the_stopword_script_misses(self):
        # Real content from a leak-corpus PDF (Löneperiod/Kontonummer/
        # Utbetalningsdatum) - no connected sentences, so the ingest-time
        # stopword script tags this "unknown". py3langid picks up Swedish
        # from the real words alone.
        text = (
            "Banklista 2021-03-19 09:21 Sida 14 Utbetalningsdatum 2021-03-25 "
            "NSP Gallus AB Ranhammarsvagen 20 B 168 67 Bromma Kontaktperson "
            "Namn Kontonummer Belopp Löneperiod Organisationsnr Bank "
            "Kontonummer Utbetalningssätt Personnummer"
        )
        assert language.detect_language(text) == "swedish"

    def test_purely_numeric_content_is_unknown(self):
        assert language.detect_language("12345 67890 11111 22222 33333") == "unknown"

    def test_empty_content_is_unknown(self):
        assert language.detect_language("") == "unknown"

    def test_below_min_word_threshold_is_unknown(self):
        # Two real words is deliberately below _MIN_WORDS (3) - too little
        # signal to trust a classifier trained on real text with.
        assert language.detect_language("Bank AB") == "unknown"

    def test_other_language_buckets_as_unknown(self):
        # French: neither of the two NER-supported languages, so it must
        # not be forced into "english" or "swedish" - entity-scan has no
        # model to run against it either way.
        assert language.detect_language("Le renard brun rapide saute par-dessus le chien paresseux.") == "unknown"
