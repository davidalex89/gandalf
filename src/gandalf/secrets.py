"""No secrets in the repo (D-120): find anything in planned output that looks like a real one.

A plan may name a secret's setting (``SMTP_URL``) and use empty or obviously fake values
in examples (``<your-api-key>``, ``user:secret@mail.example.com``, ``test-password``).
Anything shaped like a real credential is rejected, so it can never reach a PR spec and,
from there, the repo.
"""

from __future__ import annotations

import re

# Well-known credential formats.
FORMATS = [
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "an AWS access key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "a GitHub token"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "a Slack token"),
    (re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{12,}"), "a Slack webhook address"),
    (re.compile(r"\b[rs]k_live_[0-9A-Za-z]{16,}"), "a Stripe live key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "a Google API key"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "a signed token (JWT)"),
]
# user:password@host in an address
URL_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:([^/\s@]+)@([^/\s:?#]+)", re.I)
# password = "value", api_key: 'value' …
# The key may end an env-style name (DB_PASSWORD, AWS_SECRET_ACCESS_KEY) and the value may be
# unquoted, as in a .env line or YAML (0.12.0 QA review, finding 2; D-168).
ASSIGNMENT = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z0-9_]*_)?(password|passwd|passcode|pwd|secret|secret_key|api_key|apikey|"
    r"access_key|auth_token|token|private_key|client_secret)(?![A-Za-z0-9])[\"']?\s*(?:=>|[:=])\s*"
    r"(?:[\"']([^\"'\s]+)[\"']|([^\"'\s,;]+))", re.I)
FAKE_HOSTS = re.compile(r"(^|\.)(example\.(com|org|net)|example|test|invalid)$", re.I)
FAKE_WORDS = ("example", "test", "dummy", "fake", "placeholder", "sample", "your", "redacted", "xxx", "***")
DEFAULT_CREDENTIALS = {"admin", "password", "root", "123456", "12345678", "changeme", "letmein", "qwerty",
                       "default", "pass", "secret"}


def _placeholder(value: str) -> bool:
    v = value.lower()
    return (not v or v[0] in "<${%" or v.startswith("{{") or v.endswith(">")
            or any(word in v for word in FAKE_WORDS)
            or re.fullmatch(r"[a-z]+(-[a-z]+)+", value) is not None)    # a description: url-encoded-password


def find(text: str) -> list[str]:
    """What in ``text`` looks like a real secret (a description of each)."""
    found = [what for pattern, what in FORMATS if pattern.search(text)]
    for m in URL_CREDENTIALS.finditer(text):
        password, host = m.group(1), m.group(2)
        if password.lower() in DEFAULT_CREDENTIALS and not FAKE_HOSTS.search(host):
            found.append("a default password in an address")
        elif not (_placeholder(password) or FAKE_HOSTS.search(host)):
            found.append("a password in an address")
    for m in ASSIGNMENT.finditer(text):
        value = m.group(2) or m.group(3)
        if m.group(3) and re.search(r"[()\[\].]", value):     # unquoted code, not a literal: env("X"), settings.X
            continue
        if value.lower() in DEFAULT_CREDENTIALS:
            found.append(f"a default {m.group(1).lower()}")
        elif len(value) >= 8 and not _placeholder(value):
            found.append(f"a {m.group(1).lower()} value")
    return found


def scan(data, path: str = "$") -> list[str]:
    """Errors for every string in a stage output that looks like a real secret."""
    errors = []
    if isinstance(data, dict):
        for key, value in data.items():
            errors += scan(value, f"{path}.{key}")
    elif isinstance(data, list):
        for i, value in enumerate(data):
            errors += scan(value, f"{path}[{i}]")
    elif isinstance(data, str):
        for what in dict.fromkeys(find(data)):
            errors.append(f"{path}: looks like a real secret ({what}). Never put a password, key, token or "
                          "other secret in the repo, even as a default or example: read it from the repo's "
                          "configuration, and use an empty or obviously fake value in examples and tests")
    return errors
