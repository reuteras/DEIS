"""Tests for bin/entities.py's spaCy-based named-entity extraction (item
32). Uses the real, pinned spaCy models (see bin/VENDORED.md) rather than
mocking them - this module has no logic worth testing independently of
what the models actually produce, so a mock would just test the mock.

Every test sentence's expected spaCy output was verified directly against
the loaded models before being hardcoded here, rather than assumed - both
"sm" tier models are real but imperfect (confirmed while doing this: the
Swedish model mistags some real organizations as PRS/LOC/nothing depending
on sentence context), so a sentence was only used once its actual labels
were checked, not guessed from English intuition.
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_entities", REPO_ROOT / "bin" / "entities.py")
entities = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = entities
spec.loader.exec_module(entities)


class TestDetectEntitiesEnglish:
    def test_person_organization_and_location_all_bucketed(self):
        result = entities.detect_entities("Maria Johansson works at Volvo in Gothenburg.", "english")
        assert "Maria Johansson" in result["persons"]
        assert "Volvo" in result["organizations"]
        assert "Gothenburg" in result["locations"]
        assert result["has_entities"] is True

    def test_gpe_and_loc_labels_both_map_to_locations(self):
        # English's NER scheme splits locations into GPE (countries/
        # cities/states) and LOC (everything else location-like) - both
        # must land in the same "locations" bucket here. Gothenburg above
        # is GPE; this confirms the label map itself covers both without
        # needing a sentence that happens to trigger a true LOC label.
        assert entities._LABEL_BUCKETS["english"]["GPE"] == "locations"
        assert entities._LABEL_BUCKETS["english"]["LOC"] == "locations"


class TestDetectEntitiesSwedish:
    def test_organization_and_person_bucketed(self):
        result = entities.detect_entities("IKEA grundades av Ingvar Kamprad.", "swedish")
        assert "IKEA" in result["organizations"]
        assert any("Ingvar" in p for p in result["persons"])
        assert result["has_entities"] is True

    def test_location_bucketed(self):
        result = entities.detect_entities("Maria Johansson jobbar på Volvo i Göteborg.", "swedish")
        assert "Göteborg" in result["locations"]

    def test_label_scheme_differs_from_english_by_design(self):
        # Confirmed directly against both loaded models' own
        # nlp.get_pipe("ner").labels (see the module docstring) - Swedish
        # has no GPE/LOC split, and uses PRS instead of PERSON.
        assert "GPE" not in entities._LABEL_BUCKETS["swedish"]
        assert entities._LABEL_BUCKETS["swedish"]["PRS"] == "persons"


class TestDetectEntitiesEdgeCases:
    def test_unknown_language_is_skipped_not_guessed(self):
        # Numeric/tabular documents are commonly tagged "unknown" by the
        # existing language-detection script - running a model anyway
        # would either guess wrong or produce meaningless entities.
        result = entities.detect_entities("Maria Johansson works at Volvo.", "unknown")
        assert result == {"persons": [], "organizations": [], "locations": [], "has_entities": False}

    def test_empty_text_returns_empty_without_running_a_model(self):
        result = entities.detect_entities("", "english")
        assert result["has_entities"] is False
        assert result["persons"] == result["organizations"] == result["locations"] == []

    def test_text_with_no_entities_returns_empty_but_language_was_valid(self):
        result = entities.detect_entities("The meeting is scheduled for next Tuesday.", "english")
        assert result["has_entities"] is False

    def test_results_are_deduplicated_and_sorted(self):
        result = entities.detect_entities(
            "Maria Johansson met Maria Johansson again. Volvo and Volvo agreed.", "english"
        )
        assert result["persons"].count("Maria Johansson") == 1
        assert result["persons"] == sorted(result["persons"])
