"""Near-duplicate detection via SimHash (docs/IMPROVEMENTS.md item 33): mail
threads, template letters, and versioned files that differ only slightly
share almost all their content, but exact sha256 dedup (already in place -
see item 41) only catches byte-identical files. SimHash gives a fixed-size
fingerprint where near-identical text produces a small Hamming distance
between fingerprints, cheap to compare at scale without an O(n^2) text diff.

Pure functions only - no network, no Elasticsearch. See bin/deis.py's
`dedupe-scan` subcommand for how this is applied to indexed documents.
"""

import hashlib
import re
from collections.abc import Iterable

FINGERPRINT_BITS = 64
SHINGLE_SIZE = 4  # words per shingle

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _shingles(text: str, size: int = SHINGLE_SIZE) -> Iterable[str]:
    """Overlapping word n-grams, lowercased, digits/punctuation stripped out
    of the tokens themselves (this corpus's financial documents are full of
    amounts and dates that would otherwise dominate the shingle set without
    saying anything about whether two documents are the same letter/thread).
    """
    words = _WORD_RE.findall(text.lower())
    if len(words) < size:
        if words:
            yield " ".join(words)
        return
    for i in range(len(words) - size + 1):
        yield " ".join(words[i : i + size])


def _shingle_hash(shingle: str) -> int:
    """A stable (not process-randomized, unlike Python's built-in hash() for
    strings) hash, truncated to FINGERPRINT_BITS bits.
    """
    digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=FINGERPRINT_BITS // 8).digest()
    return int.from_bytes(digest, "big")


def fingerprint(text: str) -> int | None:
    """Computes a FINGERPRINT_BITS-bit SimHash: for each bit position, sums
    +1/-1 across every shingle's hash (weighted by how often a shingle
    repeats, which is exactly what should make a document's fingerprint
    more stable and representative), then sets that bit if the sum is
    positive. Two documents sharing most of their shingles end up with
    fingerprints differing in only a handful of bits, regardless of
    unrelated edits elsewhere in the text.

    Returns None - not 0 - for text that yields no shingles at all, i.e.
    that contains no alphabetic words: _shingles() strips digits and
    punctuation out of the tokens themselves, so a purely numeric/tabular
    document (a CSV of amounts and dates, very common in this corpus)
    reduces to nothing. Those documents have no measurable content to
    compare, and returning 0 made every one of them a distance-0 match for
    every other, collapsing them into a single bogus "near-duplicate"
    cluster. None forces the caller to exclude them explicitly.
    """
    bit_totals = [0] * FINGERPRINT_BITS
    shingle_count = 0
    for shingle in _shingles(text):
        shingle_count += 1
        shingle_hash = _shingle_hash(shingle)
        for bit in range(FINGERPRINT_BITS):
            if shingle_hash & (1 << bit):
                bit_totals[bit] += 1
            else:
                bit_totals[bit] -= 1

    if shingle_count == 0:
        return None

    result = 0
    for bit in range(FINGERPRINT_BITS):
        if bit_totals[bit] > 0:
            result |= 1 << bit
    return result


def hamming_distance(a: int, b: int) -> int:
    """How many bits differ between two fingerprints - 0 means identical
    shingle-sets, FINGERPRINT_BITS means completely different.
    """
    return (a ^ b).bit_count()


def cluster(fingerprints: dict[str, int], max_distance: int = 10) -> dict[str, str]:
    """Groups ids (e.g. sha256 values) whose fingerprints are within
    max_distance Hamming bits of each other into clusters, via union-find.
    Returns {id: cluster_representative_id} for every id that ended up in a
    cluster of size > 1 - ids with no near-duplicate are omitted entirely,
    so the caller can tell "not clustered" apart from "its own cluster".

    Clustering is transitive, which is a deliberate trade but worth knowing
    when reading a result: a chain of documents each within max_distance of
    the next merges into one cluster even though its two ends can be far
    further apart than max_distance. That is what makes "same template,
    edited repeatedly over months" land in a single cluster; it also means a
    large cluster's members are not all pairwise near-duplicates of each
    other. Lower --max-distance if a cluster looks like it chained too far.

    The default (10 of FINGERPRINT_BITS=64 bits) is empirically calibrated,
    not guessed: a short multi-paragraph letter with one name changed
    measured ~6 bits apart, the same letter with two more small edits ~8,
    and a genuinely unrelated document ~41 - see tests/test_simhash.py.
    Real leak-dump documents (fuller letters/threads) should shift the
    near-duplicate end even tighter, since more shingles means a few
    changed ones move a smaller share of the vote.

    O(n^2) comparisons - fine at the scale one leak's corpus actually
    reaches (hundreds to low thousands of unique documents), not something
    that would scale to a search-engine-sized corpus. A locality-sensitive
    hashing index (bucketing by fingerprint prefix) would be the next step
    if that ever became the bottleneck; not worth the complexity yet.
    """
    parent = {doc_id: doc_id for doc_id in fingerprints}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    ids = list(fingerprints)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if hamming_distance(fingerprints[ids[i]], fingerprints[ids[j]]) <= max_distance:
                union(ids[i], ids[j])

    cluster_members: dict[str, list[str]] = {}
    for doc_id in ids:
        cluster_members.setdefault(find(doc_id), []).append(doc_id)

    result = {}
    for representative, members in cluster_members.items():
        if len(members) > 1:
            for member in members:
                result[member] = representative
    return result
