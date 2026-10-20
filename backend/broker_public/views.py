"""broker_public — PUBLIC (unauthenticated) read-only broker directory + metrics API (MULTI_ACCOUNT §E/§G).

Structurally separate from every authenticated/member surface: these views explicitly set
``authentication_classes = []`` + ``permission_classes = [AllowAny]`` (the only safe way to bypass the project's
IsAuthenticated default, per the TelegramWebhookView precedent) and are DARK-gated — they 404 until
``broker_public_directory_enabled()``. They return ONLY published editorial + aggregate-only statistics; NEVER a
member identity, email, account number, withdrawal reference, individual transaction, raw notification, or evidence
pointer. Read-only; no mutation, no member data, no execution/credential authority.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

# These are unauthenticated endpoints. They carry a DEDICATED anonymous rate limit (GuvFXPublicAnonRateThrottle,
# the "public" scope) — stricter than the global IP ceiling they would otherwise inherit — so the public surface has
# its own conservative throttle in place before it is ever armed. The surface still ships DARK (404 until
# BROKER_PUBLIC_DIRECTORY_ENABLED).
from core.throttling import GuvFXPublicAnonRateThrottle

from .case_study import case_study_payload
from .flags import (broker_public_case_study_enabled, broker_public_directory_enabled,
                    broker_public_wtm_preview_enabled)
from .models import BrokerMetricAggregate, PublicBroker
from .wtm_preview import wtm_preview_payload


def _aggregate_json(agg: BrokerMetricAggregate) -> dict:
    """Aggregate-only, PII-free projection of one metric aggregate.

    When PUBLISHED it shows sample size, contributor count, methodology version, window, coarsened freshness and the
    computed stats. When below threshold (INSUFFICIENT_DATA) it returns ONLY a truthful generic stub — NO exact
    contributor count, NO exact private observation count, NO observation window, NO transaction-derived timestamp —
    so the 'insufficient' payload itself can never be read to de-anonymise a tiny sample or to difference across
    repeated/rolling-window queries (PD review §6). NEVER an individual observation."""
    out = {
        "category": agg.category_key,
        "state": agg.state,
        "methodology_version": agg.methodology_version,
    }
    if agg.state != BrokerMetricAggregate.State.PUBLISHED:
        return out   # truthful generic insufficient-data state: no counts, no window, no timestamps
    out.update({
        "sample_size": agg.sample_size,
        "contributor_count": agg.contributor_count,
        "observation_window": {"start": agg.window_start.isoformat() if agg.window_start else None,
                               "end": agg.window_end.isoformat() if agg.window_end else None},
        "last_update": agg.computed_at.isoformat() if agg.computed_at else None,
        # data_freshness is already coarsened to the date; emitted only in the PUBLISHED branch.
        "data_freshness": agg.data_freshness_at.isoformat() if agg.data_freshness_at else None,
        "stats": agg.stats,
    })
    return out


def _broker_json(b: PublicBroker, *, include_metrics: bool) -> dict:
    out = {"slug": b.slug, "display_name": b.display_name, "summary": b.summary, "website": b.website}
    if include_metrics:
        out["metrics"] = [_aggregate_json(a) for a in b.aggregates.all().order_by("category_key")]
    return out


class PublicBrokerDirectoryView(APIView):
    """GET /api/public/brokers/ — the published broker directory (no metrics detail)."""
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [GuvFXPublicAnonRateThrottle]

    def get(self, request):
        if not broker_public_directory_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        brokers = PublicBroker.objects.filter(is_published=True).order_by("display_name")
        return Response({"brokers": [_broker_json(b, include_metrics=False) for b in brokers]})


class PublicBrokerProfileView(APIView):
    """GET /api/public/brokers/<slug>/ — one published broker + its aggregate-only metrics (with INSUFFICIENT_DATA
    states shown truthfully)."""
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [GuvFXPublicAnonRateThrottle]

    def get(self, request, slug):
        if not broker_public_directory_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        b = PublicBroker.objects.filter(slug=slug, is_published=True).first()
        if b is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_broker_json(b, include_metrics=True))


class PublicWtmPreviewView(APIView):
    """GET /api/public/wtm-preview/ — the WAYOND Trader Match broker-metrics PREVIEW (investor demo, DARK).

    Returns a clearly-labelled SIMULATED broker-performance scorecard (every figure synthetic, ``simulated: True``)
    illustrating future multi-sample analytics, plus the truthful authentic state (INSUFFICIENT_VERIFIED_DATA). It
    reads NO member/withdrawal/broker data and publishes NO real rating. 404 until ``broker_public_wtm_preview_enabled``
    is armed (isolated demo only)."""
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [GuvFXPublicAnonRateThrottle]

    def get(self, request):
        if not broker_public_wtm_preview_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(wtm_preview_payload())


class PublicCaseStudyView(APIView):
    """GET /api/public/case-study/ — the genuine-evidence PRIVATE case study (investor demo, DARK).

    Returns ONLY the verified, REDACTED facts of the single real pilot withdrawal (amount/rail/timestamps/interval/
    attribution). NEVER a raw email, account number, confirmation link, wallet address, evidence pointer or member
    identity. 404 until ``broker_public_case_study_enabled`` is armed (isolated demo only), so the facts are not even
    shipped in the client bundle by default."""
    authentication_classes: list = []
    permission_classes = [AllowAny]
    throttle_classes = [GuvFXPublicAnonRateThrottle]

    def get(self, request):
        if not broker_public_case_study_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(case_study_payload())
