"""Secret and infrastructure-artifact detection (docs/IMPROVEMENTS.md
items 49 and 52): what a leak dump reveals about the victim's own systems
and credentials, as opposed to the personal identifiers bin/pii.py finds.

Two detector groups, applied by `deis secret-scan` to every indexed
document's Tika-extracted text:

- detect_secrets(): credentials. Private-key headers, cloud/API tokens
    with a recognizable fixed prefix (AWS, GitHub, Slack, Google), JWTs
    (validated by decoding their header), URLs carrying user:password@, and
    password=... style assignments. Values are stored in full, the same
    deliberate choice pii.py makes - the point is to find every document
    that exposes one specific credential, and that needs the value.
- detect_artifacts(): infrastructure and identity. IPv4/IPv6 addresses
    (split into public and private ranges), UNC paths, usernames lifted
    from Windows/Unix home-directory paths and DOMAIN\\user references,
    hostnames from URLs, .onion addresses, and Bitcoin addresses
    (base58check / bech32 validated, so a random string is not reported).
    Together they answer "what did this dump come from" - and the .onion
    and Bitcoin ones in particular are what a ransom note carries.

Every detector that has a checksum uses it. The two that cannot (password
assignments, and IP addresses beyond octet range) are the noisy ones, and
are documented as leads rather than proof, the same way pii.py's phone
numbers are.

Pure functions only - no network, no Elasticsearch, stdlib only.
"""

import base64
import hashlib
import ipaddress
import json
import re

# ----------------------------------------------------------------------------
# Secrets

# Header text is what gets recorded ("RSA PRIVATE KEY", "OPENSSH PRIVATE
# KEY", "PGP PRIVATE KEY BLOCK"), never the key material after it.
PRIVATE_KEY_RE = re.compile(r"-----BEGIN ((?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?)-----")
PUTTY_KEY_RE = re.compile(r"\bPuTTY-User-Key-File-\d+: (\S+)")
AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
GITHUB_TOKEN_RE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b")
SLACK_TOKEN_RE = re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,255}\b")
GOOGLE_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
# scheme://user:password@host... - the password is what makes it a secret,
# so a URL with only a username before the @ is not matched.
CREDENTIAL_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@'\"<>]+:[^\s/@'\"<>]+@[^\s'\"<>]+")
# key = value / key: value, the key drawn from the names that reliably
# mean "a credential follows" (in English and Swedish). Deliberately not
# "secret"/"token"/"key" alone - too many ordinary sentences contain them.
PASSWORD_ASSIGNMENT_RE = re.compile(
    r"\b(password|passwd|pwd|passphrase|l[öo]senord|api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token)"
    r"\s*[:=]\s*[\"']?([^\s\"',;<>]{4,})",
    re.IGNORECASE,
)
# Values that are the word itself, a placeholder, or a template variable.
_PLACEHOLDER_VALUES = {
    "password",
    "passwd",
    "pwd",
    "null",
    "none",
    "true",
    "false",
    "yes",
    "no",
    "required",
    "optional",
    "string",
    "redacted",
    "hidden",
    "changeme",
    "example",
}
_PLACEHOLDER_RE = re.compile(r"^(?:[*x#_.-]+|<[^>]*>|\[[^\]]*\]|\{\{.*\}\}|\$\{.*\}|\$\w+|%\w+%)$", re.IGNORECASE)
_MAX_ASSIGNMENT_CHARS = 120


def find_private_keys(text: str) -> list[str]:
    found = {match.group(1) for match in PRIVATE_KEY_RE.finditer(text)}
    found |= {f"PuTTY {match.group(1)}" for match in PUTTY_KEY_RE.finditer(text)}
    return sorted(found)


def find_aws_access_keys(text: str) -> list[str]:
    return sorted(set(AWS_ACCESS_KEY_RE.findall(text)))


def find_github_tokens(text: str) -> list[str]:
    return sorted(set(GITHUB_TOKEN_RE.findall(text)))


def find_slack_tokens(text: str) -> list[str]:
    return sorted(set(SLACK_TOKEN_RE.findall(text)))


def find_google_api_keys(text: str) -> list[str]:
    return sorted(set(GOOGLE_API_KEY_RE.findall(text)))


def _jwt_header_valid(token: str) -> bool:
    """A real JWT's first segment is base64url JSON with an "alg" claim -
    the check that separates one from any other dotted base64-ish run
    that happens to start with "eyJ" ("{" base64-encoded).
    """
    header = token.split(".", 1)[0]
    try:
        padded = header + "=" * (-len(header) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(decoded, dict) and "alg" in decoded


def find_jwts(text: str) -> list[str]:
    return sorted({token for token in JWT_RE.findall(text) if _jwt_header_valid(token)})


def find_credential_urls(text: str) -> list[str]:
    return sorted({match.group(0).rstrip(".,;)") for match in CREDENTIAL_URL_RE.finditer(text)})


def find_password_assignments(text: str) -> list[str]:
    """password=... style assignments, as written (key, separator and
    value), minus obvious placeholders. Unvalidated - a config file that
    says "Password: see vault" is reported too - so a hit is a lead, not
    proof, same as pii.py's phone numbers.
    """
    found = set()
    for match in PASSWORD_ASSIGNMENT_RE.finditer(text):
        value = match.group(2)
        if value.lower() in _PLACEHOLDER_VALUES or _PLACEHOLDER_RE.match(value):
            continue
        found.add(match.group(0)[:_MAX_ASSIGNMENT_CHARS])
    return sorted(found)


def detect_secrets(text: str) -> dict:
    """Runs every credential detector; returns a dict ready to be merged
    into a document's "secrets" field - see bin/deis.py's cmd_secret_scan.
    """
    result = {
        "private_keys": find_private_keys(text),
        "aws_access_keys": find_aws_access_keys(text),
        "github_tokens": find_github_tokens(text),
        "slack_tokens": find_slack_tokens(text),
        "google_api_keys": find_google_api_keys(text),
        "jwts": find_jwts(text),
        "credential_urls": find_credential_urls(text),
        "password_assignments": find_password_assignments(text),
    }
    result["has_secrets"] = any(result.values())
    return result


# ----------------------------------------------------------------------------
# Infrastructure and identity artifacts

IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# Loose candidate shape; ipaddress does the real validation (eight groups
# or a "::", hex only), which also rejects MAC addresses and timestamps.
IPV6_CANDIDATE_RE = re.compile(r"(?<![\w:.])(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}(?![\w:.])")
UNC_PATH_RE = re.compile(r"\\\\([A-Za-z0-9._-]{1,63})\\([^\s\\\"'<>|:*?]{1,80})")
WINDOWS_USER_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:Users|Documents and Settings)\\([^\\\s\"'<>|:*?]{1,64})")
UNIX_HOME_PATH_RE = re.compile(r"(?<![\w/])/(?:home|Users)/([A-Za-z][A-Za-z0-9._-]{0,31})/")
DOMAIN_USER_RE = re.compile(r"\b([A-Z][A-Z0-9-]{1,15})\\([a-z][a-z0-9._-]{2,30})\b")
URL_HOST_RE = re.compile(r"\bhttps?://([A-Za-z0-9.-]+\.[A-Za-z]{2,63})(?::\d+)?(?=[/\s\"'<>)]|$)")
ONION_RE = re.compile(r"\b((?:[a-z2-7]{16}|[a-z2-7]{56})\.onion)\b")
BITCOIN_LEGACY_RE = re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b")
BITCOIN_BECH32_RE = re.compile(r"\bbc1[ac-hj-np-z02-9]{25,87}\b")

# Home-directory names that are not a person.
_NOT_A_USER = {"public", "default", "default user", "all users", "administrator", "guest", "shared"}

_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def find_ipv4_addresses(text: str) -> tuple[list[str], list[str]]:
    """(public, private) IPv4 addresses - private covers RFC 1918, loopback,
    link-local and the other non-global ranges ipaddress knows. Both kept:
    the private ones map the victim's own network, the public ones are
    what it talked to."""
    public, private = set(), set()
    for candidate in IPV4_RE.findall(text):
        try:
            address = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if address.is_unspecified or address.is_multicast or address.is_reserved or candidate == "255.255.255.255":
            continue
        (public if address.is_global else private).add(candidate)
    return sorted(public), sorted(private)


def find_ipv6_addresses(text: str) -> list[str]:
    found = set()
    for candidate in IPV6_CANDIDATE_RE.findall(text):
        try:
            address = ipaddress.IPv6Address(candidate)
        except ValueError:
            continue
        if address.is_unspecified or address.is_multicast:
            continue
        found.add(address.compressed)
    return sorted(found)


def find_unc_paths(text: str) -> list[str]:
    """\\\\server\\share - the first two components only, which is the
    part that names a machine and a share rather than a file."""
    return sorted({f"\\\\{server}\\{share}" for server, share in UNC_PATH_RE.findall(text)})


def find_usernames(text: str) -> list[str]:
    """Account names lifted from home-directory paths (C:\\Users\\<name>,
    /home/<name>, /Users/<name>) and DOMAIN\\user references - a person's
    identity as their own machine records it, which no NER model finds."""
    found = set()
    for name in WINDOWS_USER_PATH_RE.findall(text) + UNIX_HOME_PATH_RE.findall(text):
        if name.lower() not in _NOT_A_USER:
            found.add(name)
    for domain, user in DOMAIN_USER_RE.findall(text):
        found.add(f"{domain}\\{user}")
    return sorted(found)


def find_domains(text: str) -> list[str]:
    """Hostnames from http(s) URLs, lowercased, .onion ones excluded (see
    find_onion_addresses)."""
    return sorted({host.lower() for host in URL_HOST_RE.findall(text) if not host.lower().endswith(".onion")})


def find_onion_addresses(text: str) -> list[str]:
    return sorted(set(ONION_RE.findall(text.lower())))


def _base58check_valid(address: str) -> bool:
    number = 0
    for char in address:
        number = number * 58 + _BASE58_ALPHABET.index(char)
    raw = number.to_bytes(25, "big")
    if raw[0] not in (0x00, 0x05):  # P2PKH / P2SH version bytes
        return False
    checksum = hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4]
    return raw[21:] == checksum


def _bech32_polymod(values: list[int]) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for i in range(5):
            if (top >> i) & 1:
                checksum ^= generator[i]
    return checksum


def _bech32_valid(address: str) -> bool:
    """BIP173 (bech32, witness v0) or BIP350 (bech32m, v1+) checksum."""
    hrp, _, data = address.partition("1")
    if hrp != "bc" or not data:
        return False
    try:
        values = [_BECH32_CHARSET.index(char) for char in data]
    except ValueError:
        return False
    hrp_expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    polymod = _bech32_polymod(hrp_expanded + values)
    witness_version = values[0]
    return polymod == (1 if witness_version == 0 else 0x2BC830A3)


def find_bitcoin_addresses(text: str) -> list[str]:
    """Bitcoin addresses that pass their own checksum - legacy base58check
    (1..., 3...) or bech32/bech32m (bc1...). A ransom note's payment
    address is the usual source."""
    found = set()
    for candidate in BITCOIN_LEGACY_RE.findall(text):
        try:
            if _base58check_valid(candidate):
                found.add(candidate)
        except OverflowError:
            continue
    for candidate in BITCOIN_BECH32_RE.findall(text):
        if _bech32_valid(candidate):
            found.add(candidate)
    return sorted(found)


def detect_artifacts(text: str) -> dict:
    """Runs every infrastructure/identity detector; returns a dict ready to
    be merged into a document's "artifacts" field - see bin/deis.py's
    cmd_secret_scan, which writes both groups in one pass.
    """
    public_ipv4, private_ipv4 = find_ipv4_addresses(text)
    result = {
        "ipv4_addresses": public_ipv4,
        "private_ipv4_addresses": private_ipv4,
        "ipv6_addresses": find_ipv6_addresses(text),
        "unc_paths": find_unc_paths(text),
        "usernames": find_usernames(text),
        "domains": find_domains(text),
        "onion_addresses": find_onion_addresses(text),
        "bitcoin_addresses": find_bitcoin_addresses(text),
    }
    result["has_artifacts"] = any(result.values())
    return result
