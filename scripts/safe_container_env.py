#!/usr/bin/env python3
"""Safe reader for a container's / process's environment variables.

WHY THIS EXISTS
---------------
An operator env probe once leaked two production ``*_KEYRING`` secret values into a tool
transcript: the ad-hoc redaction (``s/(KEY|TOKEN|SECRET|PASSWORD)=.*/.../``) only matched a
keyword *immediately* before ``=``, so a compound name like ``HOSTED_EXECUTOR_KEYRING=`` (KEY +
RING + =) slipped through, and the probe dumped the *whole* environment with a deny-list. Both
choices are unsafe: a deny-list misses any unforeseen secret-bearing name, and a full dump
exposes everything.

DESIGN — two independent layers, so a secret can NEVER reach stdout
------------------------------------------------------------------
1. **ALLOW-LIST**: only variable names passed explicitly on the command line are considered.
   The full environment is never enumerated or dumped.
2. **NAME-DENY (defence in depth)**: even an allow-listed name is redacted if its NAME matches
   the secret-name pattern below. A value is printed only when the name is BOTH allow-listed AND
   does not match the secret-name pattern.

The value itself is never inspected, hashed, or used to decide redaction — only the NAME is.

USAGE
-----
    python3 scripts/safe_container_env.py GUVFX_GIT_COMMIT GUVFX_RELEASE_ID
    # inside a container:
    docker exec <container> python3 scripts/safe_container_env.py GUVFX_GIT_COMMIT ...

Never pipe its output anywhere a secret must not go without re-reading it first; by construction
it emits no secret value, but it is still env data.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Iterable, Mapping, Optional

# A NAME containing any of these (case-insensitive) is treated as secret-bearing and redacted,
# regardless of position in the name (prefix/suffix/compound). Deliberately broad.
_SECRET_NAME = re.compile(
    r"(KEY|KEYRING|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|SIGNING|HMAC|PRIVATE|CERT|SALT|SEED|"
    r"AUTH|SESSION|COOKIE|BEARER|API_?KEY|ACCESS)",
    re.IGNORECASE,
)

REDACTED = "<redacted:secret-named>"
UNSET = "<unset>"


def is_secret_name(name: str) -> bool:
    """True iff the variable NAME looks secret-bearing (value is never consulted)."""
    return bool(_SECRET_NAME.search(name or ""))


def safe_env_lines(names: Iterable[str], env: Optional[Mapping[str, str]] = None) -> list[str]:
    """Return ``NAME=value`` lines for ONLY the allow-listed ``names``.

    A secret-named variable is emitted as ``NAME=<redacted:secret-named>`` and its value is never
    read. A name not present in the environment is emitted as ``NAME=<unset>``. The full
    environment is never enumerated.
    """
    src = os.environ if env is None else env
    out: list[str] = []
    for name in names:
        if is_secret_name(name):
            out.append(f"{name}={REDACTED}")      # value never touched
        elif name in src:
            out.append(f"{name}={src[name]}")
        else:
            out.append(f"{name}={UNSET}")
    return out


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(
            "usage: safe_container_env.py NAME [NAME ...]\n"
            "  Prints ONLY the named variables (allow-list); never dumps the full environment;\n"
            "  redacts any secret-named variable by name.\n"
        )
        return 2
    for line in safe_env_lines(argv):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
