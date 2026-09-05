"""Personal-identifier detection (docs/IMPROVEMENTS.md item 31): finds and
checksum-validates Swedish personnummer/samordningsnummer, IBANs, and card
numbers in a block of text, plus (unvalidated - there's no universal
checksum for either) email addresses and phone numbers.

Every numeric detector requires its own checksum to pass, not just a regex
shape match - this corpus is full of financial data (amounts, dates,
account numbers), and a bare digit-count match would produce constant false
positives. A checksum cuts random noise to roughly a 1-in-10 chance per
candidate, on top of already requiring the right digit count and internal
structure (valid month/day for personnummer).

Pure functions only - no network, no Elasticsearch. See bin/deis.py's
`pii-scan` subcommand for how this is applied to indexed documents.
"""

import re

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Deliberately conservative: requires a leading '+' with a country code, or
# a Swedish domestic mobile prefix - a loose "any 9-10 digit run" pattern
# would match constantly against this kind of financial-document corpus
# (amounts, account numbers, dates).
PHONE_RE = re.compile(r"(?:\+\d{1,3}[-\s]?)(?:\d[-\s]?){6,12}\d|\b07\d[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}\b")

# YYMMDD or YYYYMMDD, separator optional, then 4 digits. Samordningsnummer
# (day + 60) is matched by the same shape; validated separately below.
PERSONNUMMER_RE = re.compile(r"\b(\d{2})?(\d{2})(\d{2})(\d{2})[-+]?(\d{4})\b")

IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b")

CARD_NUMBER_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


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


def find_personnummer(text: str) -> list[str]:
    """Finds and checksum-validates Swedish personnummer and
    samordningsnummer. Returns the matched text as found (century digits
    kept if present in the source), deduplicated and sorted.
    """
    found = set()
    for match in PERSONNUMMER_RE.finditer(text):
        _century, yy, mm, dd, suffix = match.groups()
        month, day = int(mm), int(dd)
        if not _valid_month_day(month, day):
            continue
        ten_digits = yy + mm + dd + suffix[:3]
        check_digit = suffix[3]
        if _personnummer_luhn_valid(ten_digits + check_digit):
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
    """Finds and Luhn-validates card numbers (13-19 digits, spaces/dashes
    allowed as separators).
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
        if _luhn_check_digit(digits[:-1]) == digits[-1]:
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
    }
    result["has_pii"] = any(result.values())
    return result
