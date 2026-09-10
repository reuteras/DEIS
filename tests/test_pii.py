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
