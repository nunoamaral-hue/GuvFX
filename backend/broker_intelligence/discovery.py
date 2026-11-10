"""ADR-0089 — pre-retention UNKNOWN-SENDER withdrawal discovery: strict, redacted-only, fail-closed, no raw retention.

Runs only for a canary owner, only on NON-allowlisted mail that a specific parser and the ADR-0088 generic engine
never see. It classifies the message IN MEMORY and persists ONLY a redacted ``UnknownSenderDiscoveryCandidate``
(hashes + enums + auth verdicts + confidence band) — never the raw body, never personal data, never an event or
withdrawal. The core ``classify_discovery`` and the redaction contract ``validate_discovery_record_clean`` are pure.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from . import generic_detector as gdx

DETECTOR_VERSION = "discovery-1"

# Extra Tier-2 negatives (reject MORE aggressively pre-retention than Tier-1): statements, login/security alerts,
# KYC/identity/document requests. Checked when no explicit withdrawal lifecycle wording is present.
_NEG_EXTRA = (
    "account statement", "monthly statement", "your statement", "trade confirmation", "contract note",
    "login alert", "new login", "new sign-in", "sign-in attempt", "security alert", "suspicious activity",
    "verify your identity", "identity verification", "kyc", "know your customer", "document request",
    "proof of address", "upload your", "complete your verification", "password reset",
)
# Cheap subject pre-filter keywords (metadata-only, BEFORE any raw body is fetched).
_SUBJECT_KEYWORDS = gdx._INTENT

_AR_RE = re.compile(r"\b(spf|dkim|dmarc)\s*=\s*([a-z]+)", re.IGNORECASE)


def _salt() -> bytes:
    """Domain-separation salt for the stored hashes (config, not a credential). settings-then-env
    ``BROKER_DISCOVERY_HASH_SALT`` with a stable default so dedup is consistent across deploys."""
    try:
        from django.conf import settings
        import os
        raw = getattr(settings, "BROKER_DISCOVERY_HASH_SALT", None)
        if raw is None:
            raw = os.getenv("BROKER_DISCOVERY_HASH_SALT", "adr0089-discovery-v1")
    except Exception:  # noqa: BLE001 — pure/no-settings contexts
        raw = "adr0089-discovery-v1"
    return ("adr0089:" + str(raw)).encode("utf-8")


def _h(value: str) -> str:
    return hashlib.sha256(_salt() + (value or "").encode("utf-8")).hexdigest()


def subject_has_withdrawal_keyword(subject: str) -> bool:
    """Cheap metadata-only pre-filter: does the Subject carry a withdrawal keyword? Used BEFORE any raw body is
    fetched, so a non-withdrawal personal email's body is never read."""
    return gdx._any((subject or "").lower(), _SUBJECT_KEYWORDS)


def extract_auth_results(raw_text: str) -> Tuple[str, str, str]:
    """Best-effort SPF/DKIM/DMARC tokens from the ``Authentication-Results`` HEADER block ONLY — never the body, so a
    crafted body line ``dkim=pass`` can NEVER populate the record (the GATE separately uses the aligned combined
    verdict from parse_rfc822). Returns ('','','') when absent."""
    spf = dkim = dmarc = ""
    header_section = (raw_text or "").split("\r\n\r\n", 1)[0].split("\n\n", 1)[0]
    in_ar = False
    for line in header_section.splitlines():
        low = line.lower()
        if low.startswith("authentication-results") or low.startswith("arc-authentication-results"):
            in_ar = True
        elif line[:1] not in (" ", "\t"):      # a new header (not a folded continuation) ends the AR block
            in_ar = False
        if not in_ar:
            continue
        for m in _AR_RE.finditer(line):
            k, v = m.group(1).lower(), m.group(2).lower()[:12]
            if k == "spf" and not spf:
                spf = v
            elif k == "dkim" and not dkim:
                dkim = v
            elif k == "dmarc" and not dmarc:
                dmarc = v
    return spf, dkim, dmarc


@dataclass(frozen=True)
class DiscoveryResult:
    qualifies: bool
    lifecycle_class: str = ""
    confidence_band: str = ""
    reason_codes: Tuple[str, ...] = ()
    sender_domain: str = ""
    auth_approved: bool = False
    spf: str = ""
    dkim: str = ""
    dmarc: str = ""


def classify_discovery(*, subject: str, body: str, from_domain: str, auth_verdict: Optional[str],
                       spf: str = "", dkim: str = "", dmarc: str = "", html_text: str = "") -> DiscoveryResult:
    """Strict pre-retention classification for ONE non-allowlisted message. Qualifies ONLY when: the auth policy
    passes (combined aligned verdict == 'pass'), there is withdrawal intent AND explicit lifecycle wording, no hard
    negative, and no contradictory terminal wording. Pure; fail-closed."""
    text = gdx._norm(subject, body, html_text)
    subj = (subject or "").lower()

    # 1) Authentication policy — fail closed on weak/missing evidence (the pre-retention gate).
    approved = (auth_verdict or "").strip().lower() == "pass"
    if not approved:
        return DiscoveryResult(False, reason_codes=(gdx.RC_AUTH_FAIL,), spf=spf, dkim=dkim, dmarc=dmarc)

    states = gdx._classify_states(text)
    # Require an EXPLICIT withdrawal-intent WORD — a lifecycle phrase alone is NOT enough, else a 'deposit has been
    # completed' email would qualify on the generic completion phrase. Conservative for pre-retention discovery.
    if not gdx._any(text, gdx._INTENT):
        return DiscoveryResult(False, reason_codes=(gdx.RC_NO_INTENT,), auth_approved=True, spf=spf, dkim=dkim, dmarc=dmarc)

    # 2) Hard negatives in the SUBJECT ONLY — a deposit / marketing / statement / KYC / login / trade-confirmation
    # SUBJECT declares a non-withdrawal; a genuine withdrawal subject carries the withdrawal word. Checking the
    # SUBJECT (not the flattened body) means footer boilerplate common to nearly every transactional email (e.g.
    # "unsubscribe") can NEVER reject a genuine withdrawal — the ADR-0089 live-observation false negative. Deposits
    # are already rejected by the withdrawal-intent-word requirement above, so dropping the body check is safe.
    if gdx._any(subj, _NEG_EXTRA + gdx._NEG_DEPOSIT + gdx._NEG_MARKETING + gdx._NEG_VERIFY
                + gdx._NEG_MARGIN + gdx._NEG_TRADEALERT):
        return DiscoveryResult(False, reason_codes=(gdx.RC_NEGATIVE,), auth_approved=True, spf=spf, dkim=dkim, dmarc=dmarc)

    # 3) Contradictory terminal wording -> too ambiguous to persist.
    if len([s for s in states if s in gdx._TERMINAL]) >= 2:
        return DiscoveryResult(False, reason_codes=(gdx.RC_CONTRADICTORY_LIFECYCLE,), auth_approved=True,
                               spf=spf, dkim=dkim, dmarc=dmarc)

    # 4) Conservative threshold: require EXPLICIT lifecycle wording (intent alone is too weak pre-retention).
    if not states:
        return DiscoveryResult(False, reason_codes=(gdx.RC_NO_EXPLICIT_LIFECYCLE,), auth_approved=True,
                               spf=spf, dkim=dkim, dmarc=dmarc)

    proposed = max(states, key=lambda s: gdx._STATE_RANK[s])
    band = "HIGH" if proposed in (gdx.S_COMPLETED, gdx.S_CONFIRMATION_REQUIRED, gdx.S_PROCESSING) else "MEDIUM"
    return DiscoveryResult(True, lifecycle_class=proposed, confidence_band=band,
                           reason_codes=(gdx.RC_INTENT, gdx.RC_EXPLICIT_LIFECYCLE),
                           sender_domain=(from_domain or "").strip().lower(), auth_approved=True,
                           spf=spf, dkim=dkim, dmarc=dmarc)


# Fields of the record that hold text; everything else is a hash/enum/bool/timestamp.
_RECORD_TEXT_FIELDS = ("registrable_domain", "spf", "dkim", "dmarc", "lifecycle_class", "confidence_band",
                       "detector_version", "message_sha256", "provider_message_id_hash", "sender_domain_hash")


def build_discovery_record(*, owner, raw_bytes: bytes, provider_message_id: str, result: DiscoveryResult,
                           received_at) -> dict:
    """Build the REDACTED model kwargs for an UnknownSenderDiscoveryCandidate. NEVER includes a raw subject/body
    snippet, name, full email address, account number, reference, amount or payment detail. The ``registrable_domain``
    (a broker's PUBLIC domain, not personal data) is included ONLY when the authentication policy approved the sender."""
    domain = (result.sender_domain or "").strip().lower()
    return dict(
        owner=owner,
        message_sha256=hashlib.sha256(raw_bytes or b"").hexdigest(),
        provider_message_id_hash=_h("pmid:" + (provider_message_id or "")),
        sender_domain_hash=_h("dom:" + domain),
        registrable_domain=(domain[:190] if result.auth_approved else ""),
        spf=(result.spf or "")[:12], dkim=(result.dkim or "")[:12], dmarc=(result.dmarc or "")[:12],
        auth_policy_passed=bool(result.auth_approved),
        lifecycle_class=result.lifecycle_class, confidence_band=result.confidence_band,
        reason_codes=list(result.reason_codes), detector_version=DETECTOR_VERSION,
        received_at=received_at, observed_at=received_at,
    )


def validate_discovery_record_clean(record: dict, *, forbidden_substrings=()) -> Tuple[bool, str]:
    """Formally-tested REDACTION CONTRACT: assert the redacted record carries NONE of the forbidden PII substrings in
    any text field (structural guarantee — the model has no field that can carry a raw body/name/account/amount).
    Returns (ok, offending). ``forbidden_substrings`` are the known PII of a test message (name tokens, full account,
    reference, amount text, email local-parts, body snippets)."""
    blob = " ".join(str(record.get(f, "")) for f in _RECORD_TEXT_FIELDS)
    blob += " " + " ".join(str(x) for x in (record.get("reason_codes") or []))
    low = blob.lower()
    for bad in forbidden_substrings:
        b = str(bad or "").strip().lower()
        if b and b in low:
            return False, str(bad)
    return True, ""


def _html_to_text(html_body: str) -> str:
    return re.sub(r"<[^>]+>", " ", html_body) if html_body else ""


def process_discovery_messages(messages, *, owner) -> int:
    """DB service: classify the collected non-allowlisted withdrawal-keyword raw messages into REDACTED discovery
    candidates (idempotent per owner+provider-message-id-hash). NEVER stores raw bytes, NEVER creates an event/
    withdrawal. Returns the number of candidates upserted. Never raises into ingestion."""
    from .gmail_source import parse_rfc822
    from .broker_senders import _domain_of
    from .models import UnknownSenderDiscoveryCandidate
    n = 0
    for m in (messages or []):
        try:
            raw = m.get("raw_bytes") if isinstance(m, dict) else getattr(m, "raw_bytes", b"")
            raw = raw or b""
            pmid = (m.get("provider_message_id") if isinstance(m, dict) else getattr(m, "provider_message_id", "")) or ""
            f = parse_rfc822(raw)
            domain = _domain_of(f.get("from_address", "") or "")
            spf, dkim, dmarc = extract_auth_results(raw.decode("utf-8", "replace"))
            res = classify_discovery(subject=f.get("subject", "") or "", body=f.get("body", "") or "",
                                     from_domain=domain, auth_verdict=f.get("auth_verdict"),
                                     spf=spf, dkim=dkim, dmarc=dmarc,
                                     html_text=_html_to_text(f.get("html_body", "") or ""))
            if not res.qualifies:
                continue
            rec = build_discovery_record(owner=owner, raw_bytes=raw, provider_message_id=pmid, result=res,
                                         received_at=f.get("received_at"))
            UnknownSenderDiscoveryCandidate.objects.update_or_create(
                owner=owner, provider_message_id_hash=rec["provider_message_id_hash"], defaults=rec)
            n += 1
        except Exception:  # noqa: BLE001 — discovery must never crash ingestion
            continue
    return n
