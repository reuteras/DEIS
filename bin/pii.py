"""Personal-identifier detection (docs/IMPROVEMENTS.md item 31): finds and
checksum-validates Swedish personnummer/samordningsnummer, IBANs, and card
numbers in a block of text, plus (unvalidated - there's no universal
checksum for either) email addresses and phone numbers. Item 48 added the
other Nordic identifiers - Swedish organisationsnummer, bankgiro and
plusgiro, Norwegian fødselsnummer, Finnish henkilötunnus, all with their
own checksums, and Danish CPR-nummer (shape only, its checksum was
abolished in 2007) - and a normalized 12-digit form of each personnummer
so one person written two ways pivots to one value in Kibana.

Every numeric detector requires its own checksum to pass, not just a regex
shape match - this corpus is full of financial data (amounts, dates,
account numbers), and a bare digit-count match would produce constant false
positives. A checksum cuts random noise to roughly a 1-in-10 chance per
candidate, on top of already requiring the right digit count and internal
structure (valid month/day for personnummer). Card numbers add a second,
independent check on top of Luhn - a real network's own issuer-prefix and
length rule (see _card_network) - since a 1-in-10 chance still isn't rare
enough against a corpus this dense with other 13-19 digit numeric runs;
confirmed via a real false positive ("2018040201803012018", three
concatenated dates in an accounting document, Luhn-valid but not a card).

Pure functions only - no network, no Elasticsearch. See bin/deis.py's
`pii-scan` subcommand for how this is applied to indexed documents.
"""

import re
from datetime import UTC, date, datetime

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Deliberately conservative: requires a leading '+' with a country code, or
# a Swedish domestic mobile prefix - a loose "any 9-10 digit run" pattern
# would match constantly against this kind of financial-document corpus
# (amounts, account numbers, dates).
PHONE_RE = re.compile(r"(?:\+\d{1,3}[-\s]?)(?:\d[-\s]?){6,12}\d|\b07\d[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}\b")

# YYMMDD or YYYYMMDD, separator optional, then 4 digits. Samordningsnummer
# (day + 60) is matched by the same shape; validated separately below. The
# separator is captured because "+" carries meaning: it marks a person 100
# or older when no century is written out (see normalize_personnummer).
PERSONNUMMER_RE = re.compile(r"\b(\d{2})?(\d{2})(\d{2})(\d{2})([-+])?(\d{4})\b")

# Swedish organisationsnummer: ten digits, same Luhn rule as personnummer,
# told apart from one by the "month" pair being 20 or above (a real month
# never is; for a personnummer the same two digits are 01-12). An optional
# "16" prefix is Skatteverket's own 12-digit form. Never a person - listed
# because "which companies appear in this dump" is a routine question and
# because a genuine one is checksummed, so it is not noise.
ORGANISATIONSNUMMER_RE = re.compile(r"\b(?:16)?(\d{2})(\d{2})(\d{2})-?(\d{4})\b")

# Swedish bankgiro (7-8 digits written NNN-NNNN / NNNN-NNNN) and plusgiro
# (up to 8 digits written NNNNNNN-N): both end in a standard Luhn check
# digit over the whole number. The separator is required - it is how the
# numbers are always printed on invoices and payment slips, and without it
# any 7-8 digit run in an accounting export would be a candidate.
BANKGIRO_RE = re.compile(r"\b(\d{3,4})-(\d{4})\b")
PLUSGIRO_RE = re.compile(r"\b(\d{4,7})-(\d)\b")

# Norwegian fødselsnummer: DDMMYY + 3 individual digits + 2 check digits,
# eleven digits with no separator. D-numbers (temporary, for people
# without a permanent Norwegian address) add 40 to the day; H-numbers add
# 40 to the month. Two independent mod-11 check digits make this the
# best-validated identifier here: a random 11-digit run passes about 1 in
# 100 times, not 1 in 10.
FODSELSNUMMER_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})(\d{5})\b")

# Finnish henkilötunnus: DDMMYY, a century sign, three individual digits
# and a mod-31 check character. "+" = 1800s, "-" (and since 2023 Y X W V
# U) = 1900s, "A" (and B C D E F) = 2000s. One check character over 31
# possible values, plus the sign and a real date, so a chance match is
# rarer than 1 in 31.
HETU_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})([-+ABCDEFYXWVU])(\d{3})([0-9A-FHJ-NPR-Y])\b")
HETU_CHECK_CHARS = "0123456789ABCDEFHJKLMNPRSTUVWXY"

# Danish CPR-nummer: DDMMYY-NNNN. The original mod-11 rule was abandoned
# in 2007 (the number space ran out for some days), so there is nothing
# to validate beyond the date and the always-present separator - the one
# detector here that is shape-only, like phone numbers. A string that is
# a checksum-valid Swedish personnummer is never also reported as a CPR:
# the two formats overlap exactly (YYMMDD-NNNN vs DDMMYY-NNNN), and the
# checksummed reading wins.
CPR_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})-(\d{4})\b")

IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b")

CARD_NUMBER_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


# Real card networks' own issuer-prefix + exact-length rules (ISO/IEC
# 7812), checked on top of the Luhn check below - Luhn alone isn't enough
# evidence on a corpus this dense with other 13-19 digit numeric runs
# (invoice numbers, accounting references, concatenated dates): a random
# digit string passes Luhn by chance roughly 1 in 10 times, and a corpus
# this size generates enough 13-19 digit candidates for that to show up
# routinely. Confirmed against a real false positive from this corpus:
# "2018040201803012018" (three concatenated dates in an accounting
# document, not a card number) passed Luhn and was flagged before this -
# see TestCardNumbers' regression test. Numeric prefix comparisons rather
# than a hand-rolled regex range - much easier to verify correct against
# the real published BIN ranges than getting an oddly-shaped range like
# Mastercard's 2221-2720 right in regex.
def _card_network(digits: str) -> str | None:
    length = len(digits)
    two, three, four = int(digits[:2]), int(digits[:3]), int(digits[:4])
    if digits[0] == "4" and length in (13, 16, 19):
        return "visa"
    if (51 <= two <= 55 or 2221 <= four <= 2720) and length == 16:
        return "mastercard"
    if two in (34, 37) and length == 15:
        return "amex"
    if (four == 6011 or two == 65 or 644 <= three <= 649) and length == 16:
        return "discover"
    if (300 <= three <= 305 or two in (36, 38)) and length == 14:
        return "diners"
    if 3528 <= four <= 3589 and length == 16:
        return "jcb"
    return None


def _luhn_check_digit(digits: str) -> str:
    """Standard Luhn check digit, doubling every second digit from the
    right - used for card numbers. See _personnummer_luhn_valid for the
    related but distinct formula personnummer uses (doubling from the
    left, over the whole number including the check digit).
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def _personnummer_luhn_valid(ten_digits: str) -> bool:
    """The Skatteverket checksum: over all 10 digits (9 base + 1 check
    digit), double every digit at an odd 1-indexed position from the left
    (1st, 3rd, 5th, 7th, 9th), subtract 9 if the result exceeds 9, sum
    everything (including the untouched even-position digits), valid if
    the total is a multiple of 10.
    """
    total = 0
    for i, ch in enumerate(ten_digits):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def compute_personnummer_check_digit(nine_digits: str) -> str:
    """Given YYMMDDNNN (9 digits), returns the check digit that makes the
    full 10-digit number valid. Used by tests to construct known-valid
    fixtures, rather than trusting a memorized "real" personnummer.
    """
    for candidate in range(10):
        if _personnummer_luhn_valid(nine_digits + str(candidate)):
            return str(candidate)
    raise AssertionError("unreachable: exactly one digit 0-9 always satisfies the checksum")


def _valid_month_day(month: int, day: int) -> bool:
    if not 1 <= month <= 12:
        return False
    # Samordningsnummer adds 60 to the day of birth.
    real_day = day - 60 if day > 60 else day
    if not 1 <= real_day <= 31:
        return False
    if month in (4, 6, 9, 11) and real_day > 30:
        return False
    return not (month == 2 and real_day > 29)


def _personnummer_matches(text: str):
    """Every checksum-valid personnummer/samordningsnummer match object in
    text - shared by find_personnummer (which keeps the text as found)
    and find_personnummer_normalized (which rewrites it to 12 digits).
    """
    for match in PERSONNUMMER_RE.finditer(text):
        _century, yy, mm, dd, _separator, suffix = match.groups()
        month, day = int(mm), int(dd)
        if not _valid_month_day(month, day):
            continue
        ten_digits = yy + mm + dd + suffix[:3]
        check_digit = suffix[3]
        if _personnummer_luhn_valid(ten_digits + check_digit):
            yield match


def find_personnummer(text: str) -> list[str]:
    """Finds and checksum-validates Swedish personnummer and
    samordningsnummer. Returns the matched text as found (century digits
    kept if present in the source), deduplicated and sorted.
    """
    return sorted({match.group(0) for match in _personnummer_matches(text)})


def normalize_personnummer(value: str, today: date | None = None) -> str | None:
    """The canonical 12-digit form (YYYYMMDDNNNN) of a personnummer
    written any of the usual ways - "800101-1234", "8001011234",
    "19800101-1234", "198001011234", "800101+1234" - so the same person
    pivots to the same value in Kibana however each document happened to
    write the number. Only the ten-digit forms need the century inferred:
    a person is under 100 unless the separator is "+", per Skatteverket's
    own convention, so the century is the most recent one that keeps the
    birth year in the past (or "today", for a newborn). `today` is
    injectable for tests; the inferred century for a "-"/no-separator
    number therefore shifts once a year at the century boundary, which
    is inherent to the ten-digit form, not a bug here. Returns None for
    anything that doesn't match the personnummer shape at all.
    """
    match = PERSONNUMMER_RE.fullmatch(value.strip())
    if match is None:
        return None
    century, yy, mm, dd, separator, suffix = match.groups()
    if century is not None:
        return century + yy + mm + dd + suffix
    if today is None:
        today = datetime.now(tz=UTC).date()
    year = (today.year // 100) * 100 + int(yy)
    if year > today.year:
        year -= 100
    if separator == "+":
        year -= 100
    return f"{year:04d}{mm}{dd}{suffix}"


def find_personnummer_normalized(text: str, today: date | None = None) -> list[str]:
    """find_personnummer's matches, each rewritten to the 12-digit form -
    see normalize_personnummer. Deduplicated on the normalized value, so a
    document that writes the same person both ways yields it once.
    """
    found = set()
    for match in _personnummer_matches(text):
        normalized = normalize_personnummer(match.group(0), today=today)
        if normalized is not None:
            found.add(normalized)
    return sorted(found)


def find_organisationsnummer(text: str) -> list[str]:
    """Finds and checksum-validates Swedish organisationsnummer, returned
    as their bare ten digits (no separator, no "16" prefix) so the same
    company pivots to one value.
    """
    found = set()
    for match in ORGANISATIONSNUMMER_RE.finditer(text):
        group_digits, mm, rest, suffix = match.groups()
        if int(mm) < 20:
            continue
        ten_digits = group_digits + mm + rest + suffix
        if _personnummer_luhn_valid(ten_digits):
            found.add(ten_digits)
    return sorted(found)


def _standard_luhn_valid(digits: str) -> bool:
    return _luhn_check_digit(digits[:-1]) == digits[-1]


def find_bankgiro(text: str) -> list[str]:
    """Finds Luhn-valid Swedish bankgiro numbers, kept as written
    (NNN-NNNN / NNNN-NNNN, the form they are always printed in)."""
    found = set()
    for match in BANKGIRO_RE.finditer(text):
        digits = match.group(1) + match.group(2)
        if _standard_luhn_valid(digits):
            found.add(match.group(0))
    return sorted(found)


def find_plusgiro(text: str) -> list[str]:
    """Finds Luhn-valid Swedish plusgiro numbers, kept as written
    (NNNNNNN-N)."""
    found = set()
    for match in PLUSGIRO_RE.finditer(text):
        digits = match.group(1) + match.group(2)
        if _standard_luhn_valid(digits):
            found.add(match.group(0))
    return sorted(found)


def _mod11_check_digit(digits: str, weights: tuple[int, ...]) -> int | None:
    """Norway's fødselsnummer check-digit rule: 11 minus the weighted sum
    mod 11, where 11 means 0 and 10 means "no valid number exists with
    these leading digits" (returned as None).
    """
    remainder = sum(int(d) * w for d, w in zip(digits, weights, strict=True)) % 11
    check = 11 - remainder
    if check == 11:
        return 0
    if check == 10:
        return None
    return check


_FODSELSNUMMER_K1_WEIGHTS = (3, 7, 6, 1, 8, 9, 4, 5, 2)
_FODSELSNUMMER_K2_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def fodselsnummer_valid(digits: str) -> bool:
    """Whether eleven digits are a valid Norwegian fødselsnummer (or
    D-/H-number): a real date under the +40 day/month conventions, and
    both mod-11 check digits correct. Exposed (not underscored) so tests
    can build known-valid fixtures by search rather than from memory.
    """
    if len(digits) != 11 or not digits.isdigit():
        return False
    day, month = int(digits[0:2]), int(digits[2:4])
    if day > 40:
        day -= 40
    if month > 40:
        month -= 40
    if not _valid_month_day(month, day) or day > 31:
        return False
    k1 = _mod11_check_digit(digits[:9], _FODSELSNUMMER_K1_WEIGHTS)
    if k1 is None or k1 != int(digits[9]):
        return False
    k2 = _mod11_check_digit(digits[:10], _FODSELSNUMMER_K2_WEIGHTS)
    return k2 is not None and k2 == int(digits[10])


def find_fodselsnummer(text: str) -> list[str]:
    """Finds and checksum-validates Norwegian fødselsnummer (see
    fodselsnummer_valid)."""
    return sorted({match.group(0) for match in FODSELSNUMMER_RE.finditer(text) if fodselsnummer_valid(match.group(0))})


def hetu_valid(value: str) -> bool:
    """Whether a string is a valid Finnish henkilötunnus: DDMMYY, a
    recognized century sign, three digits and the right mod-31 check
    character (see HETU_RE/HETU_CHECK_CHARS)."""
    match = HETU_RE.fullmatch(value)
    if match is None:
        return False
    dd, mm, yy, _sign, nnn, check = match.groups()
    if not _valid_month_day(int(mm), int(dd)) or int(dd) > 31:
        return False
    return HETU_CHECK_CHARS[int(dd + mm + yy + nnn) % 31] == check


def find_hetu(text: str) -> list[str]:
    """Finds and checksum-validates Finnish henkilötunnus (see hetu_valid)."""
    return sorted({match.group(0) for match in HETU_RE.finditer(text) if hetu_valid(match.group(0))})


def find_cpr(text: str) -> list[str]:
    """Finds Danish CPR-numre by shape only (DDMMYY-NNNN with a real
    date) - there has been no checksum to validate since 2007, see CPR_RE.
    Anything that is also a checksum-valid Swedish personnummer is left
    to find_personnummer instead. Treat a hit as a weaker lead than the
    checksummed detectors' hits.
    """
    swedish = set(find_personnummer(text))
    found = set()
    for match in CPR_RE.finditer(text):
        dd, mm, _yy, _suffix = match.groups()
        if not _valid_month_day(int(mm), int(dd)) or int(dd) > 31:
            continue
        if match.group(0) in swedish:
            continue
        found.add(match.group(0))
    return sorted(found)


def find_emails(text: str) -> list[str]:
    return sorted(set(EMAIL_RE.findall(text)))


def find_phone_numbers(text: str) -> list[str]:
    return sorted({match.group(0).strip() for match in PHONE_RE.finditer(text)})


def find_ibans(text: str) -> list[str]:
    """Finds and checksum-validates IBANs (mod-97, per ISO 7064)."""
    found = set()
    for match in IBAN_RE.finditer(text):
        candidate = match.group(1)
        rearranged = candidate[4:] + candidate[:4]
        numeric = "".join(str(int(ch, 36)) for ch in rearranged)
        if int(numeric) % 97 == 1:
            found.add(candidate)
    return sorted(found)


def find_card_numbers(text: str) -> list[str]:
    """Finds card numbers (13-19 digits, spaces/dashes allowed as
    separators) that both pass the Luhn checksum AND match a real card
    network's own issuer-prefix/length rule (see _card_network above) -
    Luhn alone isn't enough evidence on this kind of corpus.
    """
    found = set()
    for match in CARD_NUMBER_RE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group(0))
        if not 13 <= len(digits) <= 19:
            continue
        if digits == digits[0] * len(digits):
            # All-same-digit runs pass Luhn by construction but are never
            # a real card number.
            continue
        if _luhn_check_digit(digits[:-1]) == digits[-1] and _card_network(digits) is not None:
            found.add(digits)
    return sorted(found)


def detect_all(text: str) -> dict:
    """Runs every detector and returns a dict ready to be merged into a
    document's "pii" field - see bin/deis.py's cmd_pii_scan.
    """
    result = {
        "personnummer": find_personnummer(text),
        "emails": find_emails(text),
        "phone_numbers": find_phone_numbers(text),
        "ibans": find_ibans(text),
        "card_numbers": find_card_numbers(text),
        "organisationsnummer": find_organisationsnummer(text),
        "bankgiro": find_bankgiro(text),
        "plusgiro": find_plusgiro(text),
        "fodselsnummer": find_fodselsnummer(text),
        "hetu": find_hetu(text),
        "cpr": find_cpr(text),
    }
    result["has_pii"] = any(result.values())
    # Derived from "personnummer", not a detector of its own, so it is
    # added after has_pii is decided - see normalize_personnummer.
    result["personnummer_normalized"] = find_personnummer_normalized(text)
    return result
