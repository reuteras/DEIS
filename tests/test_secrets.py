"""Tests for bin/secretscan.py's credential and infrastructure-artifact
detectors (items 49/52).

Private-key fixtures are assembled at runtime ("-----BEGIN " + "RSA
PRIVATE KEY-----") so the repo's own detect-private-key pre-commit hook
does not trip on this file. Bitcoin fixtures are the published BIP173/
BIP350 test vectors and the genesis-block address - never real discovered
data. Every token-shaped fixture is a made-up value of the right shape.
"""

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("deis_secretscan", REPO_ROOT / "bin" / "secretscan.py")
secretscan = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = secretscan
spec.loader.exec_module(secretscan)


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _jwt(alg: str = "HS256") -> str:
    return f"{_b64url({'alg': alg, 'typ': 'JWT'})}.{_b64url({'sub': '1234567890'})}.abcdefghijklmnop"


class TestPrivateKeys:
    def test_records_the_header_kind_not_the_material(self):
        text = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END " + "RSA PRIVATE KEY-----"
        assert secretscan.find_private_keys(text) == ["RSA PRIVATE KEY"]

    def test_openssh_and_pgp_blocks(self):
        text = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nx\n-----BEGIN " + "PGP PRIVATE KEY BLOCK-----"
        assert secretscan.find_private_keys(text) == ["OPENSSH PRIVATE KEY", "PGP PRIVATE KEY BLOCK"]

    def test_certificate_or_public_key_is_not_reported(self):
        assert secretscan.find_private_keys("-----BEGIN CERTIFICATE-----\n-----BEGIN PUBLIC KEY-----") == []

    def test_putty_key_file(self):
        assert secretscan.find_private_keys("PuTTY-User-Key-File-3: ssh-ed25519\n") == ["PuTTY ssh-ed25519"]


class TestTokens:
    def test_aws_access_key(self):
        assert secretscan.find_aws_access_keys("key=AKIAIOSFODNN7EXAMPLE end") == ["AKIAIOSFODNN7EXAMPLE"]

    def test_aws_wrong_length_is_rejected(self):
        assert secretscan.find_aws_access_keys("AKIAIOSFODNN7EXAMPL") == []

    def test_github_token(self):
        token = "ghp_" + "A" * 36
        assert secretscan.find_github_tokens(f"token {token} x") == [token]

    def test_slack_token(self):
        assert secretscan.find_slack_tokens("xoxb-123456789012-abcdefghijkl") == ["xoxb-123456789012-abcdefghijkl"]

    def test_google_api_key(self):
        key = "AIza" + "a" * 35
        assert secretscan.find_google_api_keys(key) == [key]

    def test_jwt_with_a_real_header(self):
        token = _jwt()
        assert secretscan.find_jwts(f"Authorization: Bearer {token}") == [token]

    def test_jwt_shaped_string_without_alg_header_is_rejected(self):
        bogus = f"{_b64url({'foo': 'bar', 'padding': 'x'})}.{_b64url({'sub': 1})}.abcdefghijklmnop"
        assert bogus.startswith("eyJ")
        assert secretscan.find_jwts(bogus) == []


class TestCredentialUrls:
    def test_url_with_user_and_password(self):
        assert secretscan.find_credential_urls("db: postgres://app:s3cret@db.internal:5432/app.") == [
            "postgres://app:s3cret@db.internal:5432/app"
        ]

    def test_url_with_only_a_username_is_not_a_secret(self):
        assert secretscan.find_credential_urls("ssh://deploy@host/repo") == []


class TestPasswordAssignments:
    def test_assignment_is_kept_as_written(self):
        assert secretscan.find_password_assignments("Password = Winter2024!") == ["Password = Winter2024!"]

    def test_swedish_key(self):
        assert secretscan.find_password_assignments("Lösenord: hemligt123") == ["Lösenord: hemligt123"]

    @pytest.mark.parametrize(
        "text",
        ["password=********", "password: <password>", "pwd=${DB_PASSWORD}", "password = null", "password: changeme"],
    )
    def test_placeholders_are_skipped(self, text):
        assert secretscan.find_password_assignments(text) == []

    def test_plain_word_without_a_value_is_not_reported(self):
        assert secretscan.find_password_assignments("Enter your password below") == []


class TestDetectSecrets:
    def test_has_secrets_false_when_nothing_found(self):
        result = secretscan.detect_secrets("quarterly report, nothing to see")
        assert result["has_secrets"] is False
        assert all(value == [] for key, value in result.items() if key != "has_secrets")

    def test_has_secrets_true_when_something_found(self):
        assert secretscan.detect_secrets("AKIAIOSFODNN7EXAMPLE")["has_secrets"] is True


class TestIpAddresses:
    def test_public_and_private_are_split(self):
        public, private = secretscan.find_ipv4_addresses("from 192.168.1.10 to 8.8.8.8 and 10.0.0.1")
        assert public == ["8.8.8.8"]
        assert private == ["10.0.0.1", "192.168.1.10"]

    def test_out_of_range_octet_is_rejected(self):
        assert secretscan.find_ipv4_addresses("999.1.1.1") == ([], [])

    def test_version_string_with_four_parts_is_not_an_address(self):
        assert secretscan.find_ipv4_addresses("v1.2.3.4.5") == ([], [])

    def test_ipv6(self):
        assert secretscan.find_ipv6_addresses("host 2001:db8::1 and fe80::1%eth0") == ["2001:db8::1", "fe80::1"]

    def test_mac_address_and_timestamp_are_not_ipv6(self):
        assert secretscan.find_ipv6_addresses("00:1a:2b:3c:4d:5e at 12:30:45") == []


class TestPathsAndUsers:
    def test_unc_path_keeps_server_and_share_only(self):
        assert secretscan.find_unc_paths(r"copy \\FILESRV01\Finance\2024\budget.xlsx") == [r"\\FILESRV01\Finance"]

    def test_windows_user_path(self):
        assert secretscan.find_usernames(r"C:\Users\anna.svensson\Desktop\x.docx") == ["anna.svensson"]

    def test_unix_home_paths(self):
        assert secretscan.find_usernames("/home/erik/notes and /Users/lisa/Documents") == ["erik", "lisa"]

    def test_domain_user(self):
        assert secretscan.find_usernames(r"logon by CORP\jdoe at") == ["CORP\\jdoe"]

    def test_generic_profile_names_are_skipped(self):
        assert secretscan.find_usernames(r"C:\Users\Public\Desktop C:\Users\Default\x") == []


class TestDomainsAndOnion:
    def test_hostnames_from_urls(self):
        text = "see https://Portal.Example.com/login and http://mail.example.org:8080/x"
        assert secretscan.find_domains(text) == ["mail.example.org", "portal.example.com"]

    def test_onion_addresses(self):
        v3 = "a" * 56 + ".onion"
        text = f"http://{v3}/ and http://abcdefghijklmnop.onion/x"
        assert secretscan.find_onion_addresses(text) == sorted(["abcdefghijklmnop.onion", v3])
        assert secretscan.find_domains(text) == []


class TestBitcoin:
    @pytest.mark.parametrize(
        "address",
        [
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",  # genesis block P2PKH
            "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",  # P2SH
            "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq",  # BIP173 vector
            "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",  # BIP350 vector
        ],
    )
    def test_checksum_valid_addresses_are_found(self, address):
        assert secretscan.find_bitcoin_addresses(f"pay to {address} now") == [address]

    @pytest.mark.parametrize(
        "address",
        [
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNb",  # last char changed
            "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdr",
            "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3",  # right shape, wrong checksum
        ],
    )
    def test_checksum_invalid_addresses_are_rejected(self, address):
        assert secretscan.find_bitcoin_addresses(address) == []


class TestDetectArtifacts:
    def test_has_artifacts_false_when_nothing_found(self):
        assert secretscan.detect_artifacts("plain prose")["has_artifacts"] is False

    def test_all_keys_present(self):
        result = secretscan.detect_artifacts(r"\\srv\share 10.0.0.5")
        for key in (
            "ipv4_addresses",
            "private_ipv4_addresses",
            "ipv6_addresses",
            "unc_paths",
            "usernames",
            "domains",
            "onion_addresses",
            "bitcoin_addresses",
        ):
            assert key in result
        assert result["has_artifacts"] is True
