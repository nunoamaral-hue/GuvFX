"""GENUINE-TEMPLATE fixture extraction (§4-G) — privacy-safe, deterministic, pure.

When a GENUINE broker email is captured, a positive classifier can only be built + certified against its real
structure (§5: "Establish the actual message structure before implementing extraction rules"). But a genuine broker
message carries PII and financial identifiers that MUST NOT be committed to the repository (§5). This module turns a
genuine message into a STRUCTURE-PRESERVING, PII-redacted fixture: the labels, layout, lifecycle wording and amount
*shape* survive (so a parser author can see the template), while personal names, email addresses, account/reference
numbers, confirmation URLs, authorization tokens and phone numbers are masked.

NAME REDACTION (packet 008). The previous masker only masked an English-style ``Dear <name>,`` salutation and so
LEAKED the member's name on a MULTI-LINGUAL HTML broker email (the name survived in every localised salutation and in
an explicit ``Name`` field). This module now removes a personal name by THREE independent means, then PROVES the result
is clean or FAILS CLOSED:

  1. EXPLICIT owner identity. ``known_names`` — the connected-mailbox owner's name already known to GuvFX — is masked
     everywhere (every language block, every field), case-insensitively, after Unicode normalisation.
  2. SELF-DISCOVERY from the message. The member's name tokens are discovered from the message's own salutations and
     its labelled ``Name`` field (multi-lingual), so a name GuvFX records only partially (e.g. a first name only) is
     still removed in full — the message is authoritative for the exact registered name.
  3. STRUCTURAL residual net. After redaction, a greeting/honorific or a ``Name`` label followed by a residual
     name-shaped token (a name we never knew) is itself masked — and surfaced to the fail-closed validator.

The companion ``validate_pii_clean`` is the acceptance gate: it fails closed (no fixture) if ANY personal name token
(known OR discovered), e-mail, URL, token, account/reference number, or phone number survives — including a name split
across whitespace/markup (it checks a COLLAPSED, accent/case-folded form, so "N u n o" is caught as "nuno").

What this is NOT:
  * It is NOT the ``redaction`` log-sanitiser — that only masks URLs/opaque tokens in free text destined for logs.
  * It is NOT a positive classifier and emits no ``ExternalWithdrawalSignals``: it reveals a template, it does not
    decide a category. It needs no genuine *external-withdrawal* sample to be correct — it runs on ANY message.
  * It does NOT display or return any personal value: discovered/known name tokens are used only to build masks and
    are never placed in the fixture or any count/summary.

Deterministic, pure, no I/O, no DB, no network, holds no authority. A fixture derived from a real message is the
``Provenance.SANITISED`` tier (real email, structure/fields preserved) — distinct from SYNTHETIC and from genuine REAL.
"""
from __future__ import annotations

import re
import unicodedata

from .redaction import redact  # masks confirmation URLs + long opaque authorization tokens

# An e-mail address anywhere in the text -> <EMAIL> (mask the whole address incl. local-part + domain).
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NAME_PLACEHOLDER = "<NAME>"
_EMAIL_PLACEHOLDER = "<EMAIL>"
_PHONE_PLACEHOLDER = "<PHONE>"
# Collapse a run of adjacent name placeholders (one per name token) back to a single <NAME>, so a multi-token name
# reads as one placeholder ("Dear <NAME>," not "Dear <NAME> <NAME>,").
_NAME_RUN = re.compile(r"%s(?:[ \t]+%s)+" % (re.escape(_NAME_PLACEHOLDER), re.escape(_NAME_PLACEHOLDER)))

# Default length at/above which a run of consecutive digits is treated as an account / reference identifier (sensitive)
# rather than an ordinary quantity. A typical broker account/reference is >=5 digits; monetary amounts (e.g. "24.12",
# "1000.00", "500") have <=4 digits per run and are PRESERVED so the amount *shape* stays visible in the fixture.
_DEFAULT_MIN_ACCOUNT_DIGITS = 5

# Letters that may form part of a personal name token (ASCII + Latin-1/Extended-A), used for token boundaries so a
# name adjacent to non-Latin script (e.g. a CJK salutation) or punctuation is still bounded correctly.
_NAME_LETTERS = r"A-Za-zÀ-ÿĀ-ſ"
# A capitalised Latin name token, used only to DISCOVER a name following a greeting/label (never to decide a category).
_LATIN_NAME_TOKEN = r"[A-ZÀ-ÖØ-Þ][%s'’.\-]*" % _NAME_LETTERS

# Multi-lingual salutation leads + honorifics. Broker confirmation mail repeats its body in many languages; the member's
# (Latin) name follows a greeting in each block. We do not need exhaustive coverage — the English block alone yields the
# full name for token-discovery — but a broad set lets the residual net catch a name in a non-English block too.
_GREETINGS = [
    "dear", "hi", "hello", "hey", "hiya",
    "hallo", "sehr geehrte", "sehr geehrter", "liebe", "lieber", "guten tag", "guten morgen",
    "bonjour", "salut", "cher", "chere", "chère", "madame", "monsieur",
    "hola", "estimado", "estimada", "estimados", "querido", "querida", "buenas", "buenos",
    "ola", "olá", "prezado", "prezada", "caro", "cara", "oi",
    "ciao", "gentile", "salve", "egregio", "egregia",
    "beste", "geachte", "hoi",
    "cześć", "szanowny", "szanowna", "witaj",
    "merhaba", "sayın",
    "你好", "您好", "親愛的", "亲爱的", "こんにちは", "안녕하세요",
    "مرحبا", "عزيزي", "عزيزتي",
]
_HONORIFICS = ["mr", "mrs", "ms", "miss", "mx", "dr", "prof", "herr", "frau",
               "sr", "sra", "srta", "don", "mme", "mlle", "sig", "dhr", "mevr", "bay", "bayan"]
# Multi-lingual "name" field labels (the explicit labelled field a withdrawal-confirmation table carries).
_NAME_LABELS = ["full name", "account name", "account holder", "holder name", "client name", "customer name", "name",
                "nombre completo", "nombre", "nom complet", "nom", "nome", "naam", "nazwa", "imię", "ad soyad",
                "isim", "ad", "姓名", "氏名", "お名前", "名前", "이름", "성명", "الاسم", "اسم", "имя"]


def _alt(words) -> str:
    """Regex alternation of ``words``, longest-first so a multi-word lead (e.g. "sehr geehrte") wins over a prefix."""
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


# Discover a name following a greeting / honorific (capture the following capitalised Latin tokens, <=4, up to a comma,
# newline, or non-name punctuation). Used to LEARN name tokens AND, as a residual masker, to remove an undiscovered one.
_SAL_DISCOVER = re.compile(
    r"(?i)(?:(?<![%s])|^)(?:%s)[^\S\n]+(%s(?:[^\S\n]+%s){0,3})" % (
        _NAME_LETTERS, _alt(_GREETINGS + _HONORIFICS), _LATIN_NAME_TOKEN, _LATIN_NAME_TOKEN))
# Discover the value of an explicit "Name" field. Colon/dash optional because an HTML table cell may space-join the
# label and value after tag-stripping ("Name Jane Doe"). Bounded to <=4 capitalised Latin tokens so it never swallows a
# following field on the same line.
_NAMEFIELD_DISCOVER = re.compile(
    r"(?im)(?:^|[>\|])[^\S\n]{0,12}(?:%s)[^\S\n]{0,3}[:\-]?[^\S\n]{0,12}(%s(?:[^\S\n]+%s){0,3})" % (
        _alt(_NAME_LABELS), _LATIN_NAME_TOKEN, _LATIN_NAME_TOKEN))

# A conservative phone-number run: an international "+" number, a parenthesised area code, or >=3 digit groups joined by
# space/dash/dot. Validated by digit count (7..15) in the callback so a decimal amount (one '.', <=2 groups) never matches.
_PHONE = re.compile(
    r"(?<!\w)(?:\+\d[\d\s().\-]{5,}\d|\(\d{2,4}\)[\s.\-]?\d[\d\s.\-]{3,}\d|\d{2,4}[\s.\-]\d{2,4}[\s.\-]\d{2,4}(?:[\s.\-]\d{2,4})?)(?!\w)")

# A mixed letter+digit run (a possible confirmation code / short token) longer than a plain word. Masked as <TOKEN:n>.
_TOKEN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,}")
# A labelled reference value (purely numeric or mixed) -> <REF>, so even a short all-digit reference is removed. The
# whole label word is anchored with \b (so "reference" cannot split into "ref"+"erence"), the value must contain a
# digit (so ordinary prose after a label word — "request processed" — is never masked), and it runs before the generic
# account/token maskers. Group 1 is the label+separator prose (preserved); group 2 is the value (replaced).
_LABELLED_REF = re.compile(
    r"(?i)(\b(?:reference|ref|transaction|txn|confirmation|request|withdrawal\s+id|payment\s+id)\b"
    r"(?:\s*(?:id|no\.?|number|code|#))?[ \t]*[:#.\-]?[ \t]*)"
    r"(?=[A-Za-z0-9\-]*\d)([A-Za-z0-9][A-Za-z0-9\-]{2,31})\b")


def _account_run_re(min_digits: int) -> re.Pattern:
    # A maximal run of >= min_digits digits that is part of neither a larger digit run NOR a DECIMAL number (so a
    # monetary amount "12345.67" is preserved while a bare identifier "51763830" and a sentence-ending "647390." mask).
    return re.compile(r"(?<!\d)(?<!\d\.)\d{%d,}(?!\d)(?!\.\d)" % int(min_digits))


def _residual_run_re(min_digits: int) -> re.Pattern:
    # DELIBERATELY BROADER than the masker: every digit run >= min_digits, with NO decimal exclusion — so a decimal-glued
    # identifier the masker preserves as a pseudo-amount is still surfaced for review.
    return re.compile(r"(?<!\d)\d{%d,}(?!\d)" % int(min_digits))


# ---- Unicode helpers ------------------------------------------------------------------------------------------------
def _nfkc(s) -> str:
    """NFKC-normalise (fold full-width/compatibility forms to their canonical ASCII-ish form) so a name/number written
    with full-width or compatibility characters is matched and masked like its plain form."""
    return unicodedata.normalize("NFKC", "" if s is None else str(s))


def _collapse(s) -> str:
    """Accent-strip, case-fold, and drop every non-letter/digit character (Unicode-aware, so CJK/Cyrillic/Greek/Arabic
    letters are KEPT) — a script-agnostic comparison form in which a name split by whitespace or stray markup
    ("N u n o", "张 伟") collapses back to a single token ("nuno", "张伟")."""
    s = unicodedata.normalize("NFKD", "" if s is None else str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", s.casefold())            # \W is Unicode-aware: keeps letters+digits of every script


# Scripts without reliable word boundaries (no inter-word spaces): a name in them is masked as a plain substring, since
# a word-boundary match would never fire (every neighbour is also a "letter"). Covers CJK, Kana, Hangul, Thai, etc.
_BOUNDARYLESS = (
    (0x3040, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF), (0xAC00, 0xD7AF),
    (0x0E00, 0x0E7F), (0x0E80, 0x0EFF), (0x1780, 0x17FF), (0x20000, 0x2A6DF),
)


def _is_boundaryless(tok: str) -> bool:
    return any(any(lo <= ord(c) <= hi for lo, hi in _BOUNDARYLESS) for c in tok)


def _name_token_re(tok: str) -> re.Pattern:
    """Case-insensitive pattern for one name token, SCRIPT-AWARE. A non-ASCII token (CJK/Cyrillic/Greek/Arabic ...) is
    masked as a plain SUBSTRING — word boundaries are unreliable there and over-masking is privacy-safe. An ASCII
    (Latin) token is bounded by non-LATIN letters (so a Latin name written directly against a non-Latin script, e.g.
    a CJK salutation "您好Jane", is still bounded and masked) and, for len>=3, tolerates whitespace BETWEEN
    characters so a token split by inline markup ("Nu no") is still masked."""
    tok = _nfkc(tok)
    if _is_boundaryless(tok) or not tok.isascii():
        return re.compile(r"(?i)%s" % re.escape(tok))
    core = r"\s*".join(re.escape(c) for c in tok) if len(tok) >= 3 else re.escape(tok)
    return re.compile(r"(?i)(?<![%s])%s(?![%s])" % (_NAME_LETTERS, core, _NAME_LETTERS))


def _name_tokens(*values) -> list[str]:
    """Usable name tokens from arbitrary strings, SCRIPT-AGNOSTIC: NFKC, take maximal runs of Unicode letters (any
    script), keep those of length >=2, PLUS the whole letters-only concatenation of each value (so a space-separated
    single-character CJK given+family name, e.g. "张 伟", still yields the maskable token "张伟").
    De-duplicated case-insensitively, ordered longest-first so a full surname masks before any shorter sub-part."""
    seen: dict[str, str] = {}
    for v in values:
        s = _nfkc(v)
        runs = re.findall(r"[^\W\d_]+", s)                 # runs of letters of any script
        whole = "".join(runs)
        for raw in runs + ([whole] if len(whole) >= 2 else []):
            t = raw.strip("'’.-")
            if len(t) >= 2:
                seen.setdefault(t.casefold(), t)
    return sorted(seen.values(), key=lambda t: (-len(t), t.casefold()))


def _discover_name_tokens(text: str) -> list[str]:
    """Name tokens the MESSAGE itself reveals — from its salutations and its labelled ``Name`` field. Lets the masker
    remove the member's exact registered name even when GuvFX records only part of it."""
    s = _nfkc(text)
    found: list[str] = []
    for rx in (_SAL_DISCOVER, _NAMEFIELD_DISCOVER):
        for m in rx.finditer(s):
            found.extend(_name_tokens(m.group(1)))
    # de-dup preserving order, longest-first
    return _name_tokens(" ".join(found))


def _mask_phone(s: str) -> str:
    def _sub(m):
        digits = sum(ch.isdigit() for ch in m.group(0))
        return _PHONE_PLACEHOLDER if 7 <= digits <= 15 else m.group(0)
    return _PHONE.sub(_sub, s)


def _mask_names(s: str, tokens) -> str:
    """Mask every occurrence of each name token, longest-first (so a surname is masked before any shorter sub-token)."""
    for tok in sorted(set(tokens), key=lambda t: (-len(t), t.casefold())):
        s = _name_token_re(tok).sub(_NAME_PLACEHOLDER, s)
    return s


def _mask_residual_salutation_and_fields(s: str) -> str:
    """Mask a name that FOLLOWS a greeting/honorific or a ``Name`` label but was not among the known/discovered tokens
    (a name GuvFX never recorded). Replaces only the captured name span, leaving the greeting/label structure visible."""
    def _sub(m):
        return m.group(0).replace(m.group(1), _NAME_PLACEHOLDER, 1)
    s = _SAL_DISCOVER.sub(_sub, s)
    s = _NAMEFIELD_DISCOVER.sub(_sub, s)
    return s


def sanitise_text(text, *, min_account_digits: int = _DEFAULT_MIN_ACCOUNT_DIGITS, known_names=()) -> str:
    """Return ``text`` with personal data + financial identifiers masked, preserving transactional STRUCTURE. Order:
    NFKC-normalise; e-mail addresses (so no bare domain is left); URLs/opaque tokens (``redact``); personal NAMES
    (``known_names`` plus names discovered from the message's salutations/``Name`` field, then a residual
    greeting/label net); phone numbers; long digit runs (account/reference); labelled references and mixed-alnum
    tokens. Pure; never raises on non-str (coerces)."""
    s = _nfkc(text)
    s = _EMAIL.sub(_EMAIL_PLACEHOLDER, s)
    s = redact(s)                                               # confirmation URLs + long opaque authorization tokens
    name_tokens = _name_tokens(*known_names) + _discover_name_tokens(s)
    s = _mask_names(s, name_tokens)                            # explicit + self-discovered personal names
    s = _mask_residual_salutation_and_fields(s)               # names we never knew, caught structurally
    s = _NAME_RUN.sub(_NAME_PLACEHOLDER, s)                    # a multi-token name collapses to a single <NAME>
    s = _mask_phone(s)
    s = _LABELLED_REF.sub(lambda m: f"{m.group(1)}<REF>", s)
    s = _account_run_re(min_account_digits).sub(lambda m: f"<ACCT:{len(m.group(0))}>", s)  # length-aware shape
    s = _TOKEN_CANDIDATE.sub(lambda m: f"<TOKEN:{len(m.group(0))}>", s)
    return s


def residual_numeric_runs(text, *, min_digits: int = 3) -> list[str]:
    """Numeric runs of >= ``min_digits`` left in ``text`` — surfaced so a human reviewer can judge whether any remaining
    number is a sensitive identifier (defence in depth). Deduplicated, order-preserving."""
    s = "" if text is None else str(text)
    seen: list[str] = []
    for m in _residual_run_re(min_digits).finditer(s):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


def residual_token_candidates(text) -> list[str]:
    """Mixed letter+digit runs of >= 6 chars left in ``text`` — possible short confirmation codes. Surfaced for review.
    Deduplicated, order-preserving."""
    s = "" if text is None else str(text)
    seen: list[str] = []
    for m in _TOKEN_CANDIDATE.finditer(s):
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return seen


def sanitise_header_block(*, from_address: str = "", to_addresses=(), subject: str = "",
                          date: str = "", auth_verdict: str = "", known_names=()) -> dict:
    """Sanitise the header fields a fixture author needs to SEE (domains + subject shape), addresses masked to domain
    only. Never includes a raw recipient address or display name."""
    def _domain(addr: str) -> str:
        addr = (addr or "").strip()
        return addr.split("@", 1)[1] if "@" in addr else _EMAIL_PLACEHOLDER if addr else ""

    return {
        "from_domain": _domain(from_address),
        "to_domains": sorted({_domain(a) for a in (to_addresses or ()) if a}),
        "subject": sanitise_text(subject, known_names=known_names),
        "date": date or "",
        "auth_verdict": auth_verdict or "",
    }


def extract_fixture(*, from_address: str = "", to_addresses=(), subject: str = "", body: str = "",
                    date: str = "", auth_verdict: str = "", known_names=(),
                    min_account_digits: int = _DEFAULT_MIN_ACCOUNT_DIGITS) -> dict:
    """Build a privacy-safe, STRUCTURE-PRESERVING fixture view of one message, plus residual-run warnings. Pure — takes
    already-parsed fields (e.g. from ``gmail_source.parse_rfc822``) and the owner's ``known_names``; writes nothing and
    returns NO personal value (only a COUNT of name tokens used to build masks)."""
    header = sanitise_header_block(from_address=from_address, to_addresses=to_addresses, subject=subject,
                                   date=date, auth_verdict=auth_verdict, known_names=known_names)
    name_tokens = _name_tokens(*known_names) + _discover_name_tokens(_nfkc(body))
    sanitised_body = sanitise_text(body, min_account_digits=min_account_digits, known_names=known_names)
    return {
        "header": header,
        "body": sanitised_body,
        "residual_numeric_runs": residual_numeric_runs(sanitised_body),
        "residual_token_candidates": residual_token_candidates(sanitised_body),
        "name_tokens_masked_count": len(set(t.casefold() for t in name_tokens)),
        "min_account_digits": int(min_account_digits),
    }


# ---- Fail-closed acceptance gate (packet 008) -----------------------------------------------------------------------
def validate_pii_clean(*, subject: str = "", body: str = "", known_names=(),
                       min_account_digits: int = _DEFAULT_MIN_ACCOUNT_DIGITS) -> dict:
    """Prove a SANITISED subject+body carries no residual personal data, or report exactly which category survived.

    Returns ``{"ok": bool, "counts": {category: n}}`` with counts ONLY (never a literal value). ``ok`` is True iff every
    category is zero. Categories:
      * ``known_or_discovered_name`` — a known/discovered name token still present, checked in COLLAPSED accent/case-
        folded form (len>=4) or bounded form (len 2-3), so a whitespace/markup-split name is still caught.
      * ``residual_salutation_or_field_name`` — a greeting/honorific/``Name`` label still followed by a name-shaped
        token (a name we never knew) — the undiscovered-name backstop.
      * ``email`` / ``url`` / ``token`` — any surviving address, link, or mixed-alnum/opaque token.
      * ``account_or_reference_number`` — any surviving >= ``min_account_digits`` digit identifier (decimal-guarded, so
        a monetary amount is not counted).
      * ``phone`` — any surviving phone-shaped number.
    """
    s = _nfkc(f"{subject}\n{body}")
    name_tokens = _name_tokens(*known_names) + _discover_name_tokens(s)
    collapsed = _collapse(s)

    def _name_present(tok: str) -> bool:
        ctok = _collapse(tok)
        if len(ctok) < 2:
            return False
        if str(tok).isascii():
            # Bounded + whitespace-between-characters match: catches a verbatim OR a whitespace/markup-split name
            # ("N u n o") PRECISELY, without the false positives a plain substring would hit inside the large collapsed
            # legal-text blob (a 4-letter name is often an accidental substring there; a bounded match is not).
            return _name_token_re(tok).search(s) is not None
        return ctok in collapsed                               # non-ASCII (CJK/Cyrillic/Greek/Arabic): collapsed substring

    present = set()
    # Fail-closed set: every known/discovered name TOKEN, plus each RAW known owner value (any script) in case
    # tokenisation missed it — all checked with the same precise length-aware rule.
    for t in list(name_tokens) + [str(v) for v in known_names if v]:
        if _name_present(t):
            present.add(_collapse(t))

    # Strip placeholders before the residual structural scan so "<NAME>" after a greeting is not re-counted as a name.
    without_ph = re.sub(r"<[^>\n]{0,20}>", " ", s)
    residual_names = len(_SAL_DISCOVER.findall(without_ph)) + len(_NAMEFIELD_DISCOVER.findall(without_ph))

    from .redaction import _URL, _TOKEN  # reuse the exact log-sanitiser detectors
    counts = {
        "known_or_discovered_name": len(present),
        "residual_salutation_or_field_name": residual_names,
        "email": len(_EMAIL.findall(s)),
        "url": len(_URL.findall(s)),
        "token": len(_TOKEN.findall(s)) + len(_TOKEN_CANDIDATE.findall(s)),
        "account_or_reference_number": len(_account_run_re(min_account_digits).findall(s)),
        "phone": sum(1 for m in _PHONE.finditer(s) if 7 <= sum(c.isdigit() for c in m.group(0)) <= 15),
    }
    return {"ok": all(v == 0 for v in counts.values()), "counts": counts}
