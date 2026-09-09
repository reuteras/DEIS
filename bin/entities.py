"""Named-entity extraction (docs/IMPROVEMENTS.md item 32): finds people,
organizations, and locations in a block of text using spaCy's trained NER
models - not a regex/heuristic approach, unlike bin/pii.py's detectors,
since there is no checksum or fixed shape to validate a person's name
against, and a low-quality regex "NER" (flagging every capitalized phrase)
would be worse than nothing on a tool whose whole premise is trustworthy
answers. See bin/VENDORED.md for why spaCy specifically, and the exact
pinned model versions.

Both language models are loaded once at import time, not per call - model
loading takes a real, noticeable amount of time (~1-2s each), so doing it
per-document would make a full-corpus scan impractically slow.

Both are loaded with the tagger/parser/attribute_ruler/lemmatizer
components excluded - none of those are dependencies of "ner" in these
models' default pipelines (confirmed: excluding them changes nothing
about which entities are found), and running them anyway costs real time
against this corpus's largest documents (confirmed against a real
194,000-character document - the Tika indexed_chars cap most documents
this size hit - full pipeline vs. tok2vec+ner only measurably differed,
and per-document cost at this size is already ~7s even NER-only, which is
why entity-scan's own docstring in bin/deis.py flags a full-corpus run as
a genuinely long operation, not a quick pass).

The two languages' NER label sets are NOT the same scheme (confirmed
directly against both loaded models' own nlp.get_pipe("ner").labels,
rather than assumed from English's scheme alone):
- English (en_core_web_sm): PERSON, ORG, GPE, LOC (+ others not used here -
    DATE, MONEY, etc.). Locations split into GPE (countries/cities/states)
    and LOC (everything else location-like, e.g. mountain ranges) - both
    bucketed together here.
- Swedish (sv_core_news_sm): PRS, ORG, LOC (+ others not used here - EVN,
    MSR, OBJ, TME, WRK). No GPE/LOC split - LOC covers all locations.

Pure functions only (aside from the one-time model load below) - no
network, no Elasticsearch. See bin/deis.py's `entity-scan` subcommand for
how this is applied to indexed documents.
"""

import spacy

# A name not present in a given model's own pipeline is silently ignored
# (confirmed directly) rather than an error, so one shared list covering
# both models' non-NER components is safe even though the two don't have
# identical pipelines (Swedish also has "morphologizer", English doesn't).
_UNUSED_PIPES = ["tagger", "parser", "attribute_ruler", "lemmatizer", "morphologizer"]
_NLP_BY_LANGUAGE = {
    "english": spacy.load("en_core_web_sm", exclude=_UNUSED_PIPES),
    "swedish": spacy.load("sv_core_news_sm", exclude=_UNUSED_PIPES),
}

# Per-language NER label -> this module's own bucket name. Deliberately a
# lookup table per language rather than one shared set of label names -
# see the module docstring for why the two models' schemes don't match.
_LABEL_BUCKETS = {
    "english": {
        "PERSON": "persons",
        "ORG": "organizations",
        "GPE": "locations",
        "LOC": "locations",
    },
    "swedish": {
        "PRS": "persons",
        "ORG": "organizations",
        "LOC": "locations",
    },
}


def detect_entities(text: str, language: str) -> dict:
    """Runs the NER model matching `language` ("english"/"swedish", the
    same values bin/deis.py's language-detection stopword script already
    tags every document with) and buckets its entities into
    persons/organizations/locations - the same array-of-string shape
    bin/pii.py's detect_all() uses for its own fields, for consistency.

    "unknown" (common on numeric/tabular documents, per the language-
    detection script's own docstring) is deliberately not attempted here:
    there is no model to pick, and running one anyway would either guess
    a language wrong or produce meaningless entities on content that's
    mostly numbers - returns everything empty, the same shape a language
    with a model but with no entities found would return, rather than a
    different sentinel a caller would have to special-case.
    """
    result = {"persons": [], "organizations": [], "locations": []}
    nlp = _NLP_BY_LANGUAGE.get(language)
    if nlp is None or not text:
        result["has_entities"] = False
        return result

    buckets = _LABEL_BUCKETS[language]
    found = {"persons": set(), "organizations": set(), "locations": set()}
    for ent in nlp(text).ents:
        bucket = buckets.get(ent.label_)
        if bucket is not None:
            found[bucket].add(ent.text.strip())

    for bucket, values in found.items():
        result[bucket] = sorted(values)
    result["has_entities"] = any(result.values())
    return result
