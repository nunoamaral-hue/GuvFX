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

# NOTE: these are unauthenticated endpoints. They ship DARK (404 until armed), so no throttle is attached yet;
# configuring an anonymous/scoped rate limit (DEFAULT_THROTTLE_RATES / a scoped throttle) is a PRE-ACTIVATION
# requirement (tracked in the platform report) that must be in place before the public surface is enabled.

from .flags import broker_public_directory_enabled
from .models import BrokerMetricAggregate, PublicBroker


def _aggregate_json(agg: BrokerMetricAggregate) -> dict:
    """Aggregate-only, PII-free projection of one metric aggregate. Shows sample size, contributor count, methodology
    version, window, freshness, and the computed stats — and an explicit INSUFFICIENT_DATA state when below threshold.
    NEVER an individual observation."""
    return {
        "category": agg.category_key,
        "state": agg.state,
        "methodology_version": agg.methodology_version,
        "sample_size": agg.sample_size,
        "contributor_count": agg.contributor_count,
        "observation_window": {"start": agg.window_start.isoformat() if agg.window_start else None,
                               "end": agg.window_end.isoformat() if agg.window_end else None},
        "last_update": agg.computed_at.isoformat() if agg.computed_at else None,
        "data_freshness": agg.data_freshness_at.isoformat() if agg.data_freshness_at else None,
        # stats only when PUBLISHED; INSUFFICIENT_DATA shows no numbers (tiny-sample privacy guard).
        "stats": agg.stats if agg.state == BrokerMetricAggregate.State.PUBLISHED else {},
    }


def _broker_json(b: PublicBroker, *, include_metrics: bool) -> dict:
    out = {"slug": b.slug, "display_name": b.display_name, "summary": b.summary, "website": b.website}
    if include_metrics:
        out["metrics"] = [_aggregate_json(a) for a in b.aggregates.all().order_by("category_key")]
    return out


class PublicBrokerDirectoryView(APIView):
    """GET /api/public/brokers/ — the published broker directory (no metrics detail)."""
    authentication_classes: list = []
    permission_classes = [AllowAny]

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

    def get(self, request, slug):
        if not broker_public_directory_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        b = PublicBroker.objects.filter(slug=slug, is_published=True).first()
        if b is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_broker_json(b, include_metrics=True))
