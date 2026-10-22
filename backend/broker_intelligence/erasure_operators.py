"""ADR-0054 — per-operator executor LOGIN naming + lifecycle (F-5).

Short-lived executor logins (members of the NOLOGIN ``guvfx_erasure_exec`` group) are the ONLY authenticated
path to the governed erasure procedure. To make the capability rollback reliable, every such login MUST carry
the fixed ``guvfx_erx_`` prefix: ``production_rollback_erasure.sql`` enumerates and drops exactly
``guvfx_erx_%`` and nothing else, so a login minted outside this convention would survive a rollback with its
execute authority intact. This module is the single fail-closed gate for that naming, plus an expiry contract.

No secrets here: the operator password is supplied out of band (env/secret store) to the mint command and is
never logged or placed on argv; the role additionally carries a hard ``VALID UNTIL`` expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

OPERATOR_LOGIN_PREFIX = "guvfx_erx_"
# PostgreSQL identifiers are <= 63 bytes (NAMEDATALEN-1). Lowercase ascii word characters only, so the name is
# safe to interpolate as an identifier and cannot smuggle quoting/whitespace. Anchored with \A ... \Z (NOT $,
# which in Python also matches immediately before a trailing newline) so a suffix like "op1\n" is rejected.
_SUFFIX_RE = re.compile(r"\A[a-z0-9_]{1,40}\Z")
_FULL_RE = re.compile(r"\Aguvfx_erx_[a-z0-9_]{1,40}\Z")


def scram_sha256_verifier(password: str, *, iterations: int = 4096) -> str:
    """Compute a PostgreSQL SCRAM-SHA-256 password verifier from a cleartext password (RFC 5802 / PG storage
    format). The verifier is what gets sent in ``CREATE ROLE ... PASSWORD '<verifier>'`` so the CLEARTEXT never
    enters a SQL statement and therefore cannot leak to the PostgreSQL server log on a statement error or under
    ``log_statement=ddl/all``. The verifier is a salted hash and is NOT reversible to the password."""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return "SCRAM-SHA-256${}:{}${}:{}".format(
        iterations, base64.b64encode(salt).decode("ascii"),
        base64.b64encode(stored_key).decode("ascii"), base64.b64encode(server_key).decode("ascii"))


class OperatorNamingError(ValueError):
    """Raised (fail-closed) when a per-operator executor login name does not conform to the convention."""


def operator_login_name(suffix: str) -> str:
    """Build + validate a per-operator executor login role name from a suffix. Fail-closed on anything that is
    not a short lowercase ``[a-z0-9_]`` token, so the result is always a conforming ``guvfx_erx_<suffix>`` <= 63
    bytes."""
    if not isinstance(suffix, str) or not _SUFFIX_RE.match(suffix or ""):
        raise OperatorNamingError(
            f"operator suffix must match {_SUFFIX_RE.pattern} (lowercase word chars, 1-40); got {suffix!r}")
    name = OPERATOR_LOGIN_PREFIX + suffix
    if len(name.encode("utf-8")) > 63 or not _FULL_RE.match(name):
        raise OperatorNamingError(f"operator login {name!r} is not a conforming guvfx_erx_* identifier (<=63 bytes)")
    return name


def validate_operator_login_name(name: str) -> str:
    """Validate a fully-formed operator login name; fail-closed unless it is a conforming ``guvfx_erx_<suffix>``
    (so the rollback's ``guvfx_erx_%`` enumeration is guaranteed to cover it)."""
    if not isinstance(name, str) or len(name.encode("utf-8")) > 63 or not _FULL_RE.match(name or ""):
        raise OperatorNamingError(
            f"{name!r} is not a conforming per-operator executor login — it MUST be guvfx_erx_<suffix> so the "
            "governed rollback (DROP of guvfx_erx_%) reliably removes its execute authority")
    return name
