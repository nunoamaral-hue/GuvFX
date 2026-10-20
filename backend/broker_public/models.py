"""broker_public — PUBLIC broker directory + versioned, privacy-safe metric aggregates (MULTI_ACCOUNT §E/§F).

A SEPARATE app on purpose: it keeps the PUBLIC editorial + statistics surface structurally apart from the INTERNAL
broker configuration (broker_intelligence.BrokerProfile / trading.BrokerServer) and from all owner-scoped member
data. It holds NO credential, NO member identity, NO individual transaction — only canonical broker identity, public
editorial text, and aggregate-only statistics. Everything is DARK (the public endpoints 404 until armed) and the
migration is additive/reversible and NOT applied to production under this packet.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models


class PublicBroker(models.Model):
    """The canonical PUBLIC identity of a broker (one per broker), addressable by a stable slug. Linked to the
    internal ``broker_intelligence.BrokerProfile`` by SLUG only (a soft, decoupled reference — never an FK — so the
    public surface can never pull internal config fields). ``is_published`` is the editorial gate (is this broker's
    page meant to be visible), DISTINCT from the DARK feature flag (whether the public API is served at all)."""

    slug = models.SlugField(max_length=64, unique=True)              # canonical public slug, e.g. "tradersway", "is6"
    display_name = models.CharField(max_length=128)
    source_slug = models.CharField(max_length=64, blank=True, default="")   # soft link to BrokerProfile.slug (internal)
    summary = models.TextField(blank=True, default="")              # public editorial blurb (no PII, no member data)
    website = models.CharField(max_length=255, blank=True, default="")
    is_published = models.BooleanField(default=False)               # editorial visibility (separate from the DARK flag)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]
        indexes = [models.Index(fields=["is_published"]), models.Index(fields=["source_slug"])]

    def __str__(self) -> str:
        return f"PublicBroker({self.slug}, published={self.is_published})"


class BrokerMetricCategory(models.Model):
    """An independently-versioned public metric CATEGORY. Reuses the broker_catalogue.CatalogueVersion discipline:
    exactly ONE ACTIVE version per ``key`` at a time, a documented ``methodology``, and per-category publication
    thresholds. The first category is WITHDRAWAL_PROCESSING; future categories (EXECUTION_LATENCY, SPREAD, SLIPPAGE,
    BROKER_RELIABILITY, ORDER_HANDLING) are added as rows when their methodology is approved — NO schema change, NO
    fabricated measurement."""

    class Key(models.TextChoices):
        WITHDRAWAL_PROCESSING = "WITHDRAWAL_PROCESSING", "Withdrawal processing"
        # Reference list only (future, methodology-gated): EXECUTION_LATENCY, SPREAD, SLIPPAGE, BROKER_RELIABILITY,
        # ORDER_HANDLING. `key` is an open CharField so a new approved category needs no migration.

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"          # methodology being drafted; not served
        ACTIVE = "ACTIVE", "Active"       # the one live methodology version for this key
        RETIRED = "RETIRED", "Retired"    # superseded; kept for audit/rollback

    key = models.CharField(max_length=32)                           # open vocab; Key is the reference list
    methodology_version = models.PositiveIntegerField(default=1)
    methodology = models.TextField(blank=True, default="")          # the documented, approved methodology
    # Documented publication minimums — below EITHER, an aggregate is INSUFFICIENT_DATA (tiny-sample privacy guard).
    min_contributors = models.PositiveIntegerField(default=5)       # distinct members contributing
    min_samples = models.PositiveIntegerField(default=20)           # observations after de-dup + per-contributor cap
    # A DISTRIBUTION statistic (e.g. processing-time median/quantiles) needs its own floor of completed observations,
    # independent of the overall sample, so a single completed withdrawal can never BE the published median.
    min_completed_samples = models.PositiveIntegerField(default=5)
    # Max share of the sample any single contributor may provide (winsorisation), so one user/duplicate can't
    # dominate a public statistic. Expressed as a percentage 1..100.
    max_contributor_share_pct = models.PositiveSmallIntegerField(default=40)
    # k-anonymity small-bucket floor: any single lifecycle/currency bucket smaller than this is collapsed into a
    # "suppressed" tally rather than published as an exact count, so a rare outcome (e.g. one member's single REJECTED
    # or a lone currency) can never pinpoint that member. A PROPOSED default — the reviewed value is governance (PD).
    min_bucket_size = models.PositiveSmallIntegerField(default=5)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.DRAFT, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # At most one ACTIVE methodology version per category key (the atomic active pointer, DB-enforced).
            models.UniqueConstraint(fields=["key"], condition=models.Q(status="ACTIVE"),
                                    name="uniq_bmc_one_active_per_key"),
        ]
        indexes = [models.Index(fields=["key", "status"])]

    def __str__(self) -> str:
        return f"BrokerMetricCategory({self.key} v{self.methodology_version}, {self.status})"


class BrokerMetricAggregate(models.Model):
    """A PUBLISHED, privacy-safe aggregate for ONE (public broker, category). Aggregate-only: it NEVER stores or
    exposes a member identity, email, trading-account number, withdrawal reference, individual transaction, raw
    notification, or evidence pointer — only counts, a contributor count, and PII-free computed ``stats``. It is
    ``INSUFFICIENT_DATA`` (no stats) until the category's contributor + sample thresholds are met."""

    class State(models.TextChoices):
        INSUFFICIENT_DATA = "INSUFFICIENT_DATA", "Insufficient data"
        PUBLISHED = "PUBLISHED", "Published"

    public_broker = models.ForeignKey(PublicBroker, on_delete=models.CASCADE, related_name="aggregates")
    category_key = models.CharField(max_length=32)                  # WITHDRAWAL_PROCESSING, ... (matches a category)
    methodology_version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=20, choices=State.choices, default=State.INSUFFICIENT_DATA)
    sample_size = models.PositiveIntegerField(default=0)            # observations the published stats rest on
    contributor_count = models.PositiveIntegerField(default=0)      # distinct members contributing
    window_start = models.DateTimeField(null=True, blank=True)
    window_end = models.DateTimeField(null=True, blank=True)
    stats = models.JSONField(default=dict)                         # PII-free computed statistics (see aggregation.py)
    data_freshness_at = models.DateTimeField(null=True, blank=True)  # newest eligible observation in the window
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["public_broker", "category_key"],
                                    name="uniq_bma_broker_category"),   # the current aggregate per (broker, category)
        ]
        indexes = [models.Index(fields=["public_broker", "category_key"])]

    def __str__(self) -> str:
        return f"BrokerMetricAggregate({self.public_broker_id}/{self.category_key}, {self.state}, n={self.sample_size})"


class MetricParticipationConsent(models.Model):
    """Explicit, revocable OPT-IN by a GuvFX member to let THEIR genuine withdrawal evidence contribute to a PUBLIC
    broker metric. This is a DISTINCT concern from Gmail inbox-READ consent (which authorises ingestion, not public
    use): a member may allow GuvFX to read their broker mail yet NOT allow that evidence to feed a public statistic.

    Fail-closed: aggregation includes a member's evidence only while an ACTIVE row exists for the scope. Supersede,
    never overwrite (data.md): a revocation is a state transition to REVOKED (``revoked_at`` set) — the ACTIVE row's
    history is retained, never edited away. DB-enforced at most one ACTIVE row per ``(owner, scope)`` (mirrors the
    one-ACTIVE-per-key pointer on BrokerMetricCategory). Holds NO credential, NO transaction, NO identity beyond the
    owner FK + an opaque scope string. DARK: consulted only when ``broker_public_consent_required()``.
    """

    class State(models.TextChoices):
        ACTIVE = "ACTIVE", "Active (participating)"
        REVOKED = "REVOKED", "Revoked (withdrawn)"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="metric_participation_consents")
    # Opaque participation scope — the authoritative internal broker key (source_slug), or "*" for all brokers. NOT a
    # display name. Kept as a string so scope granularity can evolve without a schema change.
    scope = models.CharField(max_length=64)
    state = models.CharField(max_length=8, choices=State.choices, default=State.ACTIVE, db_index=True)
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # At most one ACTIVE participation row per (owner, scope) — the atomic participation pointer, DB-enforced.
            models.UniqueConstraint(fields=["owner", "scope"], condition=models.Q(state="ACTIVE"),
                                    name="uniq_mpc_one_active_per_owner_scope"),
        ]
        indexes = [models.Index(fields=["owner", "scope", "state"])]

    def __str__(self) -> str:
        return f"MetricParticipationConsent(owner={self.owner_id}, scope={self.scope}, {self.state})"
