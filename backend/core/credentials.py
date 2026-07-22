"""Canonical credential resolution (CVM-Inc-3 post-rotation hardening, WS1 + WS3).

WHY THIS EXISTS
---------------
The 2026-07-22 bridge-token rotation exposed a latent coupling: ``mt5_validate_worker`` resolved its
worker credential as ``MT5_WORKER_TOKEN or GUVFX_WORKER_TOKEN or GUVFX_AGENT_TOKEN``. It had only the
*agent* token, so it silently authenticated with the **bridge's** credential. That worked solely because
the two secrets happened to hold the same value; rotating one broke it instantly.

THE RULE (permanent governance): a service may never silently substitute another service's credential.

WHAT IS ALLOWED
---------------
* **Aliases** — several environment NAMES for the SAME logical secret (e.g. the bridge agent token is read
  as ``GUVFX_AGENT_TOKEN`` / ``GUVFX_WINDOWS_AGENT_TOKEN`` / ``WINDOWS_AGENT_TOKEN`` across deployments).
  Aliases are declared explicitly and, if more than one is set, they **must agree** — disagreement is a
  misconfiguration and fails closed rather than picking one.

WHAT IS FORBIDDEN
-----------------
* Falling back from one logical secret to a **different** one. There is no API to express that here.

FAILURE BEHAVIOUR
-----------------
Missing / empty / whitespace-only / placeholder text raises :class:`CredentialError` with a diagnostic that
names the secret, its purpose and every name checked — so the operator learns the problem at STARTUP rather
than when live traffic hits an auth boundary.
"""
from __future__ import annotations

import os

__all__ = ["CredentialError", "resolve_secret", "is_placeholder", "PLACEHOLDER_MARKERS"]


class CredentialError(RuntimeError):
    """A required credential is absent, empty, placeholder text, or its aliases disagree."""


# Substrings that indicate example/scaffold text rather than a real secret. Deliberately broad: a false
# positive fails a service closed at startup with a clear message, which is far safer than a service
# authenticating with the literal string "changeme".
PLACEHOLDER_MARKERS = (
    "replace", "changeme", "change_me", "example", "your-", "your_", "xxx", "placeholder",
    "dummy", "sample", "todo", "fixme", "notreal", "fake", "redacted", "scrubbed",
    "<", ">", "${", "$(",
)


def is_placeholder(value: str) -> bool:
    """True if ``value`` looks like scaffold/example text rather than a real secret."""
    low = (value or "").lower()
    return any(marker in low for marker in PLACEHOLDER_MARKERS)


def resolve_secret(
    primary: str,
    *,
    aliases: tuple[str, ...] = (),
    purpose: str = "",
    min_length: int = 1,
    env: dict | None = None,
) -> str:
    """Resolve exactly ONE logical secret. Never substitutes a different credential.

    :param primary: canonical environment variable name.
    :param aliases: additional NAMES for the SAME secret (deployment history). If several are set they
        must hold identical values.
    :param purpose: short human description used in the diagnostic.
    :param min_length: reject values shorter than this (catches truncated pastes).
    :raises CredentialError: with an operator-actionable message. The secret VALUE is never included.
    """
    env = os.environ if env is None else env
    names = (primary,) + tuple(aliases)
    present = {n: (env.get(n) or "").strip() for n in names}
    found = {n: v for n, v in present.items() if v}

    what = f"{primary}" + (f" ({purpose})" if purpose else "")
    if not found:
        raise CredentialError(
            f"{what} is not configured. Checked: {', '.join(names)}. "
            f"Set the secret in the deployment environment; the service refuses to start without it "
            f"(it will not fall back to another service's credential)."
        )

    distinct = set(found.values())
    if len(distinct) > 1:
        # Never guess which one is right — disagreement means the deployment is inconsistent.
        raise CredentialError(
            f"{what}: aliases disagree ({', '.join(sorted(found))} hold different values). "
            f"These names must all refer to the SAME secret. Refusing to guess."
        )

    value = distinct.pop()
    if is_placeholder(value):
        raise CredentialError(
            f"{what} looks like placeholder/example text, not a real secret. "
            f"Provide the real value in the deployment environment."
        )
    if len(value) < min_length:
        raise CredentialError(
            f"{what} is too short ({len(value)} chars, minimum {min_length}) — likely truncated."
        )
    return value
