"""Broker Intelligence — identity foundation (WP1).

``BrokerEmailAlias``: ONE opaque, non-enumerable email alias per Model-A BrokerAccount lifecycle instance
(``<opaque>@accounts.guvfx.com``). The alias is the stable identity a broker's transactional mail (withdrawals,
etc.) is addressed to; a later, separately-deployed ingestion service resolves ``To:``-alias → BrokerAccount.

INVARIANTS (see docs/BROKER_EMAIL_IDENTITY_DESIGN.md):
  * **Never reused / never reassigned.** ``alias_local`` is globally unique across ALL rows (active + retired), so a
    retired token is permanently reserved. A re-added account is a NEW Model-A instance (new pk) → a NEW alias.
  * **Per instance.** OneToOne to ``trading.TradingAccount``; NULLABLE to admit Journey B (alias minted before the
    broker account exists), bound write-once later.
  * **Opaque.** The local-part is a server-generated CSPRNG token embedding NO account id / user / broker / PII.
  * **Lifecycle:** RESERVED(=PENDING) → ACTIVE (bound) → RETIRED (terminal; row + token retained forever).
  * Carries NO credential and NO forward FK to event/withdrawal tables (those FK TO the alias later). It is a
    public *identifier*, not a secret (never Fernet-encrypted), but PII-adjacent (owner-scoped; masked in logs).
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction


def alias_domain() -> str:
    """The alias domain, configuration not a constant (data rule). Default ``accounts.guvfx.com``; a deploy may
    point at a different domain. Snapshotted into ``domain_at_creation`` at mint so a later domain change never
    orphans a brokerage registration."""
    return str(getattr(settings, "BROKER_EMAIL_ALIAS_DOMAIN", "") or "accounts.guvfx.com").strip()


class BrokerEmailAlias(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Reserved (minted, not yet bound)"
        ACTIVE = "ACTIVE", "Active (bound to a live account)"
        RETIRED = "RETIRED", "Retired (tombstoned/abandoned; never reused)"

    class Journey(models.TextChoices):
        CONNECT_EXISTING = "CONNECT_EXISTING", "Connect an existing broker account"
        OPEN_NEW = "OPEN_NEW", "Open a new broker account (alias before account)"

    # The opaque local-part — the global never-reuse anchor (unconditional unique; see Meta).
    alias_local = models.CharField(max_length=64, unique=True, editable=False, db_index=True)
    # Immutable snapshot of the domain at mint (so a later domain change never changes a registered address).
    domain_at_creation = models.CharField(max_length=255, editable=False)
    # The Model-A lifecycle instance. NULLABLE for Journey B (minted before the account exists); bound write-once.
    trading_account = models.OneToOneField(
        "trading.TradingAccount", on_delete=models.PROTECT, null=True, blank=True,
        related_name="broker_email_alias")
    # Owner — set at mint so a journey-B alias has an owner before any account exists.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="broker_email_aliases")
    origin_journey = models.CharField(max_length=24, choices=Journey.choices, default=Journey.CONNECT_EXISTING)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    bound_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    _IMMUTABLE = ("alias_local", "domain_at_creation")

    class Meta:
        constraints = [
            # UNCONDITIONAL (all-rows) uniqueness — a RETIRED token stays occupied forever ⇒ never recyclable.
            models.UniqueConstraint(fields=["alias_local"], name="uniq_broker_email_alias_local"),
        ]
        indexes = [models.Index(fields=["user", "status"])]

    def address(self) -> str:
        """The full routable address, assembled from the opaque local-part + the pinned domain. Pure; no I/O."""
        return f"{self.alias_local}@{self.domain_at_creation}"

    def masked(self) -> str:
        """A log/audit-safe rendering that never exposes the full opaque token (PII-adjacent linker)."""
        lp = self.alias_local or ""
        head = lp[:3] if len(lp) > 3 else "***"
        return f"{head}…@{self.domain_at_creation}"

    def save(self, *args, **kwargs):
        # Model-layer immutability (binds the DRF full-save path too): the opaque token + domain are write-once
        # always; the trading_account bind is write-once (the single NULL→account transition for Journey B is
        # allowed exactly once); status is monotonic and RETIRED is terminal.
        if self.pk is not None:
            prior = type(self).objects.filter(pk=self.pk).first()
            if prior is not None:
                for f in self._IMMUTABLE:
                    if getattr(prior, f) != getattr(self, f):
                        raise ValidationError(f"BrokerEmailAlias.{f} is immutable")
                if prior.trading_account_id is not None and prior.trading_account_id != self.trading_account_id:
                    raise ValidationError("BrokerEmailAlias.trading_account is write-once (cannot be re-pointed)")
                if prior.status == self.Status.RETIRED and self.status != self.Status.RETIRED:
                    raise ValidationError("BrokerEmailAlias.status RETIRED is terminal")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"BrokerEmailAlias({self.masked()}, {self.status})"


# ======================================================================================================
# WP2 — Evidence + event/withdrawal schema (DARK; no writers until the WP3 ingestion service).
# ======================================================================================================

class Provenance(models.TextChoices):
    """Where a record's data came from. DEFAULT is SYNTHETIC on both BrokerEvent and Withdrawal — it is NEVER
    allowed to default to REAL, so an un-labelled row can never masquerade as a genuine broker observation
    (Sponsor rule: synthetic fixtures must be labelled synthetic; only an explicit real-ingestion path sets REAL)."""
    SYNTHETIC = "SYNTHETIC", "Synthetic fixture (not a real broker email)"
    SANITISED = "SANITISED", "Real email, sanitised (structure/fields preserved)"
    REAL = "REAL", "Genuine, un-sanitised broker observation"


class TransactionCategory(models.TextChoices):
    """WHAT KIND of money movement a message is about — DISTINCT from the lifecycle status (``event_type``). The
    Withdrawal projection (WP5) counts ONLY ``EXTERNAL_WITHDRAWAL``; INTERNAL_TRANSFER/DEPOSIT/UNKNOWN never pollute
    withdrawal statistics. A message is EXTERNAL_WITHDRAWAL ONLY with positive evidence money LEFT the broker
    (external destination / payment method / receiving institution / explicit external type) — NEVER from the word
    'withdrawal', the subject, the sender, an amount or an account number alone. Ambiguous ⇒ UNKNOWN (never guess).
    (Sponsor packet 2026-10-08 §8: a TradersWay 'confirm your withdrawal request' email was in fact an internal
    transfer; it must classify as NOT-external and never enter withdrawal metrics.)"""
    EXTERNAL_WITHDRAWAL = "EXTERNAL_WITHDRAWAL", "Money leaving the broker environment"
    INTERNAL_TRANSFER = "INTERNAL_TRANSFER", "Transfer between accounts inside the broker"
    DEPOSIT = "DEPOSIT", "Money entering the broker environment"
    UNKNOWN = "UNKNOWN", "Transaction type not resolvable from evidence (never guessed)"


# Open vocabulary (registry, not a DB CHECK): the withdrawal LIFECYCLE event types the deterministic parser may
# emit. ``event_type`` is a free CharField so a new broker template can introduce a value without a migration; this
# tuple is the authoritative reference list used by parsers/tests and surfaced in the admin/read models. Lifecycle is
# orthogonal to category: a CONFIRMATION_REQUIRED email can be an internal transfer (category UNKNOWN/INTERNAL).
BROKER_EVENT_TYPES = (
    "WITHDRAWAL_REQUESTED",
    "WITHDRAWAL_CONFIRMATION_REQUIRED",
    "WITHDRAWAL_CONFIRMED",
    "WITHDRAWAL_PROCESSING",
    "WITHDRAWAL_APPROVED",
    "WITHDRAWAL_COMPLETED",
    "WITHDRAWAL_REJECTED",
    "WITHDRAWAL_CANCELLED",
    # Deposit lifecycle (IS6 parser, PR-B). Orthogonal to category: a DEPOSIT_* lifecycle always carries category
    # DEPOSIT, which the withdrawal projection NEVER counts — a deposit can therefore never enter withdrawal metrics.
    "DEPOSIT_REQUESTED",
    "DEPOSIT_PROCESSING",
    "DEPOSIT_COMPLETED",
    "DEPOSIT_REJECTED",
)


class EvidenceBlob(models.Model):
    """Immutable, content-addressed pointer to ONE stored raw broker message (the ``evidence_ref`` of the design).

    POINTER, NOT PAYLOAD: the raw bytes live in a configurable external store (see ``evidence.EvidenceStore``);
    this row holds only the integrity hash + the storage pointer + metadata, never the body inline (data rule:
    no bulk/raw data in the DB or Git). Content-addressed by ``sha256`` (unique ⇒ identical messages de-dup to one
    blob), so the store is inherently write-once: a key maps to exactly one byte-sequence. Fully immutable — no
    UPDATE and no DELETE — enforced at BOTH the app layer (``save()``/``delete()`` overrides) AND the DB layer
    (BEFORE-UPDATE + BEFORE-DELETE triggers, migration 0002), so the ORM bulk paths (``QuerySet.update()`` /
    ``QuerySet.delete()``) and raw SQL — none of which call the instance overrides — are blocked too. Raw evidence
    is quarantined, never destroyed (data rule); an event additionally PROTECTs its blob."""

    class Source(models.TextChoices):
        EMAIL = "EMAIL", "Broker transactional email"

    sha256 = models.CharField(max_length=64, unique=True, editable=False, db_index=True)
    storage_backend = models.CharField(max_length=16, default="file", editable=False)
    storage_key = models.CharField(max_length=255, editable=False)   # pointer within the backend, NOT the payload
    byte_size = models.PositiveBigIntegerField(editable=False)
    content_type = models.CharField(max_length=64, default="message/rfc822", editable=False)
    source = models.CharField(max_length=16, choices=Source.choices, default=Source.EMAIL, editable=False)
    received_at = models.DateTimeField(editable=False)               # when the ingestion service received it
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["sha256"])]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("EvidenceBlob is immutable (content-addressed, write-once)")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("EvidenceBlob is immutable raw evidence; it is never deleted (quarantine, not destroy)")

    def __str__(self) -> str:
        return f"EvidenceBlob({self.sha256[:12]}…, {self.byte_size}B)"


class BrokerEvent(models.Model):
    """One append-only, evidence-backed observation parsed from a broker message (e.g. a withdrawal was REQUESTED/
    COMPLETED). Append-only EVIDENCE: every evidential field is write-once and the row is never deleted — enforced
    at the app layer (``save()``/``delete()``) AND the DB layer (a BEFORE-UPDATE trigger that rejects any change to
    an evidential column, and a BEFORE-DELETE trigger), so the ORM bulk paths (``QuerySet.update()`` /
    ``QuerySet.delete()``) and raw SQL are blocked too. Corrections are NEW rows, never edits. The single
    non-evidential field ``correlation_status`` MAY advance later (WP5) — it records bookkeeping, not what the
    broker said.

    Amount/currency/occurred_at/reference-id are NULLABLE and NEVER fabricated — absent in the source ⇒ NULL here.
    The event FKs TO the account (+ the alias it was addressed to, for attribution); there is no forward FK from
    the alias. ``provenance`` defaults SYNTHETIC so a fixture can never be mistaken for a real broker observation."""

    class CorrelationStatus(models.TextChoices):
        UNRESOLVED = "UNRESOLVED", "Not yet correlated to a withdrawal"
        CORRELATED = "CORRELATED", "Correlated to exactly one withdrawal"
        AMBIGUOUS = "AMBIGUOUS", "Multiple/again candidates — needs review, never guessed"

    # Attribution + resolution. Both nullable: an event can be captured before (or without) account resolution.
    alias = models.ForeignKey(BrokerEmailAlias, on_delete=models.PROTECT, null=True, blank=True,
                              related_name="broker_events")
    trading_account = models.ForeignKey("trading.TradingAccount", on_delete=models.PROTECT, null=True, blank=True,
                                        related_name="broker_events")
    broker = models.CharField(max_length=64, blank=True, default="")
    source = models.CharField(max_length=16, choices=EvidenceBlob.Source.choices, default=EvidenceBlob.Source.EMAIL)
    event_type = models.CharField(max_length=48)                      # open vocab (BROKER_EVENT_TYPES registry) = LIFECYCLE
    # WHAT KIND of movement (orthogonal to lifecycle). Defaults UNKNOWN — never guessed external. The withdrawal
    # projection counts ONLY EXTERNAL_WITHDRAWAL, so an internal transfer / ambiguous message can never inflate stats.
    transaction_category = models.CharField(max_length=24, choices=TransactionCategory.choices,
                                            default=TransactionCategory.UNKNOWN)
    occurred_at = models.DateTimeField(null=True, blank=True)         # broker-stated time (may be unknown → NULL)
    received_at = models.DateTimeField()                              # when the ingestion service received it
    broker_reference_id = models.CharField(max_length=128, blank=True, default="")
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)   # never fabricated
    currency = models.CharField(max_length=8, blank=True, default="")
    # The transaction's own source/destination trading-account numbers AS STATED by the broker (parsed, never
    # fabricated — absent ⇒ ''). These are the broker's internal account identifiers for the money movement; they are
    # DISTINCT from ``trading_account`` (the resolved GuvFX account, set only on a verified match — see below) and
    # must not be conflated with the recipient-mailbox owner's account.
    source_account = models.CharField(max_length=64, blank=True, default="")
    destination_account = models.CharField(max_length=64, blank=True, default="")
    # Broker-stated fee (parsed; NULL when absent, never fabricated). Evidential/write-once.
    fee = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    fee_currency = models.CharField(max_length=8, blank=True, default="")
    # Verified mailbox OWNER (the GuvFX user whose connected mailbox/alias received this mail). Records ownership +
    # broker relationship INDEPENDENTLY of ``trading_account`` — so an event can be owner-attributed even when the
    # specific transaction trading-account is UNRESOLVED (no GuvFX account matches the transaction's own accounts).
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="broker_events")
    # Pointer to the stored raw message; PROTECT so evidence can't be orphaned by deleting the blob.
    evidence = models.ForeignKey(EvidenceBlob, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="events")
    evidence_hash = models.CharField(max_length=64, blank=True, default="")   # sha256 of the raw msg (tamper-evident)
    parser_name = models.CharField(max_length=64, blank=True, default="")
    parser_version = models.CharField(max_length=32, blank=True, default="")
    confidence = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)   # 0.000–1.000
    correlation_status = models.CharField(max_length=12, choices=CorrelationStatus.choices,
                                          default=CorrelationStatus.UNRESOLVED)   # the ONLY mutable field
    provenance = models.CharField(max_length=12, choices=Provenance.choices, default=Provenance.SYNTHETIC)
    created_at = models.DateTimeField(auto_now_add=True)

    # Evidential columns — write-once. ``correlation_status`` is deliberately absent (it may advance in WP5).
    _IMMUTABLE = (
        "alias_id", "trading_account_id", "broker", "source", "event_type", "transaction_category", "occurred_at",
        "received_at", "broker_reference_id", "amount", "currency", "source_account", "destination_account",
        "fee", "fee_currency", "evidence_id", "evidence_hash", "parser_name", "parser_version", "confidence",
        "provenance",
    )   # ``owner`` is deliberately NOT here: it is set-once at ingest but SET_NULL-able if the user is deleted.

    class Meta:
        ordering = ["id"]   # chronological, append-only
        constraints = [
            # Per-OWNER content-addressed idempotency backstop: one (owner, EvidenceBlob) -> at most one BrokerEvent.
            # Structural guarantee (not just the app-layer filter-then-create), so a concurrent/redelivered re-ingest
            # by the SAME owner cannot persist a duplicate event. Owner-scoped (NOT global) so two DIFFERENT mailbox
            # owners who receive BYTE-IDENTICAL raw bytes share the ONE content-addressed EvidenceBlob yet each keep
            # their OWN owner-attributed event — closing the cross-owner inheritance leak without discarding either
            # authentic delivery (MULTI_ACCOUNT §D; security.md no-cross-user-leak; data.md never-drop-evidence).
            # The live mailbox path ALWAYS writes a non-null owner (the mailbox owner), so every real event is covered.
            # The owner-null bucket (synthetic/legacy callers only) relies on the app-layer idempotency filter; a hard
            # per-blob unique there is deliberately OMITTED — it would hit Postgres NULLS-DISTINCT, and because owner is
            # SET_NULL on user deletion it would collide a to-be-nulled sibling for a shared blob and deadlock the
            # operator purge (see migration 0014).
            models.UniqueConstraint(fields=["owner", "evidence"],
                                    condition=models.Q(evidence__isnull=False, owner__isnull=False),
                                    name="uniq_brokerevent_evidence_owner"),
        ]
        indexes = [
            models.Index(fields=["trading_account", "id"]),
            models.Index(fields=["broker_reference_id"]),
            models.Index(fields=["correlation_status"]),
            models.Index(fields=["transaction_category"]),   # the withdrawal projection filters EXTERNAL_WITHDRAWAL
        ]

    def save(self, *args, **kwargs):
        # App-layer mirror of the DB trigger: evidential content is write-once; only correlation_status may change.
        if self.pk is not None:
            prior = type(self).objects.filter(pk=self.pk).first()
            if prior is not None:
                for f in self._IMMUTABLE:
                    if getattr(prior, f) != getattr(self, f):
                        raise ValidationError(f"BrokerEvent.{f} is append-only/immutable; corrections are new rows")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("BrokerEvent is append-only evidence; it is never deleted")

    def effective_transaction_category(self) -> str:
        """The EFFECTIVE transaction category (ADR-0059): the latest valid append-only classification revision's
        derived category when one exists, else the original (immutable) ``transaction_category``. Deterministic — the
        newest revision by id (each revision supersedes the prior effective one). This NEVER mutates the stored
        category; a correction is a new ``BrokerEventClassificationRevision`` row. The withdrawal projection consumes
        this only through the correlation engine (a batch job), never a per-page reclassify."""
        rev = self.classification_revisions.order_by("-id").values_list("derived_category", flat=True).first()
        return rev or self.transaction_category

    def __str__(self) -> str:
        return f"BrokerEvent({self.event_type}, acct={self.trading_account_id}, {self.correlation_status})"


class BrokerEventClassificationRevision(models.Model):
    """WP6.1 / ADR-0059 — an append-only, write-once CLASSIFICATION CORRECTION for a ``BrokerEvent``.

    The original ``BrokerEvent`` (and its stored ``transaction_category``) and the ``EvidenceBlob`` are NEVER mutated;
    a revision records a category RE-DERIVED from the SAME immutable evidence under the current certified parser, with
    full provenance. The EFFECTIVE category of an event (``BrokerEvent.effective_transaction_category``) is the latest
    valid revision's ``derived_category``, else the original. A revision is ONLY ever parser-derived — there is NO
    operator-supplied category override. Write-once + never-deleted, like the evidence it corrects (data.md: raw
    evidence is immutable; corrections are new records). It carries NO email body, URL, token, full account or request
    number — only a category, provenance, a safe evidence hash, and a deterministic idempotency key."""
    event = models.ForeignKey("BrokerEvent", on_delete=models.PROTECT, related_name="classification_revisions")
    derived_category = models.CharField(max_length=32, choices=TransactionCategory.choices)
    original_category = models.CharField(max_length=32, editable=False)          # event's stored category at revision time (audit)
    original_parser_version = models.CharField(max_length=32, blank=True, default="", editable=False)  # historical parser
    parser_name = models.CharField(max_length=64, editable=False)                # corrected parser identity
    parser_version = models.CharField(max_length=32, editable=False)
    classification_rule_version = models.CharField(max_length=32, editable=False)
    reason = models.CharField(max_length=64, editable=False)                     # reason CODE (e.g. stale_ingestion_reclassify)
    source_evidence_sha256 = models.CharField(max_length=64, editable=False)     # re-read + integrity-verified evidence hash
    actor = models.CharField(max_length=128, editable=False)                     # initiating process/actor (e.g. cmd:reclassify_broker_event)
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="superseded_by", editable=False)
    idempotency_key = models.CharField(max_length=64, unique=True, editable=False)  # deterministic; rerun → no duplicate
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["event", "id"])]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("BrokerEventClassificationRevision is write-once; a correction is a new row")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("BrokerEventClassificationRevision is append-only; it is never deleted")

    def __str__(self) -> str:
        return f"ClassificationRevision(event={self.event_id}, {self.original_category}->{self.derived_category})"


class Withdrawal(models.Model):
    """The durable, correlated withdrawal record projected from one-or-more ``BrokerEvent``s. Unlike an event this
    IS mutable (status advances as evidence accrues) but only ever on positive evidence — the advance logic + the
    monotonic-status guard belong to the WP5 correlation engine; this WP2 schema just carries the state + the
    evidence links. Idempotent correlation is anchored by a partial-unique ``(trading_account, broker_reference_id)``
    (when a reference id is present) so the same broker withdrawal can never spawn two rows.

    ``status`` semantics: REQUESTED→PROCESSING→COMPLETED/REJECTED/CANCELLED on evidence; **PENDING = requested but
    completion not yet observed (NOT a failure)**; UNRESOLVED = correlated to an account but otherwise ambiguous."""

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested"
        PROCESSING = "PROCESSING", "Processing"
        COMPLETED = "COMPLETED", "Completed"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"
        PENDING = "PENDING", "Requested; completion not yet observed"
        UNRESOLVED = "UNRESOLVED", "Ambiguous / not resolvable"

    class CorrelationMethod(models.TextChoices):
        REFERENCE_ID = "REFERENCE_ID", "Broker reference id (deterministic)"
        HEURISTIC = "HEURISTIC", "Bounded heuristic (account+amount+currency+time window)"

    # Account-OPTIONAL (ADR-0050): an owner-attributed withdrawal whose TradersWay account is not registered in GuvFX
    # stays account-UNRESOLVED (trading_account NULL) rather than being bound to an unrelated account. PROTECT is kept
    # (a withdrawal still pins a resolved account against deletion); nullability is the only change.
    trading_account = models.ForeignKey("trading.TradingAccount", on_delete=models.PROTECT,
                                        related_name="withdrawals", null=True, blank=True)
    # Owner attribution, INDEPENDENT of trading_account — mirrors BrokerEvent.owner (SET_NULL so an operator
    # User.delete() purge decays attribution to NULL without destroying the financial-evidence row). Populated by the
    # correlation engine from the originating event's owner. Enables surfacing account-UNRESOLVED withdrawals.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="owner_withdrawals")
    broker_reference_id = models.CharField(max_length=128, blank=True, default="")
    amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True, default="")
    requested_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED)
    requested_event = models.ForeignKey(BrokerEvent, on_delete=models.PROTECT, null=True, blank=True,
                                        related_name="+")
    completed_event = models.ForeignKey(BrokerEvent, on_delete=models.PROTECT, null=True, blank=True,
                                        related_name="+")
    correlation_method = models.CharField(max_length=16, choices=CorrelationMethod.choices, blank=True, default="")
    provenance = models.CharField(max_length=12, choices=Provenance.choices, default=Provenance.SYNTHETIC)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            # Idempotency is split in TWO so it holds for BOTH account-resolved and owner-attributed/account-NULL
            # withdrawals (Postgres treats NULL trading_account as DISTINCT, so a single (trading_account, ref) partial
            # index would NOT dedupe account-NULL rows — ADR-0050; same owner-scoping idiom as migration 0014 for
            # BrokerEvent). Both partial: only a present reference id is uniqueness-bearing (blank refs are
            # heuristic/unresolved).
            models.UniqueConstraint(
                fields=["trading_account", "broker_reference_id"],
                condition=models.Q(broker_reference_id__gt="", trading_account__isnull=False),
                name="uniq_withdrawal_account_reference"),
            models.UniqueConstraint(
                fields=["owner", "broker_reference_id"],
                condition=models.Q(broker_reference_id__gt="", trading_account__isnull=True, owner__isnull=False),
                name="uniq_withdrawal_owner_reference"),
        ]
        indexes = [
            models.Index(fields=["trading_account", "status"]),
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["status"]),
        ]

    def duration_seconds(self):
        """Completed-minus-requested in whole seconds, or None if not both observed. Pure; no I/O."""
        if self.requested_at and self.completed_at:
            return int((self.completed_at - self.requested_at).total_seconds())
        return None

    def __str__(self) -> str:
        return f"Withdrawal(acct={self.trading_account_id}, {self.status}, ref={self.broker_reference_id or '-'})"


# ======================================================================================================
# WP5 — Multi-mailbox foundation (DARK; no ingestion/OAuth wired here — that is WP3b).
# Three DISTINCT identities (Sponsor packet 2026-10-08 §4, do NOT conflate):
#   ConnectedMailbox   = a real inbox GuvFX reads (owns the encrypted OAuth token ref + sync cursor)
#   BrokerEmailIdentity = an address a broker sends to (0/1 BrokerAccount; 0/1 receiving mailbox)
#   BrokerEmailAlias   = the permanent opaque GuvFX alias (defined above, unchanged)
# ======================================================================================================

class ConnectedMailbox(models.Model):
    """A mailbox GuvFX is authorised to READ for broker mail. Holds only a POINTER to an encrypted OAuth token in the
    ingestion service's own store (``credential_ref``) — never the token itself (no secret in the DB). One GuvFX user
    may own many mailboxes. Dedup is by the PROVIDER-STABLE id, never the email string, so a Gmail mailbox addressed
    as both ``…@gmail.com`` and ``…@googlemail.com`` cannot be connected twice (§5). Grants NO trading/MT5/execution
    authority and NO send/delete scope."""

    class Provider(models.TextChoices):
        GMAIL = "GMAIL", "Gmail (personal or Workspace)"
        IMAP = "IMAP", "Generic IMAP"
        OUTLOOK = "OUTLOOK", "Microsoft Outlook"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting OAuth consent"
        CONNECTED = "CONNECTED", "Connected (read-only)"
        REVOKED = "REVOKED", "Disconnected / consent revoked"
        ERROR = "ERROR", "Needs re-auth"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="connected_mailboxes")
    provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.GMAIL)
    # Provider-stable mailbox identity (e.g. Gmail account id / 'sub') — the dedup anchor (NOT the email string).
    provider_mailbox_id = models.CharField(max_length=255)
    primary_email = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    credential_ref = models.CharField(max_length=255, blank=True, default="")   # pointer to the encrypted token; NOT the token
    scopes = models.CharField(max_length=512, blank=True, default="")           # granted read-only scope(s)
    cursor_state = models.CharField(max_length=255, blank=True, default="")     # Gmail historyId / IMAP UIDVALIDITY+UID
    last_successful_sync = models.DateTimeField(null=True, blank=True)
    # INGESTION_INCIDENT_R1 (§5) — DARK mailbox-level health observability. All nullable/defaulted + additive, so the
    # migration is a pure column-add (no backfill, reversible). Written ONLY by the ingestion worker's own per-pass
    # bookkeeping on its OWN row (never evidence, never financial, never a secret) and read by the health read-model.
    # They record HEALTH, not what a broker said; a successful systemd unit exit does NOT imply they say healthy.
    last_attempted_sync = models.DateTimeField(null=True, blank=True)            # when the worker last RAN a pass
    last_pass_cursor_advanced = models.BooleanField(null=True, blank=True)       # did the last pass advance the cursor?
    last_error_code = models.CharField(max_length=48, blank=True, default="")    # secret-free diagnostic code (R1 §4)
    last_error_http_status = models.PositiveSmallIntegerField(null=True, blank=True)  # safe Gmail HTTP status, if any
    last_error_at = models.DateTimeField(null=True, blank=True)                  # when the last failure was recorded
    consecutive_failures = models.PositiveIntegerField(default=0)                # resets to 0 on a successful pass
    # ADR-0054 P0 (ingestion/erasure race): a durable generation bumped on every (re)connect. An in-flight
    # ingestion pass captures the generation of the mailbox it is processing; under the per-owner erasure lock it
    # re-reads the row and refuses its write if the row is gone (erased), REVOKED, owner-mismatched, or the
    # generation advanced (a reconnect) — so a stale pass cannot revive data after erasure + reconnection.
    generation = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One row per underlying mailbox: dedup by (provider, provider-stable id), NOT the email — so gmail vs
            # googlemail (same Google account) cannot double-connect (§5).
            models.UniqueConstraint(fields=["provider", "provider_mailbox_id"], name="uniq_connectedmailbox_provider_id"),
        ]
        indexes = [models.Index(fields=["user", "status"])]

    def masked_email(self) -> str:
        e = self.primary_email or ""
        if "@" not in e:
            return "***"
        local, _, dom = e.partition("@")
        head = local[:2] if len(local) > 2 else "*"
        return f"{head}…@{dom}"

    def __str__(self) -> str:
        return f"ConnectedMailbox({self.provider}:{self.masked_email()}, {self.status})"


class BrokerEmailIdentity(models.Model):
    """An email address registered AT A BROKER (the address a broker's transactional mail is sent to). Distinct from
    a mailbox (what GuvFX reads) and from a GuvFX alias. Bound to 0/1 BrokerAccount lifecycle (NULLABLE until the
    account is onboarded) and 0/1 receiving ConnectedMailbox (NULLABLE until the routing relationship is verified).
    A GuvFX user is attached only once ownership/authorisation is established — NEVER merely because the address was
    listed (§4). Unverified broker-registration identities are retained as PENDING/unbound."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Registered at the broker; ownership/routing not yet verified"
        VERIFIED = "VERIFIED", "Mailbox routing + ownership verified"
        UNRESOLVED = "UNRESOLVED", "Cannot be attributed to an account from available evidence"
        RETIRED = "RETIRED", "No longer in use"

    class Origin(models.TextChoices):
        BROKER_REGISTERED = "BROKER_REGISTERED", "A pre-existing address registered directly with the broker"
        GUVFX_ALIAS = "GUVFX_ALIAS", "A GuvFX-controlled accounts.guvfx.com alias"

    email = models.CharField(max_length=255)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="broker_email_identities")   # attached only after ownership is verified
    broker_name = models.CharField(max_length=64, blank=True, default="")
    trading_account = models.ForeignKey("trading.TradingAccount", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="broker_email_identities")
    connected_mailbox = models.ForeignKey(ConnectedMailbox, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name="broker_email_identities")   # the verified receiving mailbox
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    origin = models.CharField(max_length=20, choices=Origin.choices, default=Origin.BROKER_REGISTERED)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Idempotent registry: one identity row per (email, broker). Re-running the backfill is a no-op.
            models.UniqueConstraint(fields=["email", "broker_name"], name="uniq_brokeremailidentity_email_broker"),
        ]
        indexes = [
            models.Index(fields=["email"]),
            models.Index(fields=["trading_account"]),
            models.Index(fields=["status"]),
        ]

    def masked_email(self) -> str:
        e = self.email or ""
        if "@" not in e:
            return "***"
        local, _, dom = e.partition("@")
        head = local[:2] if len(local) > 2 else "*"
        return f"{head}…@{dom}"

    def __str__(self) -> str:
        return f"BrokerEmailIdentity({self.masked_email()}@{self.broker_name or '?'}, {self.status})"


class BrokerEmailIdentityAudit(models.Model):
    """WP7 — append-only history of identity VERIFICATION / BINDING actions. Every explicit verify, confirm, reject or
    unbind of a ``BrokerEmailIdentity`` writes one row recording WHO acted, for WHICH claimed owner/broker/account,
    the PROOFS relied on, the OUTCOME, and the before/after status — so every ownership attribution is attributable
    and reviewable. A REJECT (proof failed / ambiguous / cross-user) is audited too, even though it creates/changes
    no identity row, because a refused cross-user attempt is exactly what review must see. Never updated or deleted
    (the auditable record of how an identity came to be VERIFIED); the identity FK is PROTECT so a bound identity
    carrying history is not destroyed. Holds NO credential and NO raw email body — only ids, statuses, a masked
    address, and a structured proof summary."""

    class Action(models.TextChoices):
        VERIFY = "VERIFY", "Verified + bound (owner/broker/account/mailbox)"
        CONFIRM = "CONFIRM", "Re-verify of an already-VERIFIED identity (no change)"
        REJECT = "REJECT", "Verification refused (proof failed / ambiguous / cross-user)"
        UNBIND = "UNBIND", "Binding reversed (returned to PENDING/UNRESOLVED)"

    # Nullable: a REJECT may have no identity row (the attempt is refused before any VERIFIED binding exists).
    identity = models.ForeignKey(BrokerEmailIdentity, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="audit")
    action = models.CharField(max_length=16, choices=Action.choices)
    # The GuvFX user the action was attributed to (the CLAIMED owner). SET_NULL-able (user deletion never erases audit).
    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    actor = models.CharField(max_length=128, blank=True, default="")      # who performed it (user label / 'system')
    broker_name = models.CharField(max_length=64, blank=True, default="")
    email = models.CharField(max_length=255, blank=True, default="")      # subject address (masked at display)
    trading_account = models.ForeignKey("trading.TradingAccount", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="+")
    connected_mailbox = models.ForeignKey(ConnectedMailbox, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name="+")
    outcome = models.CharField(max_length=24, blank=True, default="")     # OK, or a reason code on reject
    reason = models.CharField(max_length=255, blank=True, default="")     # human-readable, sanitised
    status_before = models.CharField(max_length=12, blank=True, default="")
    status_after = models.CharField(max_length=12, blank=True, default="")
    proofs = models.JSONField(default=dict)                               # structured proof summary; no secrets/body
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["identity", "id"]), models.Index(fields=["subject_user"])]

    def masked_email(self) -> str:
        e = self.email or ""
        if "@" not in e:
            return "***"
        local, _, dom = e.partition("@")
        head = local[:2] if len(local) > 2 else "*"
        return f"{head}…@{dom}"

    def delete(self, *args, **kwargs):
        raise ValidationError("BrokerEmailIdentityAudit is append-only; it is never deleted")

    def __str__(self) -> str:
        return f"BrokerEmailIdentityAudit({self.action}, idn={self.identity_id}, {self.outcome})"


# ======================================================================================================
# Broker profiles (DARK) — per-broker configuration so many brokers (that may share one mailbox) each have
# their own approved sender domains, trusted Authentication-Results authserv-ids, expected auth posture,
# parser, MT environments + server names, and account-identification notes. This GENERALISES the single
# ``BROKER_INTELLIGENCE_SENDER_ALLOWLIST`` env into data. Changing a profile is config only — it NEVER touches
# a live MT5 install (servers.dat is a separate governed workflow). Profiles hold NO secrets.
# ======================================================================================================

class BrokerProfile(models.Model):
    class ConfigProvenance(models.TextChoices):
        """How this broker CONFIG (not a transaction) was established. Distinct from the event/withdrawal
        ``Provenance``. Default SEED; an operator attestation or a catalogue mirror is stronger."""
        SEED = "SEED", "Initial pilot seed"
        OPERATOR_VERIFIED = "OPERATOR_VERIFIED", "Verified by an operator against the broker"
        CATALOGUE_MIRRORED = "CATALOGUE_MIRRORED", "Mirrors a verified BrokerServerCatalogue entry"

    class AuthPolicy(models.TextChoices):
        # The deployed, reviewed global policy (R3b): DMARC pass OR a From-aligned DKIM pass. A broker whose mail
        # reliably passes DMARC (e.g. IS6) satisfies it via the DMARC branch; one that only DKIM-aligns (TradersWay,
        # no DMARC record) via the aligned-DKIM branch. This is NOT a per-broker weakening — both go through the same
        # fail-closed verdict. DMARC_ONLY is a STRICTER opt-in; a looser posture would need adversarial review.
        DMARC_OR_ALIGNED_DKIM = "DMARC_OR_ALIGNED_DKIM", "DMARC pass, or From-aligned DKIM pass (default)"
        DMARC_ONLY = "DMARC_ONLY", "DMARC pass only (stricter)"

    name = models.CharField(max_length=128, unique=True)
    slug = models.SlugField(max_length=64, unique=True)
    aliases = models.CharField(max_length=255, blank=True, default="")                 # display aliases, comma-sep
    official_domains = models.CharField(max_length=512, blank=True, default="")         # brand domains, comma-sep
    approved_sender_domains = models.CharField(max_length=512, blank=True, default="")  # email allowlist, comma-sep
    trusted_authserv_ids = models.CharField(max_length=255, blank=True, default="mx.google.com")
    auth_policy = models.CharField(max_length=32, choices=AuthPolicy.choices, default=AuthPolicy.DMARC_OR_ALIGNED_DKIM)
    parser_name = models.CharField(max_length=64, blank=True, default="")
    parser_version = models.CharField(max_length=32, blank=True, default="")
    mt_environments = models.CharField(max_length=64, blank=True, default="")           # e.g. "MT4,MT5"
    server_names = models.CharField(max_length=512, blank=True, default="")
    server_aliases = models.CharField(max_length=512, blank=True, default="")
    account_id_rules = models.TextField(blank=True, default="")                         # descriptive matching notes
    # The TransactionCategory values this broker's deterministic parser can emit (comma-sep UPPER enum values). This
    # is the broker's declared CAPABILITY — a structural guard that a broker can never surface a category its parser
    # cannot actually produce (e.g. TradersWay: INTERNAL_TRANSFER,UNKNOWN — never EXTERNAL_WITHDRAWAL). It documents
    # capability; it does NOT itself cause classification.
    supported_categories = models.CharField(max_length=255, blank=True, default="")
    # How this profile's CONFIG was established (provenance of the config, not of a transaction). Defaults SEED.
    provenance = models.CharField(max_length=20, choices=ConfigProvenance.choices, default=ConfigProvenance.SEED)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["is_active"])]

    @staticmethod
    def _split(s: str):
        import re as _re
        return [p for p in _re.split(r"[,\s]+", (s or "").strip().lower()) if p]

    def approved_domains(self):
        return self._split(self.approved_sender_domains)

    def authserv_ids(self):
        return self._split(self.trusted_authserv_ids)

    def supported_category_list(self):
        """UPPER-cased TransactionCategory tokens the broker's parser may emit (case preserved to match the enum,
        unlike the lowercasing ``_split`` used for domains)."""
        import re as _re
        return [p.upper() for p in _re.split(r"[,\s]+", (self.supported_categories or "").strip()) if p]

    def server_name_list(self):
        """Declared broker server names (case preserved — MT5 server strings are case-sensitive)."""
        import re as _re
        return [p for p in _re.split(r"\s*,\s*", (self.server_names or "").strip()) if p]

    def _snapshot(self) -> dict:
        return {f: getattr(self, f) for f in (
            "name", "slug", "aliases", "official_domains", "approved_sender_domains", "trusted_authserv_ids",
            "auth_policy", "parser_name", "parser_version", "mt_environments", "server_names", "server_aliases",
            "account_id_rules", "supported_categories", "provenance", "is_active")}

    def save(self, *args, **kwargs):
        creating = self._state.adding
        super().save(*args, **kwargs)
        # Append-only config audit (history/evidence). OWN savepoint so a DB error writing the audit row can NEVER
        # poison an enclosing transaction (the WP1 tombstone-poison lesson: a bare write inside a caller's atomic()
        # sets needs_rollback, and a swallowed exception cannot clear it). Best-effort — it never blocks the save.
        try:
            with transaction.atomic():
                # ``_changed_by`` is a transient actor label set by the caller (e.g. the admin's save_model) so the
                # append-only config history records WHO changed it; blank for programmatic/seed saves.
                BrokerProfileAudit.objects.create(broker_profile=self, action="CREATE" if creating else "UPDATE",
                                                  snapshot=self._snapshot(),
                                                  changed_by=str(getattr(self, "_changed_by", "") or "")[:128])
        except Exception:  # noqa: BLE001
            pass

    def __str__(self) -> str:
        return f"BrokerProfile({self.name}, active={self.is_active})"


class BrokerProfileAudit(models.Model):
    """Append-only history of BrokerProfile config changes (what the profile looked like at each revision). Never
    updated or deleted — the auditable record of broker configuration over time. PROTECT (matching the app's other
    append-only evidence FKs) so a profile carrying history cannot be destroyed: retire a broker via is_active=False,
    never delete it (the admin also disables profile deletion)."""
    broker_profile = models.ForeignKey(BrokerProfile, on_delete=models.PROTECT, related_name="audit")
    action = models.CharField(max_length=16)
    snapshot = models.JSONField(default=dict)
    changed_by = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]

    def delete(self, *args, **kwargs):
        raise ValidationError("BrokerProfileAudit is append-only; it is never deleted")

    def __str__(self) -> str:
        return f"BrokerProfileAudit({self.broker_profile_id}, {self.action}, {self.created_at:%Y-%m-%d})"


# ======================================================================================================
# Multi-account association (MULTI_ACCOUNT_PUBLIC_METRICS §A/§C) — DARK; additive + non-destructive.
# The explicit many-to-many between a GuvFX trading account and a broker-registered notification email, so ONE
# (email, broker) can serve MANY accounts WITHOUT duplicating mailbox credentials. The credential stays on the one
# ConnectedMailbox; the legacy single-FK bindings on BrokerEmailIdentity are left UNTOUCHED (this table is additive).
# No writer is wired under this packet; the association is populated by a later governed flow / DARK backfill.
# ======================================================================================================

class TradingAccountBrokerContact(models.Model):
    """Associates ONE GuvFX trading account with ONE broker-registered email identity (``BrokerEmailIdentity`` = a
    ``(email, broker)`` registry row). Many accounts may attach to the SAME identity (``EMAIL 1 -> MANY ACCOUNTS`` /
    ``BROKER 1 -> MANY ACCOUNTS``), and many bindings may point at one ``ConnectedMailbox`` (``ONE INBOX -> MANY
    ACCOUNTS/BROKERS``), with the OAuth credential held only once on that mailbox — never duplicated per account.

    Verification is PER BINDING: mailbox control alone is NOT proof that a specific account's withdrawals route here
    (that requires explicit broker/account identifiers), so a binding stays PENDING/UNRESOLVED until proven VERIFIED;
    ambiguous attribution is never guessed. A disconnected account/mailbox RETIREs the binding — the row and all
    withdrawal evidence are retained (history is never destroyed).

    FAIL-CLOSED VERIFIED invariant (DB-ENFORCED, per the PD review of #511 — not merely an app convention a bulk/raw
    write could bypass): a ``BEFORE INSERT OR UPDATE`` trigger (migration 0015) refuses ``status='VERIFIED'`` unless
    ALL hold — (1) ``connected_mailbox`` is non-null and ``connected_mailbox.user == trading_account.user`` (mailbox
    owner == account owner), and (2) ``broker_email_identity`` belongs to ``trading_account.user`` (directly or via
    its own account). Ownership is the account's owner, NEVER the shared email address. ``verified_at`` is revocable,
    not a latch; broker-name consistency is checked by the app-layer ``contact_verification`` service (canonical-slug
    matching is a documented deferred TODO — no name→slug resolver exists yet).

    DELETION / PURGE: ``trading_account`` is PROTECT (history-safe). The operator ``User.delete()`` purge CASCADEs to
    ``TradingAccount``; a PROTECTed binding would abort it, so the purge MUST RETIRE/delete this user's bindings
    FIRST (the binding table is deletable — only the AUDIT is append-only), mirroring the existing "delete
    AccountProvisioning first" runbook step."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Associated; ownership/routing not yet verified"
        VERIFIED = "VERIFIED", "Ownership + routing verified for THIS account"
        UNRESOLVED = "UNRESOLVED", "Cannot be attributed to this account from available evidence"
        RETIRED = "RETIRED", "No longer in use (account/mailbox disconnected)"

    # PROTECT (history-safe): a trading account is never hard-deleted out from under its binding + the withdrawal/
    # event history it anchors. Normal offboarding is tombstone-only (``account_removal.remove_account``) and the API
    # ``destroy`` is refused for everyone, so PROTECT only ever guards the operator ``User.delete()`` purge and a
    # direct row delete — and the purge MUST RETIRE/delete bindings first (see the class docstring). Was CASCADE in
    # #513; corrected per the PD review of #511 (deletion-policy parity with ``broker_email_identity``).
    trading_account = models.ForeignKey("trading.TradingAccount", on_delete=models.PROTECT,
                                        related_name="broker_contacts")
    # The (email, broker) registry row. PROTECT: an identity that still has account bindings is never deleted.
    broker_email_identity = models.ForeignKey(BrokerEmailIdentity, on_delete=models.PROTECT,
                                              related_name="account_contacts")
    # The inbox that receives this account's broker mail, if known/verified. SET_NULL so revoking/deleting a mailbox
    # never destroys the binding (it is RETIRED separately). The credential lives on the mailbox, not here.
    connected_mailbox = models.ForeignKey(ConnectedMailbox, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="account_contacts")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One binding per (account, email@broker). The SAME identity may still bind to MANY other accounts, and an
            # account may bind to MANY identities — only the exact (account, identity) pair is unique.
            models.UniqueConstraint(fields=["trading_account", "broker_email_identity"],
                                    name="uniq_tabc_account_identity"),
        ]
        indexes = [
            models.Index(fields=["trading_account", "status"]),
            models.Index(fields=["broker_email_identity"]),
            models.Index(fields=["connected_mailbox"]),
        ]

    def __str__(self) -> str:
        return f"TradingAccountBrokerContact(acct={self.trading_account_id}, idn={self.broker_email_identity_id}, {self.status})"


class TradingAccountBrokerContactAudit(models.Model):
    """Append-only history of binding actions (BIND / VERIFY / RETIRE / REJECT) — who acted, for which claimed
    account/identity/mailbox, the proofs relied on, the outcome, and the before/after status. Append-only is now
    DB-ENFORCED (migration 0015: a BEFORE UPDATE trigger that rejects any change to the evidential columns, and a
    BEFORE DELETE trigger) as well as by the app ``delete()`` guard below — so a bulk/raw write cannot rewrite
    history (per the PD review of #511). The UPDATE trigger is SELECTIVE: it deliberately ALLOWS the four SET_NULL FK
    cascade updates (``subject_user``/``trading_account``/``broker_email_identity``/``connected_mailbox``), so
    deleting a referenced user/account/identity/mailbox still succeeds and simply nulls the reference. ALL five
    back-reference FKs (incl. ``contact``) are SET_NULL so this history SURVIVES a purge as orphaned rows rather than
    blocking it; the trigger permits a FK to decay to NULL but never to be re-pointed to a different value. Holds NO
    credential and NO raw email body — only ids, statuses, a masked address, and a structured proof summary."""

    class Action(models.TextChoices):
        BIND = "BIND", "Associated an account with a broker email identity"
        VERIFY = "VERIFY", "Verified ownership + routing for the account"
        RETIRE = "RETIRE", "Binding retired (account/mailbox disconnected)"
        REJECT = "REJECT", "Binding refused (proof failed / ambiguous / cross-user)"

    # SET_NULL (not PROTECT): the audit is append-only HISTORY that must SURVIVE a binding's deletion (e.g. the
    # operator purge deletes a user's bindings before User.delete()). Deleting a contact nulls this back-reference and
    # the audit row persists as orphaned history — the recorded FACTS stay immutable (the migration-0015 trigger lets
    # a back-reference FK decay to NULL on cascade but never be re-pointed or un-nulled). Was PROTECT in #513.
    contact = models.ForeignKey(TradingAccountBrokerContact, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="audit")
    action = models.CharField(max_length=16, choices=Action.choices)
    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    trading_account = models.ForeignKey("trading.TradingAccount", on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="+")
    broker_email_identity = models.ForeignKey(BrokerEmailIdentity, on_delete=models.SET_NULL, null=True, blank=True,
                                              related_name="+")
    connected_mailbox = models.ForeignKey(ConnectedMailbox, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="+")
    outcome = models.CharField(max_length=24, blank=True, default="")     # OK, or a reason code on reject
    reason = models.CharField(max_length=255, blank=True, default="")     # human-readable, sanitised
    status_before = models.CharField(max_length=12, blank=True, default="")
    status_after = models.CharField(max_length=12, blank=True, default="")
    proofs = models.JSONField(default=dict)                               # structured proof summary; no secrets/body
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["contact", "id"]), models.Index(fields=["subject_user"])]

    def delete(self, *args, **kwargs):
        raise ValidationError("TradingAccountBrokerContactAudit is append-only; it is never deleted")

    def __str__(self) -> str:
        return f"TradingAccountBrokerContactAudit({self.action}, contact={self.contact_id}, {self.outcome})"


class BrokerMessageObservation(models.Model):
    """DARK per-inbox DELIVERY record (MULTI_ACCOUNT_PUBLIC_METRICS §D, corrected per the PD review of #511).

    One row = one authenticated DELIVERY of a broker message into one connected inbox. Its identity is the TRUSTED,
    mailbox-native ``(connected_mailbox, provider_message_id)`` — the provider's own stable id for a message in that
    inbox (Gmail message id / IMAP UIDVALIDITY+UID). A genuine RE-FETCH of the SAME message from the SAME inbox is the
    same delivery and must NOT create a second ``BrokerEvent``; the ``UNIQUE(connected_mailbox, provider_message_id)``
    below enforces that.

    The RFC822 ``Message-ID`` is recorded here as NON-AUTHORITATIVE metadata ONLY — it is sender-controlled and may be
    absent, reused, or forged, so it is NEVER a suppression/trust key and raw eligible evidence is NEVER dropped on it.
    It is used solely as a cross-inbox CORRELATION HINT at the withdrawal layer (see ``correlation.py``): when the SAME
    logical email arrives through TWO different inboxes (two connected_mailboxes, two preserved owner-scoped events —
    neither discarded, data.md), the ref-less withdrawal projection may MERGE the two into ONE Withdrawal scoped to the
    same ``(owner, trading_account)`` so a duplicate delivery does not inflate statistics. A forged/reused Message-ID
    can at worst conservatively MERGE two of one owner's own events (an under-count, never inflation, never
    cross-owner).

    SEPARATE from ``BrokerEvent`` on purpose: existing ``BrokerEvent`` writes stay byte-identical, so an unmigrated
    production DB is unaffected while the feature is OFF — this table is only ever read/written when
    ``broker_cross_inbox_dedup_enabled()``. Records NO credential and NO email body; the raw copies remain in the
    immutable evidence store, and each per-inbox copy's evidence is retained (never destroyed)."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="broker_message_observations")
    # The inbox this delivery arrived through + the provider's own stable id for the message in that inbox: the
    # TRUSTED delivery identity. (``provider`` is derived from ``connected_mailbox.provider`` at write time and stored
    # for clarity.)
    connected_mailbox = models.ForeignKey(ConnectedMailbox, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="message_observations")
    provider = models.CharField(max_length=16, blank=True, default="")
    provider_message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # RFC822 Message-ID — NON-authoritative metadata / cross-inbox correlation hint ONLY; never a suppression key.
    rfc822_message_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # The event/evidence this delivery produced.
    event = models.ForeignKey(BrokerEvent, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="message_observations")
    evidence = models.ForeignKey(EvidenceBlob, on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="message_observations")
    duplicate_count = models.PositiveIntegerField(default=0)   # how many same-inbox RE-FETCHES of this delivery were suppressed
    first_seen_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One delivery per (inbox, provider-native message id). A blank provider_message_id or a null mailbox is
            # NOT a delivery identity (no suppression) — such a fetch falls back to the owner-scoped content-hash
            # idempotency on BrokerEvent. Two DIFFERENT inboxes are two DIFFERENT connected_mailboxes -> two preserved
            # deliveries (collapsed only at the withdrawal layer), never suppressed here.
            models.UniqueConstraint(fields=["connected_mailbox", "provider_message_id"],
                                    condition=models.Q(provider_message_id__gt="", connected_mailbox__isnull=False),
                                    name="uniq_bmo_delivery"),
        ]
        indexes = [
            models.Index(fields=["connected_mailbox", "provider_message_id"]),
            # Supports the withdrawal-layer cross-inbox correlation hint lookup (siblings of one logical message).
            models.Index(fields=["owner", "rfc822_message_id"]),
        ]

    def __str__(self) -> str:
        return (f"BrokerMessageObservation(owner={self.owner_id}, mbx={self.connected_mailbox_id}, "
                f"pmid={self.provider_message_id[:24]}…, refetches={self.duplicate_count})")


class ErasureHold(models.Model):
    """A legal/security HOLD that blocks controlled erasure for a data subject (ADR-0054). While an ``active`` hold
    exists for the subject, ``execute_owner_erasure`` refuses (status HELD) so data required for fraud/AML/litigation
    or security evidence (e.g. a refused cross-user attribution) is never erased. Owner-level in this increment
    (record-level holds are a documented future refinement). This table is NOT evidence and carries no message
    content — only the subject reference, a reason category and who placed/released it."""

    # SET_NULL + a durable id snapshot: the hold survives even if the User row is later removed, and the snapshot
    # keeps the subject resolvable for audit. No message content is ever stored here.
    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="erasure_holds")
    subject_user_id_snapshot = models.PositiveIntegerField(db_index=True)
    reason = models.CharField(max_length=200)                      # a category/justification, never message content
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["subject_user_id_snapshot", "active"])]

    def __str__(self) -> str:
        return f"ErasureHold(subject={self.subject_user_id_snapshot}, active={self.active})"


class ErasureRequest(models.Model):
    """A governed, owner-scoped erasure request (ADR-0054, GDPR/UK-GDPR Art 17). It is the explicit record that must
    exist before any erasure runs, carries the dry-run PLAN and the second-confirmation token, and records the OUTCOME
    — all as counts/ids only, NEVER deleted message content. It holds NO ForeignKey to the objects being erased (those
    ids live in the JSON plan/outcome), so the request survives the erasure it records. Not evidence; append-only in
    spirit but not trigger-protected (it is operational metadata, not broker evidence)."""

    class Status(models.TextChoices):
        REQUESTED = "REQUESTED", "Requested (identity not yet verified)"
        PLANNED = "PLANNED", "Dry-run plan produced; awaiting independent approval"
        APPROVED = "APPROVED", "Independently approved (two-person); ready to execute"
        EXECUTED = "EXECUTED", "Erasure executed (DB rows removed)"
        COMPLETED = "COMPLETED", "Fully erased (all file-deletion outbox tasks drained)"
        FAILED = "FAILED", "Erasure failed (rolled back)"
        HELD = "HELD", "Blocked by an active legal/security hold"
        CANCELLED = "CANCELLED", "Cancelled"

    subject_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="erasure_requests")
    subject_user_id_snapshot = models.PositiveIntegerField(db_index=True)   # durable owner id (survives SET_NULL)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")                      # the staff/governance operator
    reason = models.CharField(max_length=200, blank=True, default="")       # category/justification, never content
    identity_verified = models.BooleanField(default=False)                  # verified data-subject identity gate
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.REQUESTED, db_index=True)
    plan = models.JSONField(null=True, blank=True)                          # dry-run plan: counts + classified ids
    confirm_token = models.CharField(max_length=64, blank=True, default="") # second-confirmation token (plan digest)
    outcome = models.JSONField(null=True, blank=True)                       # execution result: counts only, no content
    error = models.CharField(max_length=300, blank=True, default="")        # sanitised failure reason (no content)
    created_at = models.DateTimeField(auto_now_add=True)
    planned_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)

    # --- Two-person separation-of-duties lifecycle (ADR-0054 Finding 7) -------------------------------------
    # Four roles are RECORDED with timestamps. The ENFORCED two-person rule is: the APPROVER must differ from the
    # ``requested_by`` requester, and a request with no recorded requester cannot be approved or executed (both
    # checked in the control plane). ``requested_by`` (above) is the operator who raised the request. The VERIFIER
    # attests the data-subject's identity was checked out-of-band (method category only, never ID documents) and
    # can only be set before approval. The EXECUTOR is recorded by the provisioned backend at execution (execute
    # is fail-closed in this build, so it is unset here). Approval carries an expiry and can be revoked; a
    # revoked/expired approval fails closed. These are operational metadata — no message content.
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_method = models.CharField(max_length=60, blank=True, default="")   # category, e.g. "signed-request"
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_expires_at = models.DateTimeField(null=True, blank=True)
    approval_revoked = models.BooleanField(default=False)
    executed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    # The operator AUTHORISED to execute this request, designated at approval (must differ from the approver).
    # The governed procedure will only execute when the DB-authenticated session maps (via ErasureExecutionGrant)
    # to exactly this operator — the executor identity is NOT a caller argument (ADR-0054, P0 non-forgeability).
    assigned_executor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name="+")

    class Meta:
        indexes = [models.Index(fields=["subject_user_id_snapshot", "status"])]
        # Finding A — DB-level row-consistency (defense in depth; the governed procedure re-validates the full
        # record at execution). NB: the staff FKs are SET_NULL by design (they survive a staff-user deletion),
        # so these CHECKs deliberately do NOT require the FK ids to be non-null — that completeness is enforced
        # by the procedure at execution (and fails closed if a staff id was later nulled).
        constraints = [
            # A verified request must carry a non-empty method category.
            models.CheckConstraint(
                name="erasurereq_verified_requires_method",
                check=models.Q(identity_verified=False) | ~models.Q(verification_method=""),
            ),
            # A request cannot reach an approved/executed/completed state without verified identity.
            models.CheckConstraint(
                name="erasurereq_advanced_requires_verified",
                check=(~models.Q(status__in=["APPROVED", "EXECUTED", "COMPLETED"])
                       | models.Q(identity_verified=True)),
            ),
        ]

    def __str__(self) -> str:
        return f"ErasureRequest(subject={self.subject_user_id_snapshot}, {self.status})"


class ErasureExecutionGrant(models.Model):
    """A non-forgeable, SINGLE-USE authorisation binding an EXACT PostgreSQL login (``db_session_user``) to an
    operator for ONE ``ErasureRequest`` (ADR-0054, P0 — executor identity must not be caller-asserted).

    Created ONLY by the independently-approved grant process (a privileged role — NEVER the application role;
    provisioning moves this table off the app role so the runtime cannot forge a grant). The governed SECURITY
    DEFINER procedure reads ``session_user`` (the login that connected — unaffected by SECURITY DEFINER, unlike
    ``current_user``), resolves the operator from THIS mapping, requires it to equal the request's
    ``assigned_executor`` and to differ from the approver, then CONSUMES the grant (sets ``consumed_at``).
    A revoked, expired, unmapped, wrong-request, or already-consumed grant fails closed."""

    request = models.ForeignKey(ErasureRequest, on_delete=models.CASCADE, related_name="execution_grants")
    db_session_user = models.CharField(max_length=63)   # exact PostgreSQL login role name (NAMEDATALEN-1)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["request", "db_session_user"],
                                               name="uniq_execgrant_request_session")]
        indexes = [models.Index(fields=["db_session_user", "request"])]

    def __str__(self) -> str:
        return f"ErasureExecutionGrant(req={self.request_id}, session={self.db_session_user}, consumed={bool(self.consumed_at)})"


class DeletionTask(models.Model):
    """Durable FILE-deletion OUTBOX (ADR-0054 Findings 3/4). Irreversible filesystem deletion (encrypted
    credential files, content-addressed evidence files) is NEVER done inside the governed DB transaction. The
    governed procedure deletes the DB rows and, in the SAME transaction, writes PENDING tasks here; an idempotent
    worker removes the files AFTER commit and marks each task COMPLETED/FAILED. A crash after commit therefore
    leaves a durable PENDING task (not an orphan file); the ``ErasureRequest`` is only COMPLETED once no
    PENDING/FAILED tasks remain. Carries only a target reference (a sha256/credential-ref pointer), never content."""

    class Kind(models.TextChoices):
        CREDENTIAL_FILE = "CREDENTIAL_FILE", "Encrypted OAuth credential file"
        EVIDENCE_FILE = "EVIDENCE_FILE", "Content-addressed evidence blob file"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending (file not yet removed)"
        COMPLETED = "COMPLETED", "Completed (file removed or already absent)"
        FAILED = "FAILED", "Failed (retryable)"
        DEAD = "DEAD", "Dead-lettered (retry cap reached; needs operator intervention)"

    request = models.ForeignKey(ErasureRequest, on_delete=models.PROTECT, related_name="deletion_tasks")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    target_ref = models.CharField(max_length=128)             # sha256 (evidence) or credential_ref — a pointer, no content
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True, default="")   # sanitised, no content
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "kind"])]
        # A given (request, kind, target_ref) is written once — idempotent re-plans do not duplicate work.
        constraints = [models.UniqueConstraint(fields=["request", "kind", "target_ref"],
                                               name="uniq_deletiontask_request_kind_target")]

    def __str__(self) -> str:
        return f"DeletionTask(req={self.request_id}, {self.kind}, {self.status})"
