"""trade_copier.urls — read-only Copy Trading API (PR C). Mounted under /api/copy-trading/."""
from django.urls import path

from trade_copier.consumer_api import (
    PartialCloseAuthorizeSendView,
    PartialCloseClaimView,
    PartialCloseCompleteView,
)
from trade_copier.attestation_api import AttestationVerifyView
from trade_copier.config_api import CopyTradingConfigView
from trade_copier.event_ingestion_api import SignedEventIngestView
from trade_copier.matrix_api import InstrumentMatrixView
from trade_copier.operator_api import OperatorLifecycleView
from trade_copier.views import (
    CopyTradingMastersView,
    CopyTradingOverviewView,
    CopyTradingStatusView,
)

urlpatterns = [
    path("status/", CopyTradingStatusView.as_view(), name="copy-trading-status"),
    path("masters/", CopyTradingMastersView.as_view(), name="copy-trading-masters"),
    # Founder-only SHADOW configuration (owner-scoped, audited, idempotent; cannot enable execution).
    path("config/", CopyTradingConfigView.as_view(), name="copy-trading-config"),
    # Founder-only, owner-scoped, read-only multi-broker instrument-mapping matrix (no write, execution OFF).
    path("instrument-matrix/", InstrumentMatrixView.as_view(), name="copy-trading-instrument-matrix"),
    path("accounts/<int:account_id>/overview/", CopyTradingOverviewView.as_view(),
         name="copy-trading-overview"),
    # ADR-0074 — SIGNED, account-scoped fast-path event ingestion (bridge → backend). DARK: fails closed with
    # 503 unless COPIER_EVENT_INGESTION_ENABLED is on. Persists immutable observations only; no order, no job.
    path("event-ingest/", SignedEventIngestView.as_view(), name="copy-trading-event-ingest"),
    # ADR-0077 — NON-LIFECYCLE host-emitter identity-attestation verification (emitter → backend). DARK: 503
    # unless COPIER_ATTESTATION_VERIFY_ENABLED is on. Proves identity only; no lifecycle, no order, no job.
    path("attest-verify/", AttestationVerifyView.as_view(), name="copy-trading-attest-verify"),
    # ADR-0056 — dedicated DEMO partial-close consumer (modern worker auth only; DARK, no worker granted
    # the permission outside tests; serves ONLY PARTIAL_CLOSE_TRADE).
    path("partial-close/claim/", PartialCloseClaimView.as_view(), name="copy-trading-pclose-claim"),
    path("partial-close/authorize-send/", PartialCloseAuthorizeSendView.as_view(),
         name="copy-trading-pclose-authorize-send"),
    path("partial-close/complete/", PartialCloseCompleteView.as_view(), name="copy-trading-pclose-complete"),
    # ADR-0083 Phase-3 — STAFF-ONLY, read-only, redacted, paginated operator lifecycle/failure view. DARK:
    # reports enabled:false / no rows unless COPIER_OPERATOR_VIEW_ENABLED is on. No mutation surface (GET only),
    # no order, no job, no arming; non-staff requests are denied (403).
    path("operator/lifecycles/", OperatorLifecycleView.as_view(), name="copy-trading-operator-lifecycles"),
]
