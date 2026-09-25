"""ADR-0034 Onboarding — the customer-facing onboarding API (DARK).

Three owner-scoped endpoints that let an ENTITLED customer drive their own Hosted Workspace journey:

    GET  /api/hosted-workspace/onboarding/journey/   → the customer's journey projection + next action
    POST /api/hosted-workspace/onboarding/request/   → request a workspace (idempotent; broker IDENTIFIERS only)
    POST /api/hosted-workspace/onboarding/bind/       → declare the expected broker identity (deferred bind; write-once)
    POST /api/hosted-workspace/onboarding/confirm/    → confirm the discovered broker account is theirs

Every route is 404-invisible while the subsystem is DARK (either the master flag or the onboarding flag OFF)
— the 404 is decided BEFORE any DB read. Reads/writes are strictly owner-scoped to ``request.user`` (IDOR
-safe: a customer only ever sees/acts on their OWN single workspace). POST is CSRF-enforced automatically by
``CookieJWTAuthentication`` for cookie auth. NO endpoint accepts a broker password, and no response carries a
credential, a full login (masked), an attach path, or a stack trace. Nothing here arms execution or places an
order — the journey stops at assignment-eligibility.
"""
from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status as http
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hosted_workspace import provisioning as P
from hosted_workspace.eligibility import strategy_assignment_eligibility
from hosted_workspace.entitlement import (
    DENY_NOT_ENTITLED,
    DENY_NO_USER,
    DENY_ONBOARDING_DARK,
    DENY_SUBSYSTEM_DARK,
)
from hosted_workspace.flags import hosted_persistent_mt5_enabled, hosted_workspace_onboarding_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.onboarding_ops import onboarding_fleet_projection
from hosted_workspace.onboarding_read_model import onboarding_journey_projection

# Bound on the operator fleet page to keep the response finite (DARK read-only diagnostics).
_OPS_ROW_LIMIT = 200

# Defence-in-depth: any request body key containing one of these secret tokens is rejected outright. The
# real (non-)storage guarantee is the orchestrator having NO password parameter — this just turns a
# mis-built client's secret-bearing body into a clear 400 instead of a silent drop. Substring match on a
# curated set that never appears in a legitimate onboarding field (expected_login/expected_server/broker_name).
_FORBIDDEN_BODY_TOKENS = ("password", "passwd", "pwd", "secret", "token", "credential", "api_key",
                          "apikey", "keyring")


def _body_has_secret(obj) -> bool:
    """True iff any (possibly nested) mapping key contains a forbidden secret token. Recurses through dicts
    and lists so a nested `{"broker": {"password": ...}}` is caught, not just top-level keys."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            low = str(key).lower()
            if any(tok in low for tok in _FORBIDDEN_BODY_TOKENS) or _body_has_secret(value):
                return True
    elif isinstance(obj, (list, tuple)):
        return any(_body_has_secret(item) for item in obj)
    return False

_NOT_FOUND = Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

# Admission reason → HTTP status. The DARK reasons are handled by the visibility gate (404) before this maps;
# an entitlement/user denial while the subsystem is visible is an honest 403.
_ADMISSION_HTTP = {
    DENY_SUBSYSTEM_DARK: http.HTTP_404_NOT_FOUND,
    DENY_ONBOARDING_DARK: http.HTTP_404_NOT_FOUND,
    DENY_NOT_ENTITLED: http.HTTP_403_FORBIDDEN,
    DENY_NO_USER: http.HTTP_403_FORBIDDEN,
}


def _subsystem_visible() -> bool:
    """The onboarding API exists only when BOTH the master subsystem flag and the onboarding flag are ON.
    Either OFF ⇒ the endpoints 404 (invisible), decided before any DB read."""
    return bool(hosted_persistent_mt5_enabled() and hosted_workspace_onboarding_enabled())


def _own_workspace(user, *, workspace_uuid=None, account_id=None):
    """Owner-scoped resolve via the immutable ``trading_account.user`` binding (the single ownership source).

    C3 (Concurrent Broker Accounts) — ACCOUNT-EXPLICIT. When a selector (``workspace_uuid`` or
    ``account_id``) is supplied, resolve exactly that ONE owned workspace; because the queryset is owner-
    scoped, a cross-user id simply yields ``None`` → 404 (IDOR-safe). With NO selector it falls back to the
    single workspace ONLY while unambiguous (the user owns ≤ 1). Once the user owns > 1, an unqualified
    resolve is AMBIGUOUS and returns ``None`` so the caller MUST identify the intended account — no silent
    arbitrary ``.first()`` pick. DARK-identical: with one workspace per user (today) this returns that one."""
    qs = (HostedMt5Workspace.objects.filter(trading_account__user=user)
          .select_related("trading_account__broker_server"))
    if workspace_uuid:
        try:
            return qs.filter(workspace_uuid=workspace_uuid).first()
        except (ValueError, DjangoValidationError):
            return None      # a malformed uuid matches nothing -> 404 (never an unhandled 500)
    if account_id is not None:
        return qs.filter(trading_account_id=account_id).first()
    if qs.count() > 1:
        return None                       # ambiguous — fail closed; caller must specify the account
    return qs.first()


def _selector(request):
    """Optional explicit account selector from the request (query for GET, body for POST): ``workspace_uuid``
    or ``account_id`` (TradingAccount.id — the internal canonical selector). Lets a multi-account customer
    identify the intended workspace; absent while a user has a single workspace (backward-compatible)."""
    src = request.query_params if request.method == "GET" else (
        request.data if isinstance(request.data, dict) else {})
    wu = str(src.get("workspace_uuid", "") or "").strip() or None
    aid = src.get("account_id")
    try:
        aid = int(aid) if aid not in (None, "") else None
    except (TypeError, ValueError):
        aid = None
    return {"workspace_uuid": wu, "account_id": aid}


def _resolve_or_error(request):
    """Account-explicit resolve for a WRITE/ARM op → ``(workspace, error_response)``. 404 when the user has no
    workspace OR when a supplied selector matches none of THEIR accounts (IDOR-safe); 400 asking for a selector
    when the user owns > 1 and gave none (never acts on an arbitrary account)."""
    sel = _selector(request)
    ws = _own_workspace(request.user, **sel)
    if ws is not None:
        return ws, None
    total = HostedMt5Workspace.objects.filter(trading_account__user=request.user).count()
    if total == 0 or sel["workspace_uuid"] or sel["account_id"] is not None:
        return None, _NOT_FOUND      # no workspace, or a selector that isn't the caller's → 404 (owner-scoped)
    return None, Response({"detail": "Multiple broker accounts — specify account_id or workspace_uuid.",
                           "reason": "account_selector_required"}, status=http.HTTP_400_BAD_REQUEST)


def _account_summary(ws):
    """Secret-free per-account row for the multi-account chooser (C3): the internal selector (account_id /
    workspace_uuid) + broker/server/demo labels only — NO login/account_number, NO credential."""
    acct = ws.trading_account
    srv = getattr(acct, "broker_server", None)
    return {"account_id": acct.id, "workspace_uuid": str(ws.workspace_uuid),
            "broker_name": acct.broker_name or "",
            "broker_server": (getattr(srv, "server_name", "") if srv else ""),
            "is_demo": bool(acct.is_demo)}


def _projection(request_user, ws, account):
    """The full customer-facing onboarding payload: the journey projection plus the tiered strategy-assignment
    eligibility (assignment < armed < order-authorised). ``user`` is passed explicitly so ENTITLED is
    meaningful even before a workspace exists."""
    staff = bool(getattr(request_user, "is_staff", False))
    body = onboarding_journey_projection(ws, account, staff=staff)
    body["assignment"] = strategy_assignment_eligibility(account, user=request_user)
    return body


class _OnboardingBase(APIView):
    permission_classes = [IsAuthenticated]

    def _dark(self):
        # 404 BEFORE any DB read while the subsystem is dark.
        return None if _subsystem_visible() else _NOT_FOUND


class OnboardingJourneyView(_OnboardingBase):
    """GET the caller's own onboarding journey. No workspace yet ⇒ the NO_WORKSPACE phase (request_workspace),
    never a leak of anyone else's state."""

    def get(self, request):
        dark = self._dark()
        if dark is not None:
            return dark
        sel = _selector(request)
        ws = _own_workspace(request.user, **sel)
        if ws is None and not sel["workspace_uuid"] and sel["account_id"] is None:
            # No selector: either 0 workspaces (NO_WORKSPACE projection, unchanged) or >1 (ambiguous) → return
            # the account chooser so a multi-account customer can pick which journey to view (C3).
            owned = list(HostedMt5Workspace.objects.filter(trading_account__user=request.user)
                         .select_related("trading_account__broker_server").order_by("id"))
            if len(owned) > 1:
                return Response({"status": "multiple_accounts",
                                 "accounts": [_account_summary(w) for w in owned]})
        account = ws.trading_account if ws is not None else None
        return Response(_projection(request.user, ws, account))


class OnboardingRequestView(_OnboardingBase):
    """POST to request a Hosted Workspace. Body: ``expected_login`` (required), ``expected_server`` /
    ``broker_name`` (optional identifiers). Idempotent. Rejects any password-bearing field explicitly."""

    def post(self, request):
        dark = self._dark()
        if dark is not None:
            return dark
        data = request.data if isinstance(request.data, dict) else {}
        # Defence in depth — the orchestrator has no password parameter, but reject a secret-bearing body
        # outright (recursively, broad token set) so a mis-built client can never even appear to submit one.
        if _body_has_secret(data):
            return Response({"detail": "A broker password must never be submitted.",
                             "reason": P.REQ_PASSWORD_FORBIDDEN}, status=http.HTTP_400_BAD_REQUEST)
        res = P.request_hosted_workspace(
            request.user,
            expected_login=str(data.get("expected_login", "") or ""),
            expected_server=str(data.get("expected_server", "") or ""),
            broker_name=str(data.get("broker_name", "") or ""),
            is_demo=True, request=request)
        if not res.ok:
            if res.reason in (P.REQ_LOGIN_REQUIRED, P.REQ_IDENTITY_INVALID):
                return Response({"detail": "The broker account number / server is missing or invalid.",
                                 "reason": res.reason}, status=http.HTTP_400_BAD_REQUEST)
            status = _ADMISSION_HTTP.get(res.reason, http.HTTP_403_FORBIDDEN)
            if status == http.HTTP_404_NOT_FOUND:
                return _NOT_FOUND
            return Response({"detail": "Not permitted.", "reason": res.reason}, status=status)
        account = res.workspace.trading_account
        body = {"status": ("created" if res.created else "exists"),
                **_projection(request.user, res.workspace, account)}
        return Response(body, status=(http.HTTP_201_CREATED if res.created else http.HTTP_200_OK))


class OnboardingAddBrokerAccountView(_OnboardingBase):
    """POST — Add ANOTHER broker account (C3, DARK). Delegates to the SAME certified service the first-account
    request uses (``request_hosted_workspace``) — no second provisioning architecture: it creates an intent-only
    ``TradingAccount`` + workspace and the existing per-account scheduler provisions ONLY this account (never
    pre-provisions the entitlement maximum). Owner-scoped (``request.user``) and entitlement-bounded server-side
    (the owned-account cap + the per-user final-slot lock live in the service). Idempotent on the canonical
    ``(expected_login, expected_server)`` pair. Supports different brokers, different servers, and DEMO or LIVE
    (``is_demo`` from the body). Rejects any password-bearing field. Returns the internal selectors
    ``trading_account_id`` + ``workspace_uuid``.

    DARK: while ``CONCURRENT_ACCOUNTS_ENFORCEMENT_ENABLED`` is OFF the service keeps one-workspace-per-user, so a
    second add returns the caller's existing workspace (``status=exists``) — it cannot create a second production
    account until the flag is armed."""

    def post(self, request):
        dark = self._dark()
        if dark is not None:
            return dark
        data = request.data if isinstance(request.data, dict) else {}
        if _body_has_secret(data):
            return Response({"detail": "A broker password must never be submitted.",
                             "reason": P.REQ_PASSWORD_FORBIDDEN}, status=http.HTTP_400_BAD_REQUEST)
        res = P.request_hosted_workspace(
            request.user,
            expected_login=str(data.get("expected_login", "") or ""),
            expected_server=str(data.get("expected_server", "") or ""),
            broker_name=str(data.get("broker_name", "") or ""),
            is_demo=bool(data.get("is_demo", True)),
            request=request)
        if not res.ok:
            if res.reason in (P.REQ_LOGIN_REQUIRED, P.REQ_IDENTITY_INVALID):
                return Response({"detail": "The broker account number / server is missing or invalid.",
                                 "reason": res.reason}, status=http.HTTP_400_BAD_REQUEST)
            if res.reason == P.REQ_LIMIT_REACHED:
                return Response({"detail": "Broker-account limit reached for your plan.",
                                 "reason": res.reason}, status=http.HTTP_409_CONFLICT)
            status = _ADMISSION_HTTP.get(res.reason, http.HTTP_403_FORBIDDEN)
            if status == http.HTTP_404_NOT_FOUND:
                return _NOT_FOUND
            return Response({"detail": "Not permitted.", "reason": res.reason}, status=status)
        acct = res.workspace.trading_account
        body = {"status": ("created" if res.created else "exists"),
                "trading_account_id": acct.id, "workspace_uuid": str(res.workspace.workspace_uuid),
                **_projection(request.user, res.workspace, acct)}
        return Response(body, status=(http.HTTP_201_CREATED if res.created else http.HTTP_200_OK))


class OnboardingConfirmView(_OnboardingBase):
    """POST to confirm the discovered broker account is the customer's. Gated on a POSITIVE observed match on
    a connected workspace; idempotent. No body required (acts on the caller's own workspace)."""

    def post(self, request):
        dark = self._dark()
        if dark is not None:
            return dark
        ws, err = _resolve_or_error(request)
        if err is not None:
            return err
        res = P.confirm_broker_account(request.user, ws, request=request)
        if not res.ok:
            if res.reason in _ADMISSION_HTTP:
                status = _ADMISSION_HTTP[res.reason]
                return _NOT_FOUND if status == http.HTTP_404_NOT_FOUND else Response(
                    {"detail": "Not permitted.", "reason": res.reason}, status=status)
            if res.reason == P.CONFIRM_NOT_OWNER:
                return _NOT_FOUND                                  # owner-scoped resolve makes this unreachable
            # CONFIRM_NO_MATCH — connected/matched not yet observed: a conflict with current state.
            return Response({"detail": "This account cannot be confirmed yet.", "reason": res.reason},
                            status=http.HTTP_409_CONFLICT)
        ws.refresh_from_db()
        body = {"status": res.reason, **_projection(request.user, ws, ws.trading_account)}
        return Response(body, status=http.HTTP_200_OK)


class OnboardingAuthorizeExecutionView(_OnboardingBase):
    """POST — the customer's EXPLICIT "Enable automated trading" authorization (ADR-0047). The ONLY path that
    may arm a hosted workspace. Owner-scoped; requires the account confirmed and the workspace observed
    CONNECTED + matched AND canonically EXECUTION_READY; idempotent. No body required. Accepts no secret,
    places no order — MT5 automation capability is not customer authorization, and the live bridge gate remains
    the sole order authority."""

    def post(self, request):
        dark = self._dark()
        if dark is not None:
            return dark
        ws, err = _resolve_or_error(request)
        if err is not None:
            return err
        res = P.authorize_workspace_execution(request.user, ws, request=request)
        if not res.ok:
            if res.reason in _ADMISSION_HTTP:
                status = _ADMISSION_HTTP[res.reason]
                return _NOT_FOUND if status == http.HTTP_404_NOT_FOUND else Response(
                    {"detail": "Not permitted.", "reason": res.reason}, status=status)
            if res.reason == P.AUTHZ_NOT_OWNER:
                return _NOT_FOUND                                  # owner-scoped resolve makes this unreachable
            # AUTHZ_NOT_CONFIRMED / AUTHZ_NOT_READY — not yet in a state that can be authorized: a conflict.
            return Response({"detail": "Automated trading cannot be enabled yet.", "reason": res.reason},
                            status=http.HTTP_409_CONFLICT)
        ws.refresh_from_db()
        body = {"status": res.reason, "arm_reason": res.arm_reason,
                **_projection(request.user, ws, ws.trading_account)}
        return Response(body, status=http.HTTP_200_OK)


class OnboardingBindView(_OnboardingBase):
    """POST to DECLARE the customer's expected broker identity (login + server) for their already-provisioned
    workspace — the deferred-bind step (Beta UX Correction). Body: ``expected_login`` (required),
    ``expected_server`` (optional). Owner-scoped; WRITE-ONCE (an identical re-declaration is idempotent, a
    different second bind conflicts); rejects any password-bearing field. Sets no password, arms nothing,
    advances no state — it only records the identity every later gate matches the observed login against."""

    def post(self, request):
        dark = self._dark()
        if dark is not None:
            return dark
        data = request.data if isinstance(request.data, dict) else {}
        # Defence in depth — the orchestrator has no password parameter; reject a secret-bearing body outright.
        if _body_has_secret(data):
            return Response({"detail": "A broker password must never be submitted.",
                             "reason": P.REQ_PASSWORD_FORBIDDEN}, status=http.HTTP_400_BAD_REQUEST)
        ws, err = _resolve_or_error(request)                      # account-explicit; no workspace to bind → error
        if err is not None:
            return err
        res = P.bind_broker_identity(
            request.user, ws,
            expected_login=str(data.get("expected_login", "") or ""),
            expected_server=str(data.get("expected_server", "") or ""),
            request=request)
        if not res.ok:
            if res.reason in (P.BIND_LOGIN_REQUIRED, P.BIND_IDENTITY_INVALID):
                return Response({"detail": "The broker account number / server is missing or invalid.",
                                 "reason": res.reason}, status=http.HTTP_400_BAD_REQUEST)
            if res.reason == P.BIND_NOT_OWNER:
                return _NOT_FOUND                                  # owner-scoped resolve makes this unreachable
            # BIND_ALREADY / BIND_NOT_HOSTED / BIND_LIVE_FORBIDDEN / BIND_WRONG_STATE — conflict with current state.
            return Response({"detail": "This broker identity cannot be bound.", "reason": res.reason},
                            status=http.HTTP_409_CONFLICT)
        ws.refresh_from_db()
        body = {"status": res.reason, **_projection(request.user, ws, ws.trading_account)}
        return Response(body, status=http.HTTP_200_OK)


class OnboardingOpsView(_OnboardingBase):
    """GET the onboarding fleet — STAFF ONLY (404-invisible to non-staff and while DARK). Secret-free operator
    rows built from the same staff-lens projections; never exposes more than the customer's own view + context."""

    def get(self, request):
        dark = self._dark()
        if dark is not None:
            return dark
        if not bool(getattr(request.user, "is_staff", False)):
            return _NOT_FOUND                                     # staff-only; invisible to everyone else
        qs = (HostedMt5Workspace.objects.select_related("trading_account__broker_server")
              .order_by("id")[:_OPS_ROW_LIMIT])
        return Response({"rows": onboarding_fleet_projection(qs), "limit": _OPS_ROW_LIMIT})
