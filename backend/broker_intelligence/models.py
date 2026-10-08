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
from django.db import models


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
        "received_at", "broker_reference_id", "amount", "currency", "evidence_id", "evidence_hash", "parser_name",
        "parser_version", "confidence", "provenance",
    )

    class Meta:
        ordering = ["id"]   # chronological, append-only
        constraints = [
            # Idempotency backstop: one message (one EvidenceBlob) -> at most one BrokerEvent. Structural guarantee
            # (not just the app-layer filter-then-create), so a concurrent/redelivered re-ingest cannot persist a
            # duplicate event. Partial (evidence present) — events without a blob are not constrained.
            models.UniqueConstraint(fields=["evidence"], condition=models.Q(evidence__isnull=False),
                                    name="uniq_brokerevent_evidence"),
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

    def __str__(self) -> str:
        return f"BrokerEvent({self.event_type}, acct={self.trading_account_id}, {self.correlation_status})"


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

    trading_account = models.ForeignKey("trading.TradingAccount", on_delete=models.PROTECT,
                                        related_name="withdrawals")
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
            # One withdrawal per (account, reference id) — makes reference-id correlation idempotent. Partial:
            # only when a reference id is present (blank refs are heuristic/unresolved and not uniqueness-bearing).
            models.UniqueConstraint(
                fields=["trading_account", "broker_reference_id"],
                condition=models.Q(broker_reference_id__gt=""),
                name="uniq_withdrawal_account_reference"),
        ]
        indexes = [
            models.Index(fields=["trading_account", "status"]),
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
