"""Tests for bin/pii.py's checksum-validated PII detectors (item 31).

Personnummer fixtures are constructed by computing our own correct check
digit (compute_personnummer_check_digit) rather than trusting a memorized
"real" example number - that way a mistaken memory can't silently make the
test meaningless. IBAN and card-number fixtures use widely published,
standard test/example values (Wikipedia's IBAN example, the ubiquitous Visa
test card number), which are safe to hardcode since they were never real
discovered data.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_pii", REPO_ROOT / "bin" / "pii.py")
pii = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pii
spec.loader.exec_module(pii)


def _valid_personnummer(nine_digits: str) -> str:
    return nine_digits + pii.compute_personnummer_check_digit(nine_digits)


class TestPersonnummer:
    def test_valid_personnummer_is_found(self):
        pnr = _valid_personnummer("850613" + "123")  # 1985-06-13, arbitrary serial
        text = f"Contact person, personnummer {pnr}, regarding the invoice."
        assert pnr in pii.find_personnummer(text)

    def test_valid_samordningsnummer_is_found(self):
        # Day 13 + 60 = 73 marks this as a samordningsnummer.
        pnr = _valid_personnummer("850673" + "123")
        assert pnr in pii.find_personnummer(f"samordningsnummer: {pnr}")

    def test_wrong_check_digit_is_rejected(self):
        pnr = _valid_personnummer("850613123")
        # Flip the last digit to something guaranteed wrong.
        bad_last = str((int(pnr[-1]) + 1) % 10)
        bad_pnr = pnr[:-1] + bad_last
        assert bad_pnr not in pii.find_personnummer(f"personnummer {bad_pnr}")

    def test_invalid_month_is_rejected(self):
        # Month 13 doesn't exist - checksum is irrelevant, shouldn't match.
        candidate = _valid_personnummer("851301" + "123")
        assert candidate not in pii.find_personnummer(f"personnummer {candidate}")

    def test_invalid_day_is_rejected(self):
        candidate = _valid_personnummer("850632" + "123")  # day 32
        assert candidate not in pii.find_personnummer(f"personnummer {candidate}")

    def test_plain_random_digit_sequence_is_not_falsely_validated(self):
        # A decimal date/amount that happens to be 10 digits shouldn't
        # pass just because it has the right shape - it needs the actual
        # checksum too.
        found = pii.find_personnummer("Invoice total: 1234567890 SEK")
        assert found == []

    def test_dash_separator_is_optional(self):
        base = _valid_personnummer("850613123")
        with_dash = base[:6] + "-" + base[6:]
        assert with_dash in pii.find_personnummer(f"pnr: {with_dash}")
        assert base in pii.find_personnummer(f"pnr: {base}")


class TestEmails:
    def test_finds_a_normal_address(self):
        assert pii.find_emails("Contact: jane.doe@example.com for details") == ["jane.doe@example.com"]

    def test_deduplicates(self):
        text = "a@example.com appears twice: a@example.com"
        assert pii.find_emails(text) == ["a@example.com"]

    def test_ignores_non_email_text(self):
        assert pii.find_emails("no addresses here, just text @ symbols like @home") == []


class TestPhoneNumbers:
    def test_finds_international_format(self):
        assert pii.find_phone_numbers("Call +46 70 123 45 67 for support") != []

    def test_finds_swedish_mobile_format(self):
        assert pii.find_phone_numbers("Mobile: 070-123 45 67") != []

    def test_does_not_match_plain_amount(self):
        assert pii.find_phone_numbers("Total: 12345678") == []


class TestIban:
    def test_finds_valid_iban(self):
        # Germany's widely-published ISO 13616/Wikipedia example IBAN.
        iban = "DE89370400440532013000"
        assert pii.find_ibans(f"Transfer to {iban} please") == [iban]

    def test_rejects_invalid_checksum(self):
        bad_iban = "DE89370400440532013001"
        assert pii.find_ibans(f"Transfer to {bad_iban}") == []


class TestCardNumbers:
    def test_finds_valid_test_visa_number(self):
        # The standard Visa test card number used ubiquitously in payment
        # processor sandboxes - not a real card.
        card = "4111111111111111"
        assert pii.find_card_numbers(f"Card on file: {card}") == [card]

    def test_finds_card_with_space_separators(self):
        assert pii.find_card_numbers("Card on file: 4111 1111 1111 1111") == ["4111111111111111"]

    def test_rejects_invalid_luhn_checksum(self):
        bad_card = "4111111111111112"
        assert pii.find_card_numbers(f"Card: {bad_card}") == []

    def test_rejects_all_same_digit_despite_passing_luhn(self):
        assert pii.find_card_numbers("Card: 0000000000000000") == []

    # Standard test numbers for every network _card_network recognizes -
    # ubiquitous in payment processor sandboxes, not real cards - each
    # independently confirmed to pass Luhn before being hardcoded here.
    @pytest.mark.parametrize(
        ("network", "card"),
        [
            ("visa", "4111111111111111"),
            ("mastercard", "5555555555554444"),
            ("mastercard_2_series", "2221000000000009"),
            ("amex", "378282246310005"),
            ("discover", "6011111111111117"),
            ("diners", "30569309025904"),
            ("jcb", "3530111333300000"),
        ],
    )
    def test_finds_real_network_test_numbers(self, network, card):
        assert pii.find_card_numbers(f"Card ({network}): {card}") == [card]

    # Luhn-valid but not shaped like any real card network's own
    # issuer-prefix/length rule - found live against a real corpus (item
    # 31 follow-up): both of these were flagged as card_numbers by
    # deis pii-scan despite being three/two concatenated dates in a real
    # accounting document ("BFO Timavl 180425.pdf"), not card numbers at
    # all. Luhn alone passes roughly 1 in 10 candidates by chance, and a
    # corpus this dense with other 13-19 digit numeric runs generates
    # enough candidates for that to show up routinely.
    @pytest.mark.parametrize("false_positive", ["2018040201803012018", "3020360312735"])
    def test_rejects_luhn_valid_non_network_shaped_numbers(self, false_positive):
        assert pii.find_card_numbers(f"Ref: {false_positive}") == []

    def test_rejects_right_length_but_wrong_prefix(self):
        # 16 digits, genuinely Luhn-valid (check digit computed via
        # _luhn_check_digit, not guessed) but "9" isn't any recognized
        # network's issuer prefix at this length.
        assert pii.find_card_numbers("Ref: 9111111111111110") == []


class TestDetectAll:
    def test_has_pii_false_when_nothing_found(self):
        result = pii.detect_all("Just an ordinary invoice with no personal data at all.")
        assert result["has_pii"] is False
        assert result["personnummer"] == []
        assert result["emails"] == []

    def test_has_pii_true_when_something_found(self):
        result = pii.detect_all("Contact jane.doe@example.com for questions.")
        assert result["has_pii"] is True
        assert result["emails"] == ["jane.doe@example.com"]


def _luhn_complete(prefix: str) -> str:
    """prefix plus the standard Luhn check digit (bankgiro/plusgiro)."""
    return prefix + pii._luhn_check_digit(prefix)


class TestNormalizePersonnummer:
    TODAY = date(2026, 9, 16)

    def test_twelve_digit_form_is_kept(self):
        assert pii.normalize_personnummer("198001011234", today=self.TODAY) == "198001011234"
        assert pii.normalize_personnummer("19800101-1234", today=self.TODAY) == "198001011234"

    def test_ten_digit_form_gets_the_most_recent_past_century(self):
        assert pii.normalize_personnummer("800101-1234", today=self.TODAY) == "198001011234"
        assert pii.normalize_personnummer("8001011234", today=self.TODAY) == "198001011234"

    def test_year_not_yet_reached_belongs_to_the_previous_century(self):
        # "30" in 2026 can only mean 1930, not 2030.
        assert pii.normalize_personnummer("300101-1234", today=self.TODAY) == "193001011234"

    def test_current_year_is_this_century(self):
        assert pii.normalize_personnummer("260101-1234", today=self.TODAY) == "202601011234"

    def test_plus_separator_means_a_hundred_years_older(self):
        assert pii.normalize_personnummer("200101+1234", today=self.TODAY) == "192001011234"

    def test_non_personnummer_is_none(self):
        assert pii.normalize_personnummer("hello", today=self.TODAY) is None

    def test_find_normalized_collapses_both_spellings_of_one_person(self):
        number = _valid_personnummer("800101123")
        text = f"a {number[:6]}-{number[6:]} b 19{number} c"
        assert pii.find_personnummer(text) == sorted({f"{number[:6]}-{number[6:]}", "19" + number})
        assert pii.find_personnummer_normalized(text, today=self.TODAY) == ["19" + number]

    def test_detect_all_carries_the_normalized_form(self):
        number = _valid_personnummer("800101123")
        result = pii.detect_all(f"id {number}")
        assert result["personnummer"] == [number]
        assert result["personnummer_normalized"] == ["19" + number]


class TestOrganisationsnummer:
    def test_valid_number_is_found_and_normalized(self):
        number = _valid_personnummer("556000123")  # "month" 60 -> a company
        assert pii.find_organisationsnummer(f"Org.nr {number[:6]}-{number[6:]}") == [number]
        assert pii.find_organisationsnummer(f"16{number}") == [number]

    def test_wrong_check_digit_is_rejected(self):
        number = _valid_personnummer("556000123")
        wrong = number[:-1] + str((int(number[-1]) + 1) % 10)
        assert pii.find_organisationsnummer(wrong) == []

    def test_a_personnummer_is_not_an_organisationsnummer(self):
        number = _valid_personnummer("800101123")
        assert pii.find_organisationsnummer(number) == []
        assert pii.find_personnummer(number) == [number]


class TestBankgiroPlusgiro:
    def test_bankgiro_luhn_valid(self):
        digits = _luhn_complete("5050105")
        written = f"{digits[:4]}-{digits[4:]}"
        assert pii.find_bankgiro(f"Bankgiro: {written}") == [written]

    def test_bankgiro_wrong_check_digit_rejected(self):
        digits = _luhn_complete("5050105")
        wrong = digits[:-1] + str((int(digits[-1]) + 1) % 10)
        assert pii.find_bankgiro(f"{wrong[:4]}-{wrong[4:]}") == []

    def test_bankgiro_requires_the_separator(self):
        digits = _luhn_complete("5050105")
        assert pii.find_bankgiro(digits) == []

    def test_plusgiro_luhn_valid(self):
        digits = _luhn_complete("1234567")
        written = f"{digits[:-1]}-{digits[-1]}"
        assert pii.find_plusgiro(f"Plusgiro {written}") == [written]

    def test_plusgiro_wrong_check_digit_rejected(self):
        digits = _luhn_complete("1234567")
        wrong = digits[:-1] + str((int(digits[-1]) + 1) % 10)
        assert pii.find_plusgiro(f"{wrong[:-1]}-{wrong[-1]}") == []


def _valid_fodselsnummer(ddmmyy: str) -> str:
    """The first individual number on this date for which both mod-11
    check digits exist - searched for, not memorized."""
    for individual in range(1000):
        base = f"{ddmmyy}{individual:03d}"
        k1 = pii._mod11_check_digit(base, pii._FODSELSNUMMER_K1_WEIGHTS)
        if k1 is None:
            continue
        k2 = pii._mod11_check_digit(base + str(k1), pii._FODSELSNUMMER_K2_WEIGHTS)
        if k2 is None:
            continue
        return base + str(k1) + str(k2)
    raise AssertionError("no valid number on that date")


class TestFodselsnummer:
    def test_valid_number_is_found(self):
        number = _valid_fodselsnummer("010180")
        assert pii.find_fodselsnummer(f"fnr {number} ok") == [number]

    def test_d_number_day_plus_forty_is_valid(self):
        number = _valid_fodselsnummer("410180")
        assert pii.find_fodselsnummer(number) == [number]

    def test_either_check_digit_wrong_is_rejected(self):
        number = _valid_fodselsnummer("010180")
        wrong_k2 = number[:-1] + str((int(number[-1]) + 1) % 10)
        wrong_k1 = number[:-2] + str((int(number[-2]) + 1) % 10) + number[-1]
        assert pii.find_fodselsnummer(wrong_k2) == []
        assert pii.find_fodselsnummer(wrong_k1) == []

    def test_invalid_date_is_rejected(self):
        assert pii.fodselsnummer_valid("32138012345") is False


def _valid_hetu(ddmmyy: str, sign: str, nnn: str) -> str:
    return ddmmyy + sign + nnn + pii.HETU_CHECK_CHARS[int(ddmmyy + nnn) % 31]


class TestHetu:
    def test_valid_number_is_found(self):
        number = _valid_hetu("010180", "-", "123")
        assert pii.find_hetu(f"hetu {number}") == [number]

    def test_new_century_signs_are_accepted(self):
        number = _valid_hetu("010105", "A", "321")
        assert pii.find_hetu(number) == [number]
        number = _valid_hetu("010180", "Y", "321")
        assert pii.find_hetu(number) == [number]

    def test_wrong_check_character_is_rejected(self):
        number = _valid_hetu("010180", "-", "123")
        wrong = number[:-1] + ("A" if number[-1] != "A" else "B")
        assert pii.find_hetu(wrong) == []

    def test_invalid_date_is_rejected(self):
        assert pii.hetu_valid(_valid_hetu("321380", "-", "123")) is False


class TestCpr:
    def test_shape_with_a_real_date_is_found(self):
        # Pick a suffix that is not also a valid Swedish personnummer
        # (the two shapes overlap exactly - see CPR_RE).
        for suffix in range(1000, 1100):
            candidate = f"010180-{suffix}"
            if not pii.find_personnummer(candidate):
                break
        assert pii.find_cpr(f"CPR {candidate}") == [candidate]

    def test_invalid_date_is_rejected(self):
        assert pii.find_cpr("321380-1234") == []

    def test_requires_the_separator(self):
        assert pii.find_cpr("0101801234") == []

    def test_valid_swedish_personnummer_is_not_reported_as_cpr(self):
        number = _valid_personnummer("010120123")  # 2001-01-20 in Swedish terms, and a CPR shape
        written = f"{number[:6]}-{number[6:]}"
        assert pii.find_personnummer(written) == [written]
        assert pii.find_cpr(written) == []


class TestDetectAllNordic:
    def test_new_types_count_toward_has_pii(self):
        number = _valid_fodselsnummer("010180")
        result = pii.detect_all(f"x {number} y")
        assert result["has_pii"] is True
        assert result["fodselsnummer"] == [number]

    def test_every_expected_key_is_present(self):
        result = pii.detect_all("nothing here")
        for key in (
            "personnummer",
            "personnummer_normalized",
            "emails",
            "phone_numbers",
            "ibans",
            "card_numbers",
            "organisationsnummer",
            "bankgiro",
            "plusgiro",
            "fodselsnummer",
            "hetu",
            "cpr",
        ):
            assert result[key] == []
        assert result["has_pii"] is False
