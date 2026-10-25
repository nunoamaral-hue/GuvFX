"""broker_intelligence read-only member API.

WP6: the member-facing Withdrawals metrics endpoint. Owner-scoped (the authenticated member's OWN, non-disconnected
accounts only — never staff cross-user aggregation, never another member's data) and DARK-gated
(``broker_withdrawal_ux_enabled`` — 404 while OFF so the member feature stays dark until the Sponsor arms it).
Observation-only: it reads the withdrawal projection; no mutation, no broker contact, no execution/credential/
financial authority.
"""
from __future__ import annotations

import os
import secrets

from django.core import signing
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .flags import (broker_account_contact_enabled, broker_identity_verification_enabled,
                    broker_mailbox_connect_enabled, broker_mailbox_status_enabled, broker_multi_mailbox_enabled,
                    broker_multi_mailbox_enabled_for,
                    broker_withdrawal_demo_include_synthetic, broker_withdrawal_speed_enabled_for,
                    broker_withdrawal_ux_enabled)
from .metrics import withdrawal_history, withdrawal_metrics
from .withdrawal_speed import withdrawal_speed

_STATE_SALT = "broker_intelligence.mailbox_connect.state"
_STATE_MAX_AGE = 600   # seconds the signed CSRF state is valid for the connect->callback round trip

# Mailbox connect OPERATIONS (§6) — the EXPLICIT intent of a connect flow, bound into the signed state so the callback
# enforces exactly what the member asked for and can never silently target a different mailbox. V1 is single-mailbox:
# CONNECT_FIRST/ADD_NEW are refused while the member already has a live (non-revoked) mailbox; a reconnect is always a
# TARGETED update of one named, owned mailbox whose canonical identity is bound into the state.
_OP_CONNECT_FIRST = "CONNECT_FIRST_MAILBOX"
_OP_RECONNECT_EXISTING = "RECONNECT_EXISTING_MAILBOX"
_OP_ADD_NEW = "ADD_NEW_MAILBOX"
_VALID_OPS = frozenset({_OP_CONNECT_FIRST, _OP_RECONNECT_EXISTING, _OP_ADD_NEW})


def _conf(name: str) -> str:
    from django.conf import settings as _s
    v = getattr(_s, name, None)
    if v is None:
        v = os.getenv(name, "")
    return str(v or "")


_DEFAULT_MAX_MAILBOXES_PER_OWNER = 5


def _mailbox_cap() -> int:
    """Max non-revoked Gmail inboxes one member may have connected at once (multi-mailbox mode). Configurable via
    ``BROKER_MAX_MAILBOXES_PER_OWNER``; conservative default. This is a MAILBOX-connection ceiling — deliberately NOT
    the trading-account entitlement (``GUVFX_MAX_OWNED_ACCOUNTS_CEILING``), which governs owned accounts, a different
    concern. Falls back to the default on a missing/invalid value (never 0/unbounded)."""
    raw = _conf("BROKER_MAX_MAILBOXES_PER_OWNER")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_MAILBOXES_PER_OWNER
    return n if n >= 1 else _DEFAULT_MAX_MAILBOXES_PER_OWNER


def _connect_return_url() -> str:
    """The FIXED, server-known destination the browser lands on after the Google callback. It MUST be the GuvFX
    FRONTEND origin (guvfx.com) — NEVER the API origin (api.guvfx.com) that serves this callback: a relative path
    would resolve against the API host and strand the member on the API. Resolution order: an explicit absolute
    ``BROKER_MAILBOX_CONNECT_RETURN_URL``; otherwise ``FRONTEND_BASE_URL`` + ``/profile`` (default
    ``https://guvfx.com/profile``). NEVER derived from the request — so the redirect can never be an open redirect."""
    explicit = _conf("BROKER_MAILBOX_CONNECT_RETURN_URL")
    if explicit:
        return explicit
    base = _conf("FRONTEND_BASE_URL") or "https://guvfx.com"
    return base.rstrip("/") + "/profile"


# Non-sensitive outcome markers the callback may hand the frontend (never a token/code/state). The frontend maps the
# few it recognises to a customer-safe banner and treats everything else as a generic error; the owner-scoped status
# API remains the AUTHORITATIVE source — the marker is only a one-time hint.
_OUTCOME_CONNECTED = "connected"


def _callback_result(rt: bool, *, ok: bool, code: str, http_status, json_body):
    """Render a callback outcome (§7). For the BROWSER return-journey (``rt`` — a flag set only inside the member's own
    signed connect state), a 302 to the FIXED GuvFX FRONTEND destination carrying a SHORT, NON-sensitive outcome marker
    — NEVER the Google ``code``/token/``state`` and never an email body. The frontend uses the marker only for a
    customer-safe message and re-reads the authoritative owner-scoped status API. For API/operator callers (no
    ``rt``), the JSON contract is unchanged."""
    if rt:
        from urllib.parse import urlencode
        from django.http import HttpResponseRedirect
        outcome = _OUTCOME_CONNECTED if ok else (code or "error")
        dest = _connect_return_url()
        sep = "&" if "?" in dest else "?"
        return HttpResponseRedirect(f"{dest}{sep}{urlencode({'mailbox_connect': outcome})}")
    return Response(json_body, status=http_status)


def _oauth_client():
    """(client_id, client_secret, redirect_uri) from the ingestion secret store (settings-then-env) — NEVER the
    DB/repo. Empty when unprovisioned (the endpoints then fail with a clear 'not configured')."""
    return _conf("GMAIL_OAUTH_CLIENT_ID"), _conf("GMAIL_OAUTH_CLIENT_SECRET"), _conf("GMAIL_OAUTH_REDIRECT_URI")


# Per-owner serialization for the mailbox-connect MUTATION (SECURITY_HARDENING_2026-10-18). A fixed 32-bit namespace
# combined with the owner's user id into one 63-bit (positive) key for a Postgres TRANSACTION advisory lock. Schema-
# independent (no migration, no table change). See _acquire_owner_mailbox_lock.
_MAILBOX_LOCK_NS = 0x6D_62_78_31   # "mbx1"; stable, arbitrary, distinct from any other advisory-lock namespace


def _mailbox_lock_key(user_id: int) -> int:
    """The 63-bit bigint advisory-lock key for one owner. Pure; the test computes the same key to prove serialization."""
    return (_MAILBOX_LOCK_NS << 32) | (int(user_id) & 0xFFFFFFFF)


def _acquire_owner_mailbox_lock(user_id: int) -> None:
    """Take a per-owner Postgres TRANSACTION advisory lock (``pg_advisory_xact_lock``) so the single-mailbox guard and
    the mailbox write become ATOMIC per owner. Must be called INSIDE ``transaction.atomic()``: the lock is held for
    that transaction and auto-released on commit/rollback. A second concurrent callback for the SAME owner blocks here
    until the first commits, then its guard sees the now-live mailbox and is rejected — closing the two-live TOCTOU.
    It is NEVER held across the Gmail network calls (those happen before the atomic block), so the critical section is
    a short DB-only span. No-op on a non-Postgres backend (advisory locks are a Postgres feature); the app runs only
    on Postgres in every environment, so this degrades safely in e.g. a sqlite unit context without changing logic."""
    from django.db import connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", [_mailbox_lock_key(user_id)])


# Sentinel ``trading_account_id`` that cannot match any real account (FK ids are positive). Used to FAIL CLOSED: a
# specific-account scope naming a malformed / foreign / not-owned id must return an EMPTY scoped result — never
# owner-wide, and never leaking whether the id exists — so it is scoped to this non-existent account.
_UNMATCHABLE_ACCOUNT_ID = 0


def _scoped_account_id(request, owned_ids):
    """Resolve the optional ``?account_id`` for an account-scoped withdrawal query (the dashboard account selector).

    Returns one of:
      * ``None`` — no account requested ⇒ OWNER-WIDE scope (includes the member's owner-attributed, account-UNRESOLVED
        withdrawals).
      * the integer id — an EXACT, OWNED trading-account id ⇒ that account's scope (UNRESOLVED and other-account rows
        are excluded by the ``trading_account_id`` filter downstream).
      * ``_UNMATCHABLE_ACCOUNT_ID`` — an account WAS requested but the id is malformed / foreign / not owned ⇒ a
        SPECIFIC scope that FAILS CLOSED (matches nothing, excludes UNRESOLVED) and never leaks whether the id exists.

    Ownership is validated EXPLICITLY here against the member's own account ids (the raw id is never trusted), on top of
    the owner-scoped query's own intersection firewall downstream (defence in depth). A specific-account scope therefore
    NEVER falls back to owner-wide results."""
    raw = request.query_params.get("account_id")
    if raw in (None, ""):
        return None
    try:
        aid = int(raw)
    except (TypeError, ValueError):
        return _UNMATCHABLE_ACCOUNT_ID
    return aid if aid in owned_ids else _UNMATCHABLE_ACCOUNT_ID


class WithdrawalMetricsView(APIView):
    """GET /api/broker-intelligence/withdrawals/metrics/?account_id= — the member's own withdrawal statistics, OWNER-WIDE
    by default or scoped to ONE of the member's own accounts (the dashboard account selector)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_withdrawal_ux_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from trading.models import TradingAccount
        # ALL of the member's OWN accounts — INCLUDING disconnected/tombstoned ones. A withdrawal is a durable
        # historical fact; a withdrawal a member made on an account they later disconnected is still THEIR history
        # and must not silently vanish from their totals (truthfulness/completeness). Unlike the live-portfolio
        # dashboard (which excludes disconnected accounts because their LIVE balance is gone), this is a historical
        # record. Still strictly owner-scoped (user=request.user) with no staff bypass — never another member's data.
        accounts = list(TradingAccount.objects.filter(user=request.user))
        owned_ids = {a.id for a in accounts}
        # account_id None -> owner-wide (owner=request.user ALSO includes the member's owner-attributed,
        # account-UNRESOLVED withdrawals, ADR-0050); a specific owned id -> that account only (UNRESOLVED excluded);
        # a malformed/foreign id -> fails closed (empty), never owner-wide.
        account_id = _scoped_account_id(request, owned_ids)
        # include_demonstration is OFF in prod (GENUINE-only); ON only in an isolated demo env (labelled synthetic).
        return Response(withdrawal_metrics(accounts, owner=request.user, account_id=account_id,
                                           include_demonstration=broker_withdrawal_demo_include_synthetic()))


class WithdrawalHistoryView(APIView):
    """GET /api/broker-intelligence/withdrawals/history/?broker=&account_id= — the member's own per-withdrawal
    history (rows + the broker/account selector lists), owner-scoped and DARK-gated. Observation-only: no action,
    no financial authority. Shows every lifecycle state truthfully (PENDING in-flight ≠ failed; UNRESOLVED visible)."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_withdrawal_ux_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from trading.models import TradingAccount
        # Owner-scoped (user=request.user), INCLUDING disconnected accounts — a past withdrawal is durable history.
        accounts = list(TradingAccount.objects.filter(user=request.user))
        owned_ids = {a.id for a in accounts}
        broker = str(request.query_params.get("broker", "") or "").strip()
        # A member can only ever filter to ONE OF THEIR OWN accounts: _scoped_account_id validates ownership explicitly
        # (malformed/foreign -> a non-existent account id, fail-closed/empty), and withdrawal_history additionally
        # intersects the id with the member's own account set. A specific account excludes owner-attributed
        # account-UNRESOLVED rows (trading_account NULL); owner-wide (no id) INCLUDES them (ADR-0050).
        account_id = _scoped_account_id(request, owned_ids)
        # include_demonstration is OFF in prod (GENUINE-only); ON only in an isolated demo env (labelled synthetic).
        return Response(withdrawal_history(accounts, broker=broker, account_id=account_id, owner=request.user,
                                           include_demonstration=broker_withdrawal_demo_include_synthetic()))


class WithdrawalSpeedView(APIView):
    """GET /api/broker-intelligence/withdrawals/speed/ — the member's own withdrawal-PROCESSING-SPEED analytics (WS3):
    the typical (median/average) completed processing time, a per-completed-withdrawal trend series, per-broker
    comparison, and live in-progress elapsed ages — computed ONLY from the member's own confidently-paired completed
    withdrawals (pending ages are separate, never mixed in). Owner-scoped (``user=request.user``, no staff bypass) and
    gated by the OWNER-SCOPED, default-DENY cohort flag ``broker_withdrawal_speed_enabled_for`` → 404 while the member
    is outside the cohort (and the GA global is off), so the new section ships DARK and is enabled for one cohort first.
    Observation-only: reads the withdrawal projection; NO mutation, no broker contact, no execution/credential/financial
    authority, no new Gmail scope."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_withdrawal_speed_enabled_for(request.user):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from django.utils import timezone
        from trading.models import TradingAccount
        # ALL of the member's OWN accounts (INCLUDING disconnected/tombstoned) — a past withdrawal is durable history
        # and must not vanish from speed stats. Strictly owner-scoped; never another member's data. owner=request.user
        # ALSO includes owner-attributed, account-UNRESOLVED withdrawals (ADR-0050). include_demonstration OFF in prod.
        accounts = TradingAccount.objects.filter(user=request.user)
        return Response(withdrawal_speed(accounts, owner=request.user, now=timezone.now(),
                                         include_demonstration=broker_withdrawal_demo_include_synthetic()))


def _identity_json(i) -> dict:
    """Owner-private identity summary — masked address, bound account + mailbox, status. No raw email, no secret."""
    return {
        "id": i.id,
        "broker": i.broker_name,
        "email_masked": i.masked_email(),
        "status": i.status,
        "verified": i.status == i.Status.VERIFIED,
        "trading_account_id": i.trading_account_id,
        "connected_mailbox_id": i.connected_mailbox_id,
        "connected_mailbox_masked": i.connected_mailbox.masked_email() if i.connected_mailbox_id else None,
    }


class IdentityListView(APIView):
    """GET /api/broker-intelligence/identities/ — the member's OWN broker-email identities (+ their connected
    mailboxes), for the verify/bind UX. Owner-scoped (user=request.user, no staff bypass); masked; DARK-gated."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_identity_verification_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from .models import BrokerEmailIdentity, ConnectedMailbox
        identities = [_identity_json(i) for i in
                      BrokerEmailIdentity.objects.filter(user=request.user).order_by("broker_name", "id")]
        mailboxes = [{"id": m.id, "email_masked": m.masked_email(), "status": m.status}
                     for m in ConnectedMailbox.objects.filter(user=request.user).order_by("id")]
        return Response({"identities": identities, "mailboxes": mailboxes})


class IdentityVerifyView(APIView):
    """POST /api/broker-intelligence/identities/verify/ — explicitly verify + bind one identity to one of the
    member's OWN trading accounts, on composed proof (mailbox control + account ownership + same owner, fail-closed).
    Body: {email, broker, trading_account_id}. Owner-scoped + audited; DARK-gated; NEVER auto-verifies, never a
    financial action. A refusal returns the stable reason code (safe — the member is acting on their own account)."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not broker_identity_verification_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from .identity_verification import VerificationRefused, audit_refusal, verify_identity
        data = request.data if isinstance(request.data, dict) else {}
        email = str(data.get("email", "") or "").strip()
        broker = str(data.get("broker", "") or "").strip()
        acct_id = data.get("trading_account_id")
        if not email or not broker or acct_id in (None, ""):
            # Audited even for malformed input (contract: EVERY refusal is recorded; a refused attempt is evidence).
            audit_refusal(user=request.user, email=email, broker_name=broker, code="invalid_input",
                          proofs={"attempted_trading_account_id": acct_id})
            return Response({"ok": False, "error": "invalid_input",
                             "message": "A broker, address and trading account are all required."},
                            status=status.HTTP_400_BAD_REQUEST)
        from trading.models import TradingAccount
        # Owner-scoped lookup IS the cross-user firewall at the boundary: an account that is not the member's own
        # resolves to None here (indistinguishable from 'does not exist'), so it can never be bound across users.
        account = TradingAccount.objects.filter(id=acct_id, user=request.user).first()
        if account is None:
            # This is the single most security-relevant event — a member attempting to bind an account they do not
            # own (a refused CROSS-USER bind). It short-circuits before the audited verify_identity service, so we
            # record a DURABLE append-only REJECT here. The audit is internal (returned to no one), so recording the
            # attempt never leaks whether another member's account exists — the generic 422 is unchanged.
            audit_refusal(user=request.user, email=email, broker_name=broker, code="account_not_owned",
                          proofs={"attempted_trading_account_id": acct_id, "account_owned": False})
            return Response({"ok": False, "error": "account_not_owned",
                             "message": "That trading account is not one of yours."},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        try:
            identity = verify_identity(user=request.user, email=email, broker_name=broker, trading_account=account)
        except VerificationRefused as exc:
            http = {"cross_user_conflict": status.HTTP_409_CONFLICT,
                    "account_conflict": status.HTTP_409_CONFLICT,
                    "invalid_input": status.HTTP_400_BAD_REQUEST}.get(exc.code,
                                                                       status.HTTP_422_UNPROCESSABLE_ENTITY)
            return Response({"ok": False, "error": exc.code, "message": exc.message}, status=http)
        return Response({"ok": True, "status": identity.status, "identity_id": identity.id,
                         "identity": _identity_json(identity)})


class MailboxConnectView(APIView):
    """GET /api/broker-intelligence/mailboxes/connect/?op=&mailbox_id=&journey= — the Google consent URL for the
    authenticated member. The CSRF ``state`` is a signed, time-limited token binding the flow to THIS user AND to one
    explicit operation (§6), verified/enforced in the callback.

    OPERATIONS (bound into the signed state, never inferred from the account Google returns):
      * RECONNECT_EXISTING_MAILBOX — requires ``mailbox_id`` of one of the member's OWN mailboxes (owner-scoped lookup;
        a foreign/absent id is a 404). The targeted mailbox's canonical identity is bound as ``expect`` so the callback
        rejects a consent that returns a DIFFERENT Google account — a wrong-account re-consent can never retarget or
        duplicate. (A REVOKED mailbox may be reconnected by id.)
      * CONNECT_FIRST_MAILBOX — the member has NO live mailbox yet; any account is accepted as the first.
      * ADD_NEW_MAILBOX — V1 does not support a second mailbox; REFUSED while a live mailbox exists.
    When no explicit op is given the view infers conservatively: a single live mailbox ⇒ a targeted reconnect of it;
    otherwise a first connect. ``journey=web`` binds a signed return-journey flag so the callback redirects the browser
    back to the fixed GuvFX destination (§7); API/operator callers omit it and get JSON."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_mailbox_connect_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        client_id, _secret, redirect_uri = _oauth_client()
        if not client_id or not redirect_uri:
            return Response({"ok": False, "error": "oauth_not_configured",
                             "message": "Mailbox connection is not configured yet."},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        from . import gmail_oauth
        from .models import ConnectedMailbox
        from .resolver import canonical_address

        requested_op = str(request.query_params.get("op", "") or "").strip()
        if requested_op and requested_op not in _VALID_OPS:
            return Response({"ok": False, "error": "invalid_op",
                             "message": "Unknown mailbox operation."}, status=status.HTTP_400_BAD_REQUEST)
        raw_mid = request.query_params.get("mailbox_id")
        web_journey = str(request.query_params.get("journey", "") or "").strip().lower() == "web"

        # The member's LIVE (non-revoked) Gmail mailboxes — V1 permits at most one.
        live = list(ConnectedMailbox.objects
                    .filter(user=request.user, provider=ConnectedMailbox.Provider.GMAIL)
                    .exclude(status=ConnectedMailbox.Status.REVOKED).order_by("id"))

        # --- resolve the operation + (for reconnect) the OWNED target mailbox ---
        target = None
        if raw_mid not in (None, ""):
            # A specific mailbox was named ⇒ a TARGETED reconnect. Owner-scope the lookup: a foreign/absent id is a 404
            # (indistinguishable from 'does not exist'), so a member can never target another member's mailbox.
            try:
                mid = int(raw_mid)
            except (TypeError, ValueError):
                return Response({"ok": False, "error": "invalid_mailbox_id"}, status=status.HTTP_400_BAD_REQUEST)
            target = ConnectedMailbox.objects.filter(id=mid, user=request.user,
                                                     provider=ConnectedMailbox.Provider.GMAIL).first()
            if target is None:
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            op = _OP_RECONNECT_EXISTING
        elif requested_op == _OP_RECONNECT_EXISTING:
            return Response({"ok": False, "error": "reconnect_requires_mailbox",
                             "message": "Select which connected mailbox to reconnect."},
                            status=status.HTTP_400_BAD_REQUEST)
        elif requested_op in (_OP_CONNECT_FIRST, _OP_ADD_NEW):
            op = requested_op
        elif len(live) == 1:
            # No explicit op: a single live mailbox is reconnected (targeted) rather than silently adding a second.
            target, op = live[0], _OP_RECONNECT_EXISTING
        elif broker_multi_mailbox_enabled_for(request.user) and len(live) > 1:
            # Multi-mailbox: with several live mailboxes and no explicit op/target, NEVER guess which one to act on.
            return Response({"ok": False, "error": "mailbox_op_required",
                             "message": "Specify ADD_NEW_MAILBOX, or which connected mailbox to reconnect."},
                            status=status.HTTP_400_BAD_REQUEST)
        else:
            op = _OP_CONNECT_FIRST

        # --- single-mailbox enforcement at AUTHORIZATION (the authoritative guard is the callback mutation) ---
        # CONNECT_FIRST never applies while any mailbox is live (use ADD_NEW_MAILBOX instead) — unchanged in both modes.
        if op == _OP_CONNECT_FIRST and live:
            # Flag OFF: keep the V1 message verbatim (byte-identical). Flag ON: advise the op that actually works.
            msg = ("A mailbox is already connected. Use ADD_NEW_MAILBOX to connect another."
                   if broker_multi_mailbox_enabled_for(request.user)
                   else "A mailbox is already connected. Disconnect it before connecting another.")
            return Response({"ok": False, "error": "single_mailbox_only", "message": msg},
                            status=status.HTTP_409_CONFLICT)
        # ADD_NEW while a live mailbox exists is refused in single-mailbox (V1) mode; in multi-mailbox mode it is
        # PERMITTED (bounded by the per-owner cap enforced atomically at the callback).
        if op == _OP_ADD_NEW and live and not broker_multi_mailbox_enabled_for(request.user):
            return Response({"ok": False, "error": "single_mailbox_only",
                             "message": "A mailbox is already connected. Disconnect it before connecting another."},
                            status=status.HTTP_409_CONFLICT)
        # A RECONNECT of a (possibly revoked) mailbox while a DIFFERENT live mailbox exists would leave two live in V1
        # mode → refused. In multi-mailbox mode multiple live are allowed (cap-bounded at the callback; the
        # (provider, provider_mailbox_id) unique still prevents a true duplicate), so the reconnect proceeds.
        if (op == _OP_RECONNECT_EXISTING and target is not None and any(m.id != target.id for m in live)
                and not broker_multi_mailbox_enabled_for(request.user)):
            return Response({"ok": False, "error": "single_mailbox_only",
                             "message": "A different mailbox is already connected. Disconnect it first."},
                            status=status.HTTP_409_CONFLICT)

        state_payload = {"uid": request.user.id, "n": secrets.token_urlsafe(8), "op": op}
        if web_journey:
            state_payload["rt"] = True   # signed return-journey indication (§7) — the callback redirects the browser
        if op == _OP_RECONNECT_EXISTING and target is not None:
            # Bind the targeted mailbox + its canonical identity. Re-canonicalize defensively so a legitimate
            # same-account reconnect is never false-rejected by a legacy/non-canonical stored id.
            state_payload["mailbox_pk"] = target.id
            if target.provider_mailbox_id:
                state_payload["expect"] = canonical_address(target.provider_mailbox_id)
        state = signing.dumps(state_payload, salt=_STATE_SALT)
        url = gmail_oauth.build_authorization_url(client_id=client_id, redirect_uri=redirect_uri, state=state)
        return Response({"authorization_url": url, "op": op,
                         "mailbox_id": target.id if target is not None else None})


class MailboxCallbackView(APIView):
    """GET /api/broker-intelligence/mailboxes/callback/?code=&state= — complete the OAuth connection. Verifies the
    CSRF state (signature + age + issued to THIS user), exchanges the code read-only, stores the token ENCRYPTED
    (never in the DB), and records/updates the member's ConnectedMailbox."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_mailbox_connect_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        oauth_error = request.query_params.get("error", "")
        # GOOGLE CONSENT DENIAL (§505.1): when the member declines on Google's consent screen, Google redirects here
        # with an ``error`` (e.g. access_denied) and NO ``code``. That is NOT a server fault and NOT a missing/forged
        # state — surface a customer-safe 'consent_denied' outcome. Recover the return-journey flag from the signed
        # state when present (so a web journey still lands back on the FRONTEND); never echo Google's raw error string.
        if oauth_error:
            try:
                rt = bool(signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE).get("rt")) if state else False
            except signing.BadSignature:
                rt = False
            return _callback_result(rt, ok=False, code="consent_denied",
                                    http_status=status.HTTP_400_BAD_REQUEST,
                                    json_body={"ok": False, "error": "consent_denied"})
        # Pre-state failures stay JSON: with no trusted, validly-signed state we cannot know whether this is a web
        # return-journey, and must never redirect on an unverified signal.
        if not code or not state:
            return Response({"ok": False, "error": "missing_code_or_state"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            payload = signing.loads(state, salt=_STATE_SALT, max_age=_STATE_MAX_AGE)
        except signing.BadSignature:
            return Response({"ok": False, "error": "invalid_state"}, status=status.HTTP_400_BAD_REQUEST)
        # The return-journey flag is read ONLY from the validly-signed state (tamper-evident).
        rt = bool(payload.get("rt"))
        if payload.get("uid") != request.user.id:   # CSRF: state must have been issued to THIS user
            return _callback_result(rt, ok=False, code="state_user_mismatch",
                                    http_status=status.HTTP_400_BAD_REQUEST,
                                    json_body={"ok": False, "error": "state_user_mismatch"})

        client_id, client_secret, redirect_uri = _oauth_client()
        if not client_id or not client_secret or not redirect_uri:
            return _callback_result(rt, ok=False, code="oauth_not_configured",
                                    http_status=status.HTTP_503_SERVICE_UNAVAILABLE,
                                    json_body={"ok": False, "error": "oauth_not_configured"})
        from . import gmail_oauth
        from .gmail_source import fetch_profile
        from .credential_store import store_token
        from .models import ConnectedMailbox
        # RECONNECTION OWNERSHIP INVARIANT at the callback (§6/§505.3, mirrors the connect view's owner-scoped target
        # lookup): a reconnect names a target mailbox in the signed state; that mailbox MUST still exist AND belong to
        # THIS member. If it was deleted or is not owned, refuse BEFORE exchanging the code — never silently fall
        # through and create a NEW mailbox under a reconnect intent.
        op = payload.get("op") or _OP_CONNECT_FIRST
        mailbox_pk = payload.get("mailbox_pk")
        if op == _OP_RECONNECT_EXISTING and mailbox_pk is not None and not (
                ConnectedMailbox.objects.filter(id=mailbox_pk, user=request.user).exists()):
            return _callback_result(rt, ok=False, code="reconnect_target_missing",
                                    http_status=status.HTTP_404_NOT_FOUND,
                                    json_body={"ok": False, "error": "reconnect_target_missing"})
        try:
            token = gmail_oauth.exchange_code(code=code, client_id=client_id, client_secret=client_secret,
                                              redirect_uri=redirect_uri)
            profile = fetch_profile(token["access_token"])
        except Exception:   # noqa: BLE001 — never leak the code/token; a failure is a generic connect error
            return _callback_result(rt, ok=False, code="oauth_exchange_failed",
                                    http_status=status.HTTP_502_BAD_GATEWAY,
                                    json_body={"ok": False, "error": "oauth_exchange_failed"})
        from .resolver import canonical_address
        email = str(profile.get("emailAddress", "") or "")
        # Dedup anchor: the gmail-CANONICAL form (gmail.com/googlemail.com + dots/+tag unified), so the SAME Google
        # mailbox cannot be connected twice under equivalent spellings (R2 / §5). The reported primary_email keeps the
        # address Google returned. Non-Google addresses are unchanged by canonicalisation.
        mailbox_id = canonical_address(email) if email else ("gmail:" + secrets.token_hex(8))
        # Mailbox uniqueness is (provider, provider_mailbox_id) with NO user column (so the same underlying mailbox
        # can never be double-connected / hijacked across members). If this mailbox already belongs to ANOTHER
        # member, fail with a clean 409 BEFORE storing a credential — otherwise the INSERT would raise an unhandled
        # IntegrityError (500) AND leave an orphaned encrypted token file. A re-connect by the SAME owner updates
        # their own row (below) and is fine.
        clash = (ConnectedMailbox.objects
                 .filter(provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id=mailbox_id)
                 .exclude(user=request.user).first())
        if clash is not None:
            return _callback_result(rt, ok=False, code="mailbox_already_connected",
                                    http_status=status.HTTP_409_CONFLICT,
                                    json_body={"ok": False, "error": "mailbox_already_connected"})
        # CREDENTIAL-LIFECYCLE SAFETY (OAUTH_RECOVERY §6): an unattended CONNECTED mailbox needs a USABLE replacement
        # credential. Validate BEFORE store_token/update_or_create so a bad grant can never overwrite the existing
        # working credential/cursor/identity bindings, and no orphaned credential file is written on rejection:
        #   * a mailbox identity (emailAddress) must be present — else the dedup id would be random and a DUPLICATE
        #     mailbox would be created instead of updating the existing row;
        #   * a NON-EMPTY refresh_token — without it the next access-token expiry silently re-breaks ingestion;
        #   * gmail.readonly present and NO write-capable scope (defence-in-depth beyond exchange_code's scope assert).
        from .gmail_oauth import GMAIL_READONLY_SCOPE, _FORBIDDEN_SCOPES
        granted_scopes = set(str(token.get("scope", "")).split())
        if not email:
            return _callback_result(rt, ok=False, code="oauth_no_mailbox_identity",
                                    http_status=status.HTTP_400_BAD_REQUEST,
                                    json_body={"ok": False, "error": "oauth_no_mailbox_identity"})
        # WRONG-ACCOUNT SAFEGUARD (R2 §3/§6): on a TARGETED reconnect the signed state carries the expected canonical
        # mailbox identity. A consent that returns a DIFFERENT Google account is REJECTED before any credential is
        # stored — so it can never create a duplicate mailbox or overwrite the targeted row with a foreign account.
        expected_identity = payload.get("expect")
        if expected_identity and mailbox_id != expected_identity:
            return _callback_result(rt, ok=False, code="oauth_account_mismatch",
                                    http_status=status.HTTP_400_BAD_REQUEST,
                                    json_body={"ok": False, "error": "oauth_account_mismatch"})
        if not token.get("refresh_token"):
            return _callback_result(rt, ok=False, code="oauth_no_refresh_token",
                                    http_status=status.HTTP_400_BAD_REQUEST,
                                    json_body={"ok": False, "error": "oauth_no_refresh_token"})
        if GMAIL_READONLY_SCOPE not in granted_scopes or (granted_scopes & set(_FORBIDDEN_SCOPES)):
            return _callback_result(rt, ok=False, code="oauth_scope_insufficient",
                                    http_status=status.HTTP_400_BAD_REQUEST,
                                    json_body={"ok": False, "error": "oauth_scope_insufficient"})
        # CRITICAL SECTION — serialized per owner (SECURITY_HARDENING_2026-10-18). The single-mailbox GUARD and the
        # mailbox WRITE must be atomic, or two concurrent callbacks returning DIFFERENT Google accounts could both pass
        # the guard before either writes (TOCTOU) and create two live mailboxes. A per-owner Postgres transaction
        # advisory lock makes them atomic: the second concurrent callback blocks at the lock until the first commits,
        # then its guard sees the now-live mailbox and is rejected. The Gmail network calls already happened ABOVE, so
        # this span is DB-only and short. (Schema-independent — no migration.)
        from django.db import IntegrityError, transaction
        from django.db.models import F
        from .erasure_lock import acquire_owner_erasure_lock
        credential_ref = None
        try:
            with transaction.atomic():
                _acquire_owner_mailbox_lock(request.user.id)
                acquire_owner_erasure_lock(request.user.id)   # P0 (ADR-0054): serialise connect against erasure
                # AUTHORITATIVE mailbox-count guard, serialized per owner (the TOCTOU-safe mutation point). Count the
                # member's OTHER live (non-revoked) mailboxes — `.exclude(provider_mailbox_id=...)` lets a legitimate
                # in-place reconnect of the member's OWN mailbox through, so it never counts against itself.
                other_live = (ConnectedMailbox.objects
                              .filter(user=request.user, provider=ConnectedMailbox.Provider.GMAIL)
                              .exclude(status=ConnectedMailbox.Status.REVOKED)
                              .exclude(provider_mailbox_id=mailbox_id).count())
                if broker_multi_mailbox_enabled_for(request.user):
                    # Multi-mailbox: the post-write total (other_live + this one) must stay within the per-owner cap.
                    if other_live >= _mailbox_cap():
                        return _callback_result(rt, ok=False, code="mailbox_limit_reached",
                                                http_status=status.HTTP_409_CONFLICT,
                                                json_body={"ok": False, "error": "mailbox_limit_reached"})
                elif other_live > 0:
                    # V1 single-mailbox (flag OFF): any other live mailbox means this write would create a second.
                    return _callback_result(rt, ok=False, code="single_mailbox_only",
                                            http_status=status.HTTP_409_CONFLICT,
                                            json_body={"ok": False, "error": "single_mailbox_only"})
                credential_ref = store_token(token)   # encrypted; the DB stores only the ref
                common = dict(primary_email=email, credential_ref=credential_ref, scopes=token.get("scope", ""),
                              status=ConnectedMailbox.Status.CONNECTED)
                mb, _created = ConnectedMailbox.objects.update_or_create(
                    user=request.user, provider=ConnectedMailbox.Provider.GMAIL, provider_mailbox_id=mailbox_id,
                    # cursor_state is anchored to the current historyId ONLY on initial create. On a RE-connect an
                    # in-progress cursor MUST be preserved: overwriting it to 'now' would permanently skip every
                    # message added since the last sync not yet consumed (a lost-mail path). (Django 5.0+ create_defaults.)
                    create_defaults={**common, "cursor_state": str(profile.get("historyId", "") or "")},
                    defaults=common)
                # Bump the ingestion generation on every (re)connect so an in-flight ingestion pass that captured
                # an older generation refuses its stale write under the erasure lock (ADR-0054 P0).
                ConnectedMailbox.objects.filter(pk=mb.id).update(generation=F("generation") + 1)
        except IntegrityError:
            # Cross-user race: a different member connected the SAME underlying mailbox between the pre-lock clash
            # check and this write, so the global (provider, provider_mailbox_id) unique constraint rejected the
            # INSERT. Clean up the just-written credential so no orphan remains, and report a clean 409 (never a 500).
            if credential_ref:
                from .credential_store import delete_token as _del
                try:
                    _del(credential_ref)
                except Exception:   # noqa: BLE001 — best-effort cleanup; never mask the 409
                    pass
            return _callback_result(rt, ok=False, code="mailbox_already_connected",
                                    http_status=status.HTTP_409_CONFLICT,
                                    json_body={"ok": False, "error": "mailbox_already_connected"})
        # Web journey: a NON-sensitive 'connected' marker only (never the email/token/code). The JSON contract
        # (API/operator) is unchanged and still reports the connected address.
        return _callback_result(rt, ok=True, code="connected",
                                http_status=status.HTTP_200_OK,
                                json_body={"ok": True, "status": "connected", "mailbox_id": mb.id, "email": email})


class MailboxRevokeView(APIView):
    """POST /api/broker-intelligence/mailboxes/<id>/revoke/ — the member revokes a connected mailbox: destroy the
    stored credential and mark it revoked, independently of any other mailbox. Owner-scoped."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, mailbox_id):
        if not broker_mailbox_connect_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from .credential_store import delete_token
        from .models import ConnectedMailbox
        mb = ConnectedMailbox.objects.filter(id=mailbox_id, user=request.user).first()   # owner-scoped
        if mb is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if mb.credential_ref:
            delete_token(mb.credential_ref)
        mb.credential_ref = ""
        mb.status = ConnectedMailbox.Status.REVOKED
        mb.save(update_fields=["credential_ref", "status", "updated_at"])
        return Response({"ok": True, "status": "revoked", "mailbox_id": mb.id})


class MailboxStatusView(APIView):
    """GET /api/broker-intelligence/mailboxes/status/ — owner-scoped, customer-safe status of THIS member's connected
    mailbox(es), for the Connected-Email-Accounts UI. DARK (``broker_mailbox_status_enabled`` -> 404 while off) and
    independent of the identity-verification flag. Returns ONLY customer-safe fields (conceptual state, provider,
    masked email, last successful sync, reconnect-required, whether verified broker bindings exist) — NEVER a token,
    credential ref, raw cursor, email body, diagnostic code, or another member's mailbox."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not broker_mailbox_status_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from .models import ConnectedMailbox, BrokerEmailIdentity
        from .health import customer_mailbox_status, r1_columns_present, STATUS_STABLE_ONLY
        from .resolver import canonical_address
        from trading.models import TradingAccount

        # SCHEMA-0010 SAFETY (§3): production runs broker_intelligence migration 0010 — the migration-0011 health
        # columns do NOT exist yet. A default ``SELECT *`` over ConnectedMailbox would reference those columns and
        # raise ProgrammingError. So when the R1 columns are absent, select ONLY the pre-existing (0010) columns via
        # an explicit projection, and tell customer_mailbox_status not to read an R1 attribute (a deferred-column load
        # would hit the same missing column). When the columns DO exist, load everything so the status can reflect an
        # explicit re-auth diagnostic. This is introspection-driven, never a try/except-around-a-failed-query.
        r1 = r1_columns_present()
        qs = ConnectedMailbox.objects.filter(user=request.user).order_by("id")   # owner-scoped
        if not r1:
            qs = qs.only(*STATUS_STABLE_ONLY)
        mboxes = list(qs)
        verified = list(BrokerEmailIdentity.objects.filter(
            user=request.user, status=BrokerEmailIdentity.Status.VERIFIED))
        # The member's OWN trading accounts — a verified binding requires a trading account that is actually theirs,
        # never an identity whose account is NULL or (defensively) not owned by this member.
        owned_account_ids = set(TradingAccount.objects.filter(user=request.user).values_list("id", flat=True))

        def _bound(mb) -> bool:
            # A VERIFIED broker identity counts as a verified broker BINDING for this mailbox ONLY when it has a
            # NON-NULL trading account owned by this member AND is associated with THIS mailbox — by the
            # connected_mailbox FK, or by its canonical email matching the mailbox's canonical provider_mailbox_id
            # (gmail/googlemail unified). Email ownership ALONE is insufficient (§5): a verified-but-unbound address, or
            # one bound to no / another account, must not read as "verified broker account".
            for i in verified:
                if i.trading_account_id is None or i.trading_account_id not in owned_account_ids:
                    continue
                if i.connected_mailbox_id == mb.id:
                    return True
                if i.email and mb.provider_mailbox_id and canonical_address(i.email) == mb.provider_mailbox_id:
                    return True
            return False

        items = [customer_mailbox_status(mb, has_verified_bindings=_bound(mb), include_error_fields=r1)
                 for mb in mboxes]
        # CONNECTED means at least one mailbox is actually CONNECTED (§5). A member whose only mailbox is REVOKED is
        # NOT connected — the UI must offer a fresh connect, not present a dead mailbox as active.
        connected = any(getattr(mb, "status", "") == ConnectedMailbox.Status.CONNECTED for mb in mboxes)
        # can_add_mailbox: owner-scoped multi-mailbox cohort gate (default-DENY) AND below the per-owner cap counting
        # the member's non-revoked mailboxes. Drives the "Add email account" control — never exposes the flag/cohort
        # config itself, only this member's own boolean eligibility. Single-mailbox members (not in the cohort) get False.
        live = sum(1 for mb in mboxes if getattr(mb, "status", "") != ConnectedMailbox.Status.REVOKED)
        can_add = broker_multi_mailbox_enabled_for(request.user) and live < _mailbox_cap()
        return Response({"connected": connected, "mailboxes": items,
                         "can_add_mailbox": can_add, "mailbox_limit": _mailbox_cap()})


# ======================================================================================================
# Account-level broker-email CONTACT API (MULTI_ACCOUNT §A/§C/§4) — DARK (broker_account_contact_enabled).
# One (email, broker) identity may bind to MANY of the member's OWN accounts; binding is explicit + audited;
# verification reuses the fail-closed contact_verification writer (the migration-0015 DB trigger is the hard backstop).
# ======================================================================================================

def _contact_json(c) -> dict:
    """Owner-private binding summary — masked identity + mailbox, status. No raw email, no secret."""
    idn = c.broker_email_identity
    return {
        "id": c.id,
        "trading_account_id": c.trading_account_id,
        "broker": idn.broker_name,
        "email_masked": idn.masked_email(),
        "broker_email_identity_id": c.broker_email_identity_id,
        "connected_mailbox_id": c.connected_mailbox_id,
        "connected_mailbox_masked": c.connected_mailbox.masked_email() if c.connected_mailbox_id else None,
        "status": c.status,
        "verified": c.status == c.Status.VERIFIED,
        "verified_at": c.verified_at.isoformat() if c.verified_at else None,
    }


def _owned_account_or_none(request, account_id):
    """Owner-scoped account lookup — the cross-user firewall at the boundary (a foreign/absent id is indistinguishable
    from 'does not exist')."""
    from trading.models import TradingAccount
    return TradingAccount.objects.filter(id=account_id, user=request.user).first()


class AccountBrokerContactListView(APIView):
    """GET /api/broker-intelligence/accounts/<account_id>/contacts/ — the member's broker-email bindings for ONE of
    their OWN trading accounts (masked). Owner-scoped (no staff bypass); DARK-gated."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, account_id):
        if not broker_account_contact_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        account = _owned_account_or_none(request, account_id)
        if account is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from .models import TradingAccountBrokerContact
        rows = (TradingAccountBrokerContact.objects
                .filter(trading_account=account).select_related("broker_email_identity", "connected_mailbox")
                .order_by("id"))
        return Response({"trading_account_id": account.id, "contacts": [_contact_json(c) for c in rows]})


class AccountBrokerContactCreateView(APIView):
    """POST /api/broker-intelligence/accounts/<account_id>/contacts/create/ — associate a broker-registered
    notification email with one of the member's OWN accounts. Body: {email, broker, connected_mailbox_id?}. Creates
    the (email, broker) identity if new, CLAIMS it only when TRULY unowned (no user AND not attributed to another
    member's account — the same effective-owner rule the verify service/DB trigger use), refuses a DIFFERENT-member's
    identity GENERICALLY (message never reveals WHICH member) under a row lock, and get_or_creates the PENDING binding
    (idempotent under uniq_tabc_account_identity). Owner-scoped + audited; NEVER auto-verifies (status stays PENDING);
    a mailbox change on an existing binding resets it to PENDING. NB (bounded residual): the 422 cross-user refusal is
    distinguishable from success, so a member-typed address can be probed for existing cross-member registration (a
    coarse, authenticated, audited, per-user-throttled signal — never which member); a tighter create-throttle is a
    documented pre-activation hardening."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, account_id):
        if not broker_account_contact_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        account = _owned_account_or_none(request, account_id)
        if account is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        data = request.data if isinstance(request.data, dict) else {}
        email = str(data.get("email", "") or "").strip()
        broker = str(data.get("broker", "") or "").strip()
        cmid = data.get("connected_mailbox_id")
        if not email or not broker:
            return Response({"ok": False, "error": "invalid_input",
                             "message": "A broker and a broker-registered email are required."},
                            status=status.HTTP_400_BAD_REQUEST)
        from django.db import transaction
        from .models import (BrokerEmailIdentity, ConnectedMailbox, TradingAccountBrokerContact,
                             TradingAccountBrokerContactAudit)
        # Owner-scope the optional mailbox (a foreign mailbox id is simply ignored — never bound cross-user).
        mailbox = None
        if cmid not in (None, ""):
            mailbox = ConnectedMailbox.objects.filter(id=cmid, user=request.user).first()
        with transaction.atomic():
            from .erasure_lock import acquire_owner_erasure_lock
            acquire_owner_erasure_lock(request.user.id)   # P0 (ADR-0054): serialise against governed erasure
            # Lock the identity row for the claim decision so two members cannot race to claim one unowned identity
            # (last-writer-wins). Same discipline as verify_identity.
            identity = (BrokerEmailIdentity.objects.select_for_update()
                        .filter(email=email, broker_name=broker).first())
            if identity is None:
                identity = BrokerEmailIdentity.objects.create(email=email, broker_name=broker, user=request.user)
            else:
                # EFFECTIVE owner: the explicit user, else the owner of the account this identity is attributed to.
                # Both the verify service (contact_verification.check_consistency) and the migration-0015 VERIFIED
                # trigger treat trading_account.user as authoritative ownership, so the firewall MUST too — else a
                # backfilled (user=None, trading_account=<other member>) identity could be CLAIMED and later verified
                # by any member (cross-user takeover).
                eff_owner = identity.user_id or (
                    identity.trading_account.user_id if identity.trading_account_id else None)
                if eff_owner is not None and eff_owner != request.user.id:
                    # Refuse GENERICALLY + durable REJECT. (NB: a 422 here is distinguishable from the 201/200 success
                    # path, so a determined authenticated caller can probe whether a MEMBER-TYPED address is already
                    # registered to SOME other member — a coarse, audited, per-user-throttled existence signal, never
                    # revealing WHICH member. A tighter create throttle is a documented pre-activation hardening.)
                    TradingAccountBrokerContactAudit.objects.create(
                        action=TradingAccountBrokerContactAudit.Action.REJECT, subject_user=request.user,
                        trading_account=account, outcome="cross_user",
                        reason="address registered to another member", status_after="")
                    return Response({"ok": False, "error": "cannot_associate",
                                     "message": "That broker email can't be associated with this account."},
                                    status=status.HTTP_422_UNPROCESSABLE_ENTITY)
                if identity.user_id is None:
                    identity.user = request.user   # claim only a TRULY unowned (+unattributed) registry row
                    identity.save(update_fields=["user"])
            contact, created = TradingAccountBrokerContact.objects.get_or_create(
                trading_account=account, broker_email_identity=identity,
                defaults={"connected_mailbox": mailbox})
            mailbox_changed = not created and mailbox is not None and contact.connected_mailbox_id != mailbox.id
            if mailbox_changed:
                # Changing the receiving mailbox invalidates any prior verification (different routing): reset to
                # PENDING + clear verified_at so routing must be re-proven via contact_verification (never a silent
                # re-point of a VERIFIED binding).
                contact.connected_mailbox = mailbox
                status_before = contact.status
                contact.status = TradingAccountBrokerContact.Status.PENDING
                contact.verified_at = None
                contact.save(update_fields=["connected_mailbox", "status", "verified_at", "updated_at"])
            if created or mailbox_changed:
                TradingAccountBrokerContactAudit.objects.create(
                    contact=contact, action=TradingAccountBrokerContactAudit.Action.BIND, subject_user=request.user,
                    trading_account=account, broker_email_identity=identity, connected_mailbox=mailbox,
                    outcome="OK", status_before="" if created else status_before, status_after=contact.status)
        return Response({"ok": True, "contact": _contact_json(contact)},
                        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class AccountBrokerContactVerifyView(APIView):
    """POST /api/broker-intelligence/accounts/<account_id>/contacts/<contact_id>/verify/ — attempt to VERIFY one of
    the member's OWN bindings via the fail-closed contact_verification writer (composed ownership + mailbox + broker
    proof). NEVER auto-verifies; the migration-0015 DB trigger is the hard backstop. Returns the resulting status +
    reason. Owner-scoped + audited; never a financial action."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, account_id, contact_id):
        if not broker_account_contact_enabled():
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        from .contact_verification import check_consistency, verify_contact
        from .models import TradingAccountBrokerContact
        contact = (TradingAccountBrokerContact.objects
                   .filter(id=contact_id, trading_account_id=account_id, trading_account__user=request.user)
                   .select_related("trading_account", "broker_email_identity", "connected_mailbox").first())
        if contact is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        _ok, reason = check_consistency(contact)
        contact = verify_contact(contact, actor=request.user, proofs={"via": "account_contact_api"})
        return Response({"verified": contact.status == contact.Status.VERIFIED, "status": contact.status,
                         "reason": reason, "contact": _contact_json(contact)})
