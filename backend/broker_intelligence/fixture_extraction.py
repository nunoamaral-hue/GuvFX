"""GENUINE-TEMPLATE fixture extraction (§4-G) — privacy-safe, deterministic, pure.

When a GENUINE broker email is captured, a positive classifier can only be built + certified against its real
structure (§5: "Establish the actual message structure before implementing extraction rules"). But a genuine broker
message carries PII and financial identifiers that MUST NOT be committed to the repository (§5). This module turns a
genuine message into a STRUCTURE-PRESERVING, PII-redacted fixture: the labels, layout, lifecycle wording and amount
*shape* survive (so a parser author can see the template), while a recipient SALUTATION name, email addresses,
account/reference numbers, confirmation URLs and long authorization tokens are masked with stable, length-aware
placeholders. A number written WITH a decimal separator is preserved as an amount; a whole-number run of at least
``min_account_digits`` digits is masked as a possible identifier (privacy-safe default — it may be a large whole
amount, which the surrounding label/currency still makes legible).

What this is NOT:
  * It is NOT the ``redaction`` log-sanitiser — that only masks URLs/opaque tokens in free text destined for logs.
  * It is NOT a positive classifier and emits no ``ExternalWithdrawalSignals``: it reveals a template, it does not
    decide a category. It needs no genuine *external-withdrawal* sample to be correct — it runs on ANY message.
  * It is NOT a complete auto-redactor: a signatory name elsewhere in the body, a short mixed-alphanumeric
    confirmation code, and any surviving digit run are NOT auto-masked — they are SURFACED in the residual-review
    output so a human finishes the redaction. It is a REVIEW AID, never an auto-committer; its output is reviewed by a
    human before any committed fixture is derived (as the genuine TradersWay template was hand-redacted). CRITICAL:
    the residual-review scan is deliberately BROADER than the masker (it does not share the masker's decimal guard),
    so a ``<digits>.<digits>`` identifier the masker leaves as a pseudo-"amount" is still surfaced for review.

Deterministic, pure, no I/O, no DB, no network, holds no authority. A fixture derived from a real message is the
``Provenance.SANITISED`` tier (real email, structure/fields preserved) — distinct from SYNTHETIC and from genuine REAL.
"""
from __future__ import annotations

import re

from .redaction import redact  # masks confirmation URLs + long opaque authorization tokens

# An e-mail address anywhere in the text -> <EMAIL> (mask the whole address incl. local-part + domain).
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# A greeting line's name: "Dear John Smith," / "Hi Jane," -> keep the greeting word, mask up to 4 capitalised name
# tokens that follow, stopping at a comma / end-of-line. Conservative: only fires after an explicit salutation word.
_SALUTATION = re.compile(
    r"\b(Dear|Hi|Hello|Hey)\b([ \t]+)([A-Z][\w'’.\-]*(?:[ \t]+[A-Z][\w'’.\-]*){0,3})")
_NAME_PLACEHOLDER = "<NAME>"
_EMAIL_PLACEHOLDER = "<EMAIL>"

# Default length at/above which a run of consecutive digits is treated as an account / reference identifier (sensitive)
# rather than an ordinary quantity. A typical broker account/reference is >=5 digits; monetary amounts (e.g. "24.12",
# "1000.00", "500") have <=4 digits per run and are PRESERVED so the amount *shape* stays visible in the fixture.
_DEFAULT_MIN_ACCOUNT_DIGITS = 5


def _account_run_re(min_digits: int) -> re.Pattern:
    # A maximal run of >= min_digits digits that is part of neither a larger digit run NOR a DECIMAL number. A '.' is
    # treated as a decimal separator ONLY when it sits between digits: "12345.67" (integer part followed by '.' + a
    # digit) and "1.23456" (fractional part preceded by a digit + '.') are preserved as amounts, while a bare
    # identifier ("51763830", "4113188") and one ending a sentence ("647390.") still mask. So:
    #   (?<!\d)(?<!\d\.) run is not continued from a digit, and not the fractional tail of a decimal
    #   (?!\d)(?!\.\d)   run is not continued by a digit, and not the integer head of a decimal
    return re.compile(r"(?<!\d)(?<!\d\.)\d{%d,}(?!\d)(?!\.\d)" % int(min_digits))


def _residual_run_re(min_digits: int) -> re.Pattern:
    # DELIBERATELY BROADER than the masker: a maximal run of >= min_digits digits with NO decimal exclusion. The
    # masker preserves decimal "amounts" (it skips a run adjacent to a '.'); if it is wrong — a machine reference
    # formatted <digits>.<digits>, or an account number glued to a decimal amount — that identifier survives masking.
    # The review net must NOT share that blind spot, so it surfaces EVERY digit run (amounts included) for a human to
    # judge. Over-surfacing a benign amount is acceptable; missing a sensitive identifier is not.
    return re.compile(r"(?<!\d)\d{%d,}(?!\d)" % int(min_digits))


# A mixed letter+digit run (a possible confirmation code / short token) that redact()'s 20-char log floor may miss.
# Surfaced for human review, not auto-masked. Excludes the module's own UPPER_SNAKE enum tokens (those carry no digit).
_TOKEN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,}")


def sanitise_text(text: str, *, min_account_digits: int = _DEFAULT_MIN_ACCOUNT_DIGITS) -> str:
    """Return ``text`` with PII + financial identifiers masked, preserving transactional STRUCTURE. Order matters:
    e-mail addresses first (so a bare domain is not left behind), then URLs/opaque tokens (``redact``), then the
    salutation name, then long digit runs (account/reference numbers). Pure; never raises on non-str (coerces)."""
    s = "" if text is None else str(text)
    s = _EMAIL.sub(_EMAIL_PLACEHOLDER, s)
    s = redact(s)                                   # confirmation URLs + long opaque authorization tokens
    s = _SALUTATION.sub(lambda m: f"{m.group(1)}{m.group(2)}{_NAME_PLACEHOLDER}", s)
    acct = _account_run_re(min_account_digits)
    s = acct.sub(lambda m: f"<ACCT:{len(m.group(0))}>", s)   # length-aware so template shape is still legible
    return s


def residual_numeric_runs(text: str, *, min_digits: int = 3) -> list[str]:
    """Numeric runs of >= ``min_digits`` left in ``text`` (AFTER sanitisation would normally run) — surfaced so a
    human reviewer can decide whether any remaining number is a sensitive identifier the extractor did not mask
    (defence-in-depth: this tool is a review aid, not the sole redactor). Deduplicated, order-preserving."""
    s = "" if text is None else str(text)
    seen: list[str] = []
    for m in _residual_run_re(min_digits).finditer(s):
        run = m.group(0)
        if run not in seen:
            seen.append(run)
    return seen


def residual_token_candidates(text: str) -> list[str]:
    """Mixed letter+digit runs of >= 6 chars left in ``text`` — possible short confirmation codes that ``redact``'s
    20-char log-sanitiser floor does not reach. Surfaced for human review, never auto-masked. Deduplicated."""
    s = "" if text is None else str(text)
    seen: list[str] = []
    for m in _TOKEN_CANDIDATE.finditer(s):
        run = m.group(0)
        if run not in seen:
            seen.append(run)
    return seen


def sanitise_header_block(*, from_address: str = "", to_addresses=(), subject: str = "",
                          date: str = "", auth_verdict: str = "") -> dict:
    """Sanitise the header fields a fixture author needs to SEE (domains + subject shape), with addresses masked to
    their domain only. Returns a plain dict; never includes a raw recipient address or display name."""
    def _domain(addr: str) -> str:
        addr = (addr or "").strip()
        return addr.split("@", 1)[1] if "@" in addr else _EMAIL_PLACEHOLDER if addr else ""

    return {
        "from_domain": _domain(from_address),
        "to_domains": sorted({_domain(a) for a in (to_addresses or ()) if a}),
        "subject": sanitise_text(subject),
        "date": date or "",
        "auth_verdict": auth_verdict or "",
    }


def extract_fixture(*, from_address: str = "", to_addresses=(), subject: str = "", body: str = "",
                    date: str = "", auth_verdict: str = "",
                    min_account_digits: int = _DEFAULT_MIN_ACCOUNT_DIGITS) -> dict:
    """Build a privacy-safe, STRUCTURE-PRESERVING fixture view of one message, plus the residual-run warnings.
    Pure — takes already-parsed fields (e.g. from ``gmail_source.parse_rfc822``), returns a dict. Writes nothing."""
    header = sanitise_header_block(from_address=from_address, to_addresses=to_addresses, subject=subject,
                                   date=date, auth_verdict=auth_verdict)
    sanitised_body = sanitise_text(body, min_account_digits=min_account_digits)
    return {
        "header": header,
        "body": sanitised_body,
        "residual_numeric_runs": residual_numeric_runs(sanitised_body),
        "residual_token_candidates": residual_token_candidates(sanitised_body),
        "min_account_digits": int(min_account_digits),
    }
