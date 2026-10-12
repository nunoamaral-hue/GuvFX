"""PR-B — IS6 (IS6FX) deterministic email parser (parser #2). NO LLM in the critical path.

Turns an IS6FX transactional email into at most one ``ParsedBrokerEvent``, extracting account/amount/currency/fee and
classifying BOTH the lifecycle (``event_type``) AND the transaction category, for authenticated IS6 mail in English
AND Japanese (IS6FX is a Japanese broker: 入金 = deposit, 出金 = withdrawal, 手数料 = fee, 口座番号 = account number).

Governing safety rules (carried verbatim from the TradersWay parser — a single structural invariant across brokers):
  * A message is ``EXTERNAL_WITHDRAWAL`` ONLY on positive evidence money LEFT the broker — and V1 has **no
    prose→EXTERNAL path at all**. External is never guessed from email text; it is certified against a genuine
    external-withdrawal sample. So no IS6 wording can ever over-claim an external withdrawal.
  * A DEPOSIT lifecycle always carries category ``DEPOSIT`` (money entering). The withdrawal projection counts ONLY
    ``EXTERNAL_WITHDRAWAL``, so a deposit can NEVER contaminate withdrawal metrics — structurally, not by filtering.
  * Completion/rejection is believed ONLY when asserted, in a perfect/past verb phrase, about the MEMBER'S OWN
    transaction of the RESOLVED direction ("your withdrawal … has been completed" for a withdrawal; 出金…完了 for JP) —
    never from a request, a generic/habitual rule, a future promise, a negation, a call-to-action, a conditional or
    temporal ("after/once …") clause, quoted-reply history, a completed SUB-STEP (review/verification/fee), or the
    OTHER direction's event (a funding "deposit completed" line in a withdrawal email). See the lifecycle-wording and
    field-grammar comments below for how each class is structurally excluded; this was hardened across a multi-round
    adversarial review (``docs/IS6_PARSER_PRB_CERTIFICATION.md``).
  * The parser NEVER extracts or emits a confirmation URL / authorization token (sensitive; evidence-only) and NEVER
    follows a link. Absent fields stay None/'' — never fabricated.

``occurred_at`` is deliberately left None: IS6 states a wall-clock time ("2026/10/12 15:13") with NO explicit
timezone, so converting it to an instant would be a fabrication / point-in-time hazard. The broker-stated time lives
in the immutable raw evidence; the worker derives a tz-correct timestamp from the message headers (as for TradersWay).

Registered on demand via ``register_default_parsers()`` (called by the WP3b worker), not at import — inert until the
DARK ingestion worker is armed. Deterministic + pure (no I/O, no DB). Certified against the genuine captured IS6
deposit email (blob 3) as a RULE-11 positive control, plus synthetic EN/JP + adversarial fixtures in the test suite.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

from .parsers import BrokerEmailParser, ParsedBrokerEvent

_SENDER_DOMAIN = "is6.com"

_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "SGD", "HKD", "ZAR", "SEK", "NOK", "DKK",
               "PLN", "CZK", "AED", "MXN", "CNH", "CNY"}

# can_parse keyword gate (EN + JP). Broad enough to claim IS6's own deposit/withdrawal/transfer mail, narrow enough
# not to claim unrelated broker notices (the From-domain match has already pinned the broker).
_KEYWORDS = ("deposit", "withdraw", "withdrawal", "transfer", "payout", "remittance",
             "入金", "出金", "振替", "振込", "送金")

# --- Lifecycle wording (EN + JP). Deterministic, order-sensitive: REJECTED/COMPLETED precede PROCESSING precede
# --- REQUESTED, so a "deposit request received" mail never reads as completed.
#
# Completion and rejection are believed ONLY when asserted, in a PERFECT/PAST verb phrase, about the MEMBER'S OWN
# transaction ("your <deposit|withdrawal|payment|transfer|procedure|funds> … has been/was/is completed"). This single
# anchor structurally rejects the whole family of false positives the adversarial review found, WITHOUT a fragile
# word-blacklist:
#   * a generic/habitual third-person subject — "a/each/this deposit is completed", "Deposits are completed within
#     24h" — has NO "your <txn>" before the verb, so it never matches;
#   * a future promise — "will be completed" — is not one of the accepted verb phrases (is|are|was|has been|
#     have been|been) completed;
#   * a negation — "your deposit has NOT been completed" — cannot match because the gap between "your <txn>" and the
#     completed verb refuses to cross "not"/"no longer"/"unable";
#   * a call-to-action — "to complete your deposit" — is the infinitive "complete", not "<verb> completed".
# The remaining false positive is the CONDITIONAL/HYPOTHETICAL clause that still uses "your deposit was declined"
# ("if your deposit was declined, contact support") — handled by dropping conditional clauses (``_assertive``) before
# matching. The subject form "completed deposit processing" (blob 3's subject) and the JP perfect forms 完了しました/
# いたしました/済/です are kept as explicit completion anchors.
_DEPOSIT_CTX = re.compile(r"deposit|入金", re.IGNORECASE)
_WITHDRAWAL_CTX = re.compile(r"withdraw|出金", re.IGNORECASE)

# Clause granularity: split on sentence terminators AND commas, so a conditional footer clause is isolated from an
# adjacent asserted-completion clause. A clause carrying a conditional/hypothetical marker is DROPPED before matching
# (suppression only — a missed marker fails safe toward REQUESTED/None, never toward a false completion). Timing words
# ("within"/"until") are deliberately NOT markers here: a genuine "has been completed and funds arrive within 1 day"
# must survive — the perfect-tense verb anchor already excludes the future "will be completed within …".
_SENTENCE_SPLIT = re.compile(r"[.!?;:,\n]+|[。！？、\n]+")
# Conditional/hypothetical AND temporal-subordinator markers: a clause stating a NOT-yet-happened condition
# ("if/when/once/unless …", and the temporal "after/before/until your deposit is completed, we will …") is dropped,
# so request-ack boilerplate can never be read as a completion/rejection (invariant C). "within"/"by" are NOT here —
# they qualify a genuine perfect-tense completion ("has been completed … within 1 day"), which the verb anchor keeps.
_CONDITIONAL = re.compile(
    r"\b(?:if|should|once|when|unless|in case|as soon as|upon|provided|in the event|whether"
    r"|after|before|until|till)\b"
    r"|もし|場合|たら|れ(?:ば)|まで", re.IGNORECASE)

# "your <≤6 words> <txn>" … (never crossing a negation OR a conjunction that would introduce a second subject) …
# "<perfect/present verb> completed" — all within one clause. The JP forms are gated the SAME way: a transaction
# token (入金/出金/振替/振込/送金/手続き) must precede the 完了/却下/拒否 verb within the clause, so an unrelated
# "ご本人確認が完了しました" (identity verification completed) / "認証が拒否されました" (authentication rejected) in a deposit
# RECEIPT email is NOT read as the deposit completing/being rejected (invariant C) — while blob 3's
# "…送金のお手続きが完了しました" still matches.
# Direction-specific vs AMBIGUOUS money tokens. A completion/rejection is tied to the resolved transaction DIRECTION:
# the anchor for a withdrawal may match only the withdrawal's own token (withdrawal/出金) or an ambiguous one
# (payment/transfer/…/送金/振込/振替) — NEVER the deposit token — and vice versa. So a "your deposit has been completed"
# line inside a withdrawal-REQUEST email (a common reassurance that the funding deposit cleared) can NOT advance the
# message to WITHDRAWAL_COMPLETED (invariant C). Blob 3 still matches because its body uses the ambiguous "payment
# procedure … bank transfer" (EN) / "送金のお手続き" (JP), both allowed for a DEPOSIT.
_TXN = {
    "DEPOSIT": r"(?:deposit|payment|transfer|procedure|remittance|funds?)",
    "WITHDRAWAL": r"(?:withdrawal|payment|transfer|procedure|remittance|funds?)",
}
_JP_TXN = {
    "DEPOSIT": r"(?:入金|送金|振込|振替)",
    "WITHDRAWAL": r"(?:出金|送金|振込|振替)",
}
# The gap between the money word and the completed/rejected verb must not cross a negation or a conjunction that would
# introduce a second subject.
_GAP = r"(?:(?!\b(?:not|no longer|unable|and|which|while|but)\b)[^\n]){0,80}?"   # bounded, tempered
# Immediately after the money word require a completion/rejection verb-starter, a preposition, or a money-operation
# noun — NOT a bare qualifier noun. This makes "your <txn>" the SUBJECT of the verb: "your withdrawal TO your bank
# account has been completed" / "your deposit VIA this method has been completed" / "your payment PROCEDURE … has been
# completed" match, while "your withdrawal BANK account …" and "your deposit ACCOUNT verification …" (real subject a
# non-money noun) do not. (The EN analogue of the JP subject-particle anchor.)
_EN_NEXT = (r"(?=\s+(?:is|are|was|has|have|been|to|of|for|via|from|into|by"
            r"|procedure|processing|payment|transfer|transaction|remittance)\b)")
# "your" must be the grammatical SUBJECT, not the OBJECT of a preposition: "review/confirmation/verification OF your
# withdrawal has been completed" is a completed SUB-STEP, not the money movement. The lookbehind rejects a governing
# preposition before "your", and the ≤6-word prefix to the txn word may not cross a preposition or a non-money
# QUALIFIER noun ("your FEE payment", "your CARD authentication FOR this deposit") — mirroring the JP subject anchor.
# ("bank"/"wire" are NOT qualifiers — "your bank transfer has been completed" is a genuine money movement.)
_EN_QUAL = r"(?:fee|card|account|identity|verification|review|confirmation|authentication|registration|method|security)"
# _EN_PREP and the _EN_SUBJ lookbehind are derived from ONE token list so they can never drift apart (a hand-kept
# second list silently dropped 6 prepositions and reopened the sub-step hole). Every token is one word, so each
# lookbehind stays fixed-width (Python requires this).
_EN_PREP_TOKENS = ("of", "for", "to", "regarding", "about", "concerning", "on", "onto", "upon", "under",
                   "from", "into", "by", "with")
_EN_PREP = r"(?:" + "|".join(_EN_PREP_TOKENS) + r")"
_EN_SUBJ = "".join(r"(?<!\b%s )" % _t for _t in _EN_PREP_TOKENS)


# The OTHER direction's exclusive keyword. When a completion/rejection is asserted via an AMBIGUOUS token
# (payment/transfer/…/送金/振込/振替), it must NOT be the other direction's event: "your DEPOSIT payment has been
# completed" in a withdrawal email names the funding deposit, not the withdrawal. So the other direction's keyword is
# forbidden in the EN prefix span and immediately before the JP token.
_EN_OTHER = {"DEPOSIT": r"(?:withdrawals?|withdraw|withdrawn)", "WITHDRAWAL": r"(?:deposits?)"}
_JP_OTHER = {"DEPOSIT": "出金", "WITHDRAWAL": "入金"}


def _own(txn: str, other: str) -> str:
    """The EN 'your <txn> … <verb>' subject anchor for a given (kind-specific) txn pattern; ``other`` is the opposite
    direction's keyword, forbidden in the prefix so an ambiguous token can't carry the other direction's event."""
    return (_EN_SUBJ + r"your\s+(?:(?!(?:" + _EN_PREP + r"|" + _EN_QUAL + r"|" + other + r")\b)\w+\s+){0,6}?"
            + txn + r"\b" + _EN_NEXT + _GAP)
# JP: the money token must be the SUBJECT of the completion/rejection verb, not merely nearby. The anchor is
# <money op>(の)(お)(手続き|処理)? + a subject particle (が/は/を/も) + a short adverbial gap. This structurally rejects
# the whole family of false positives: an unrelated procedure ("本人確認のお手続きが完了" = KYC — no money op), and a
# money word used only as a MODIFIER of another subject noun ("出金先口座の登録が完了", "入金方法の変更が完了", "入金設定が完了")
# — in those the money op is NOT followed by a particle, so it never anchors. The bare noun 手続き is also excluded as
# an operation on its own (it attaches to any procedure). Blob 3's "…銀行送金のお手続きが完了しました" still matches via
# 送金のお手続きが. The adverbial gap additionally refuses to cross a qualifier/subject noun (belt-and-braces), a SECOND
# subject/object particle (が/は/を/も — "出金を 取り消す手続きが 完了" = the CANCELLATION procedure completed, not the
# withdrawal), or a cancellation noun (取消/取り消/キャンセル/中止/返金/取りやめ).
def _jp_own(jp_txn: str, other: str) -> str:
    # ``other`` (the opposite direction's exclusive token) must not immediately precede this token — "入金送金が完了" in
    # a 出金 email names the deposit remittance. The adverbial gap additionally refuses 未/不/非 (negation prefixes:
    # 未完了 = NOT completed) besides the qualifier/cancellation nouns and a second particle.
    return (r"(?<!" + other + r")" + jp_txn + r"(?:の)?(?:お)?(?:手続き|処理)?[がはをも]"
            r"(?:(?!(?:登録|変更|方法|確認|認証|口座|先|本人|取消|取り消|キャンセル|中止|返金|取りやめ|未|不|非)|[がはをも])[^\n。、]){0,12}?")


_VERB_COMPLETED = r"(?:is|are|was|has been|have been|been)\s+completed"
# JP completion verb with a TRAILING-negation guard: 完了済 / 完了です etc. must not be the head of a negated phrase
# (完了済みではありません = "is NOT completed", 完了しておりません = "has not completed").
_JP_NEG_TAIL = r"(?!(?:み)?(?:で(?:は|も)?(?:ありません|ございません|ない)|ておりません|ていません))"
_JP_VERB_COMPLETED = (r"完了(?:(?:致し|いたし|し)ました|済|となりました|となっており(?:ます|ました)|しており(?:ます|ました)|です)"
                      + _JP_NEG_TAIL)
_VERB_REJECTED = r"(?:is|are|was|has been|have been|been)\s+(?:rejected|declined)"
_JP_VERB_REJECTED = r"(?:却下|拒否)され(?:ました|た)"
_VERB_PROCESSING = r"(?:is\s+|are\s+)?being\s+processed|(?:has|have)\s+been\s+processed"

# Completion/rejection/processing are matched with KIND-SPECIFIC anchors (see _TXN): the completion of the OTHER
# direction's token never advances this message. (The "completed <txn> processing" subject phrase is deliberately NOT
# used as an independent decider — a "Re:"/reversal reply preserves it while the body says the opposite — so the
# genuine completion is read only from the BODY via these anchors; blob 3's body matches via the ambiguous token.)
_COMPLETED = {k: re.compile(_own(_TXN[k], _EN_OTHER[k]) + _VERB_COMPLETED
                            + r"|" + _jp_own(_JP_TXN[k], _JP_OTHER[k]) + _JP_VERB_COMPLETED,
                            re.IGNORECASE) for k in ("DEPOSIT", "WITHDRAWAL")}
_REJECTED = {k: re.compile(_own(_TXN[k], _EN_OTHER[k]) + _VERB_REJECTED
                           + r"|" + _jp_own(_JP_TXN[k], _JP_OTHER[k]) + _JP_VERB_REJECTED,
                           re.IGNORECASE) for k in ("DEPOSIT", "WITHDRAWAL")}
_PROCESSING = {k: re.compile(r"(?:" + _own(_TXN[k], _EN_OTHER[k]) + r"(?:" + _VERB_PROCESSING + r"))"
                             r"|" + _jp_own(_JP_TXN[k], _JP_OTHER[k]) + r"処理中", re.IGNORECASE)
               for k in ("DEPOSIT", "WITHDRAWAL")}
_REQUESTED = re.compile(
    r"\b(?:requested|has been received|been received|received|request)\b|受付|受け付け|申請|リクエスト", re.IGNORECASE)

# INTERNAL-transfer positive evidence (EN + JP). Same spirit as TradersWay: money moving between the member's OWN
# accounts inside the broker is internal — never external.
_INTERNAL_EVIDENCE = re.compile(
    r"\b(internal transfer|between (your )?[\w ]{0,24}accounts?|account[- ]to[- ]account|"
    r"transferr?ing to your [\w ]{0,12}account)\b|口座間(振替|振込)|内部振替", re.IGNORECASE)

# --- Field grammar (label → value). IS6 renders transactional details as label/value pairs; after _html_to_text an
# --- HTML table row becomes "<label> <value>" on its OWN line and a <p>/<div> pair becomes "<label>\n <value>".
# --- The label is ANCHORED to the start of a line (``_LINE``) so a stray occurrence of the label word in prose
# --- (e.g. "Any bonus amount 500 is...", "sent to your bank account 12345678", "concerns your account 55551234")
# --- can NEVER be mis-grabbed before the real labelled detail row. The separator is a BOUNDED run of whitespace /
# --- ':' / '：' / '|' only — it matches whitespace, so it can never skip over an intervening word, only bridge the
# --- blank gap (incl. one line break) between a label cell and its value cell.
_LINE = r"(?m)^[^\S\r\n]*"
_SEP = r"[\s:：|]{0,16}"
# A monetary value: optional leading symbol/code, a number (US thousands grouping, European decimal comma, or plain),
# optional trailing code in half/full-width parens — e.g. "200 (USD)", "200（USD）", "USD 200", "$200",
# "1,250.00 (USD)", "12,34 (EUR)". The trailing ``(?![.,]?\d)`` is a FAIL-CLOSED guard: a multi-separator number such
# as a European thousands+decimal "1.234,56" matches NEITHER accepted shape cleanly, so _MONEY refuses it entirely
# (→ amount None) rather than silently truncating to a wrong value.
_MONEY = (r"(?:(?P<cur1>[A-Za-z]{3}|[$＄])\s*)?"
          r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![.,]?\d)"
          r"[^\S\r\n]*[(（]?[^\S\r\n]*(?P<cur2>[A-Za-z]{3})?[^\S\r\n]*[)）]?")

# Amount is extracted ONLY from an explicit amount LABEL at a line start (EN deposit/withdrawal amount, JP 入金額/
# 出金額/金額) — never a free body scan — so an account number, a date part, a fee or a prose figure can't be grabbed.
_LABELED_AMOUNT = re.compile(_LINE + r"(?:deposit amount|withdrawal amount|amount|入金額|出金額|金額)" + _SEP + _MONEY,
                             re.IGNORECASE)
# Fee is extracted ONLY from an explicit fee label at a line start (EN fee, JP 手数料). A fee of 0 is a LEGITIMATE
# explicit value and is preserved (Decimal('0')) — only an ABSENT fee label yields None (never fabricated).
_LABELED_FEE = re.compile(_LINE + r"(?:fee|fees|手数料)" + _SEP + _MONEY, re.IGNORECASE)
# Account number from its explicit FULL label at a line start (EN "account number/no./#", JP 口座番号) — the bare words
# "account"/"口座" are deliberately NOT accepted, so a prose "your account <N>" / "銀行口座 <N>" can never shadow the
# labelled detail row. 4–12 digits.
_ACCOUNT = re.compile(_LINE + r"(?:account\s+(?:number|no\.?)|account\s*#|口座番号)" + _SEP + r"(?P<acct>\d{4,12})\b",
                      re.IGNORECASE)


def _norm_currency(tok: str) -> str:
    t = (tok or "").strip().upper()
    if t in ("$", "＄"):
        return "USD"
    return t if t in _CURRENCIES else ""


# Quoted-reply history must never be classified or extracted as live content: a thread/support reply can quote an
# earlier "Your deposit has been completed" line while the live message says the deposit is still pending. Drop
# quote-prefixed lines (">"/"＞", any indent incl. nbsp / full-width space) and cut everything from a reply delimiter
# onward. (HTML <blockquote>s without a ">" marker are flattened upstream by _html_to_text and cannot be recovered
# here — recorded as a residual limitation; ingestion is forward-only and these notifications are from a noreply
# sender, not a reply thread.)
_QUOTED_LINE = re.compile(r"(?m)^[ \t 　]*[>＞].*$")
_REPLY_DELIM = re.compile(r"(?ims)^[ \t]*(?:-{2,}\s*original message\s*-{2,}|on\b.{0,200}?\bwrote:).*\Z")


def _strip_quoted(text: str) -> str:
    """Remove quoted-reply history so only the live message is parsed (classification AND field extraction)."""
    text = _REPLY_DELIM.sub("", text)
    return _QUOTED_LINE.sub("", text)


def _assertive_clauses(text: str) -> list:
    """Return the clauses that ASSERT a fact (conditional/hypothetical clauses removed), each matched independently so
    the COMPLETED/REJECTED/PROCESSING anchors are never read out of a hypothetical footer, and a "your <txn> …" anchor
    can never span across an unrelated clause."""
    return [s.strip() for s in _SENTENCE_SPLIT.split(text)
            if s.strip() and not _CONDITIONAL.search(s)]


def _classify_lifecycle(subject: str, body: str) -> Optional[str]:
    """Map IS6 wording to a lifecycle event type, or None if there is no recognisable deposit/withdrawal signal.
    Deposit is checked first (IS6's genuine certified template is a deposit); withdrawal mirrors TradersWay. Within
    each, REJECTED/COMPLETED precede PROCESSING precede REQUESTED. Completion/rejection/processing are matched ONLY on
    the ASSERTIVE clauses (conditional/hypothetical clauses removed) and only when asserted about the member's OWN
    transaction in perfect/past tense — so completion is never inferred from a request, a generic rule, a future
    promise, a negation, or a footer; REQUESTED falls back to the full text (the safe default)."""
    t = f"{subject}\n{body}"
    clauses = _assertive_clauses(t)

    def _asserts(rx):
        return any(rx.search(c) for c in clauses)

    is_deposit = bool(_DEPOSIT_CTX.search(t))
    is_withdrawal = bool(_WITHDRAWAL_CTX.search(t))
    # When BOTH words appear (e.g. withdrawal boilerplate in a deposit notice), the DOMINANT context is whichever the
    # subject is about; fall back to deposit (the certified path) only if the subject is silent but the body deposits.
    subj = subject
    if _DEPOSIT_CTX.search(subj) and not _WITHDRAWAL_CTX.search(subj):
        kind = "DEPOSIT"
    elif _WITHDRAWAL_CTX.search(subj) and not _DEPOSIT_CTX.search(subj):
        kind = "WITHDRAWAL"
    elif is_deposit and not is_withdrawal:
        kind = "DEPOSIT"
    elif is_withdrawal and not is_deposit:
        kind = "WITHDRAWAL"
    else:
        return None                                   # ambiguous or neither → not a recognisable lifecycle (fail-closed)

    # KIND-SPECIFIC anchors: a completion/rejection of the OTHER direction's token never advances THIS message.
    if _asserts(_REJECTED[kind]):
        return f"{kind}_REJECTED"
    if _asserts(_COMPLETED[kind]):                    # an asserted "your <this-kind txn> … completed" clause (body)
        return f"{kind}_COMPLETED"
    if _asserts(_PROCESSING[kind]):
        return f"{kind}_PROCESSING"
    if _REQUESTED.search(t):                          # the safe fallback: a request is matched on the full text
        return f"{kind}_REQUESTED"
    return None                                       # a deposit/withdrawal word but no lifecycle verb → not resolvable


def _classify_category(lifecycle: Optional[str], subject: str, body: str) -> str:
    """Category is tied to the lifecycle, bypass-free: a DEPOSIT_* lifecycle is DEPOSIT (so it can NEVER enter the
    EXTERNAL_WITHDRAWAL projection); an internal-transfer is INTERNAL_TRANSFER on explicit positive evidence; a
    withdrawal lifecycle WITHOUT internal evidence is UNKNOWN — there is NO prose→EXTERNAL path, so external is never
    guessed from IS6 text (certified against a genuine external sample)."""
    if lifecycle and lifecycle.startswith("DEPOSIT"):
        return "DEPOSIT"
    if _INTERNAL_EVIDENCE.search(f"{subject}\n{body}"):
        return "INTERNAL_TRANSFER"
    return "UNKNOWN"


def _extract_money(m) -> tuple:
    """Turn a matched _MONEY group into (Decimal|None, currency). Number normalisation (mirrors TradersWay so the two
    parsers agree): US thousands grouping '1,250.00' → strip commas; European decimal comma '12,34' → dot; plain
    otherwise. Currency is whitelist-validated (never fabricated). Returns (None, '') when the number cannot be parsed
    — never a silently mis-scaled value."""
    if not m:
        return None, ""
    raw = m.group("num")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d{1,2})?", raw):
        raw = raw.replace(",", "")                    # US thousands grouping
    elif re.fullmatch(r"\d+,\d{1,2}", raw):
        raw = raw.replace(",", ".")                   # European decimal comma
    cur = _norm_currency(m.group("cur2")) or _norm_currency(m.group("cur1"))
    try:
        return Decimal(raw), cur
    except (InvalidOperation, ValueError):
        return None, ""


class IS6Parser:
    name = "is6"
    version = "v1"

    def can_parse(self, *, subject: str, body: str, from_address: str) -> bool:
        # EXACT registrable-domain / subdomain match on the From domain — NOT a substring (a look-alike like
        # "evilis6.com" or "is6.com.evil.com" is rejected). Sender authenticity (Authentication-Results) is enforced
        # separately in the ingestion gate; the From is not trusted on its own.
        from .broker_senders import _domain_of, domain_matches
        if not domain_matches(_domain_of(from_address), _SENDER_DOMAIN):
            return False
        t = f"{subject}\n{body}".lower()
        return any(k in t or k in f"{subject}\n{body}" for k in _KEYWORDS)   # EN via lower, JP (no case) via raw

    def parse(self, *, subject: str, body: str, from_address: str) -> Optional[ParsedBrokerEvent]:
        body = _strip_quoted(body)                    # never classify or extract quoted-reply history as live content
        lifecycle = _classify_lifecycle(subject, body)
        if lifecycle is None:
            return None                               # not a recognisable deposit/withdrawal-lifecycle message
        category = _classify_category(lifecycle, subject, body)
        amount, currency = _extract_money(_LABELED_AMOUNT.search(body))
        if amount is not None and amount <= 0:
            amount, currency = None, ""               # a non-positive "amount" is not a real amount (never fabricate)
        fee, fee_currency = _extract_money(_LABELED_FEE.search(body))   # fee of 0 is kept; absent → None
        # Account from the labelled detail row(s). 0 or >1 DISTINCT labelled accounts ⇒ '' (UNRESOLVED — never guess
        # one, never let a second labelled row silently win): a single unambiguous labelled account only.
        accts = {m.group("acct") for m in _ACCOUNT.finditer(body)}
        acct = next(iter(accts)) if len(accts) == 1 else ""
        # Direction: a deposit's account is the DESTINATION (money enters it); anything else treats it as the SOURCE
        # (money leaving / the account acted on). resolve_transaction_account matches EITHER slot to the owner's own
        # GuvFX accounts, so attribution is unaffected; the slot records the broker-stated direction.
        source_account = "" if category == "DEPOSIT" else acct
        destination_account = acct if category == "DEPOSIT" else ""
        return ParsedBrokerEvent(
            event_type=lifecycle,
            transaction_category=category,
            broker="IS6",
            amount=amount,
            currency=currency,
            occurred_at=None,                         # broker-stated local time has NO explicit tz — never fabricated
            broker_reference_id="",                   # IS6's certified template carries no safe reference label
            source_account=source_account,
            destination_account=destination_account,
            fee=fee,
            fee_currency=fee_currency,
            confidence=Decimal("0.800"))


# IS6 is registered by the single canonical entry point ``parsers_tradersway.register_default_parsers()`` (called by
# the WP3b worker at start-up, alongside TradersWay) — NOT at import, so the registry stays empty (DARK) until the
# worker is armed.


# Static assertion the class satisfies the Protocol (cheap guard; no runtime effect).
_PROTOCOL_OK: BrokerEmailParser = IS6Parser()
