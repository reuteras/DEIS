"""Statistical language identification, replacing the ingest-time stopword
script's guess for documents it can't classify. setup/entrypoint.sh's
deis-detect-language painless script only tags a document english/swedish
if it finds 3+ hits from a fixed list of ten common function words ("the",
"and"... / "och", "det"...) - that only ever fires on flowing prose, so a
genuinely Swedish payroll table (all labels and numbers - "Kontonummer",
"Personnummer", "Utbetalningsdatum" - no connected sentences) falls
through to "unknown" even though a human reads it as Swedish instantly.
Confirmed directly against real documents from this corpus - see
bin/deis.py's `language-scan` subcommand, which reclassifies exactly that
"unknown" bucket using py3langid's statistical classifier instead: it
picks up a language from a handful of real words without needing them
arranged into sentences.

Pure functions only - no network, no Elasticsearch.
"""

import re

import py3langid as langid

# Same idea as simhash._WORD_RE: strip digits/punctuation from the token
# stream before deciding whether there's anything worth classifying at all
# - a purely numeric/tabular document (or one Tika extracted nothing from)
# should stay "unknown" rather than get a classifier's guess on noise. Kept
# as this module's own copy rather than importing simhash's private name -
# the two modules have no other reason to depend on each other.
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_MIN_WORDS = 3

# py3langid classifies into ~97 languages; narrowed here to the same
# three-value scheme (english/swedish/unknown) the ingest-time stopword
# script already tags every document with, so every existing consumer -
# entity-scan's spaCy model selection, Kibana's saved searches/dashboard,
# the notebook's stopword logic - keeps working unchanged. Only the
# accuracy of the classification improves.
_BUCKET_BY_ISO = {"en": "english", "sv": "swedish"}


def detect_language(text: str) -> str:
    """Classifies `text` as "english", "swedish", or "unknown"."""
    if len(_WORD_RE.findall(text)) < _MIN_WORDS:
        return "unknown"
    language, _score = langid.classify(text)
    return _BUCKET_BY_ISO.get(language, "unknown")
