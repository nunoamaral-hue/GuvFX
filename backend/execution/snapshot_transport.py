"""P0 DATA-ISOLATION — per-tenant MT5 *read* (snapshot) transport + downstream identity firewall.

**Why this exists.** ``order_transport`` made the ORDER-PLACEMENT destination follow the account's own
per-tenant execution endpoint. The customer-specific *read* paths (deal history for trade-ingest, live
deals, live balance) were NOT migrated: they used a single module-global ``AGENT_BASE``. On the co-resident
multi-tenant host that base is ONE tenant's bridge, and every per-tenant bridge's ``/mt5/snapshots/*``
handler attaches to its OWN fixed ``MT5_TERMINAL_PATH`` and ignores the ``username`` query param — so a read
issued for Customer A but sent to Customer B's bridge returns Customer B's financial data. That is the
confirmed P0 breach (a fresh customer's Trade History was populated with support@'s deals).

This module gives customer-specific MT5 reads the SAME authority chain the order path already uses:

    authenticated customer -> TradingAccount -> HostedMt5Workspace -> AccountProvisioning.windows_username
      -> HostedExecutionEndpoint (per-tenant host:port) -> the customer's OWN bridge/process/terminal
      -> expected broker login (== TradingAccount.account_number) + server.

Two layers, both required:

* **UPSTREAM routing** (``resolve_account_snapshot_base``): resolve the read destination from the account's
  OWN endpoint — never a caller-supplied URL, never a module-global bridge, never another tenant. A hosted
  account whose endpoint is missing / not READY fails CLOSED (the read is refused, not sent to a global or
  sibling bridge). A genuinely endpoint-less legacy account (Customer Zero pre-endpoint) keeps the global
  agent, byte-for-byte.

* **DOWNSTREAM firewall** (``verify_snapshot_identity``): even a correctly-routed read must prove the MT5
  session it actually reached is this account's. The bridge returns the terminal's observed
  ``account_login`` / ``account_server`` (read from ``account_info`` in the SAME session that produced the
  deals); the caller compares them to the account's expected identity and, on ANY mismatch or missing
  observation, persists / returns ZERO customer rows. Deals carry no per-deal login, so the WHOLE batch is
  bound to the independently-observed session identity (never invent a per-deal login field).

The module performs NO order, attach, host action or persistence — it selects a URL and validates identity.
Secret-free. Fail-closed on every ambiguity.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── snapshot-transport resolution reason codes (stable, secret-free) ──
ST_PER_TENANT_OK = "snapshot_transport_per_tenant_ok"          # hosted -> resolved account endpoint bridge
ST_LEGACY_GLOBAL = "snapshot_transport_legacy_global"          # endpoint-less legacy account -> global agent
ST_ENDPOINT_NOT_READY = "snapshot_transport_endpoint_not_ready"  # endpoint exists but not READY
ST_ENDPOINT_UNCONFIGURED = "snapshot_transport_endpoint_unconfigured"  # endpoint READY but no base_url
ST_HOSTED_NO_ENDPOINT = "snapshot_transport_hosted_no_endpoint"  # hosted account with no endpoint at all
ST_NO_ACCOUNT = "snapshot_transport_no_account"                # no account to resolve
ST_RESOLVE_ERROR = "snapshot_transport_resolve_error"          # any error resolving a hosted route

# ── identity-firewall reason codes ──
ID_OK = "snapshot_identity_ok"
ID_NO_EXPECTED_LOGIN = "snapshot_identity_no_expected_login"   # account has no account_number to expect
ID_OBSERVED_MISSING = "snapshot_identity_observed_missing"     # bridge returned no observed login/server
ID_LOGIN_MISMATCH = "snapshot_identity_login_mismatch"         # observed login != expected (WRONG tenant)
ID_SERVER_MISMATCH = "snapshot_identity_server_mismatch"       # observed server != expected server


@dataclass(frozen=True)
class SnapshotTransport:
    """Resolved read-bridge destination for ONE account's customer-specific MT5 read.

    ``ok`` gates the read. When NOT ``ok`` the caller MUST refuse — never read a global or sibling bridge."""
    ok: bool
    reason_code: str
    base_url: str = ""
    per_tenant: bool = False

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason_code": self.reason_code,
                "base_url": self.base_url, "per_tenant": self.per_tenant}


@dataclass(frozen=True)
class IdentityCheck:
    ok: bool
    reason_code: str
    expected_login: str = ""
    observed_login: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason_code": self.reason_code,
                "expected_login": self.expected_login, "observed_login": self.observed_login}


def _clean(url) -> str:
    return str(url or "").strip().rstrip("/")


def _norm(v) -> str:
    return str(v if v is not None else "").strip()


def resolve_account_snapshot_base(account, *, global_base_url) -> SnapshotTransport:
    """Resolve the per-tenant read-bridge base URL for ``account`` (deals / account_info snapshots).

    Every account reads ONLY its OWN endpoint's bridge. Precedence:

      1. account has a non-retired ``HostedExecutionEndpoint`` -> it MUST be READY with a ``base_url``
         (``ST_PER_TENANT_OK``); a not-READY / URL-less endpoint fails CLOSED (never global, never sibling);
      2. no endpoint + the account classifies HOSTED -> fail CLOSED (``ST_HOSTED_NO_ENDPOINT``) — a hosted
         tenant is NEVER read via the global agent;
      3. no endpoint + non-hosted (Customer Zero / Provider-A legacy) -> the global agent, byte-for-byte.
    """
    if account is None:
        return SnapshotTransport(False, ST_NO_ACCOUNT)
    try:
        from execution.models import HostedExecutionEndpoint
        acct_id = getattr(account, "id", None)
        ep = (HostedExecutionEndpoint.objects
              .filter(trading_account_id=acct_id)
              .exclude(state=HostedExecutionEndpoint.State.RETIRED)
              .first())
        if ep is not None:
            # Defence in depth: the loaded row's owner must be this account (a future query/join change can
            # never silently cross tenants).
            if ep.trading_account_id != acct_id:
                return SnapshotTransport(False, ST_ENDPOINT_UNCONFIGURED)
            if ep.state != HostedExecutionEndpoint.State.READY:
                return SnapshotTransport(False, ST_ENDPOINT_NOT_READY)
            base = _clean(ep.base_url)
            if not base:
                return SnapshotTransport(False, ST_ENDPOINT_UNCONFIGURED)
            return SnapshotTransport(True, ST_PER_TENANT_OK, base, per_tenant=True)

        # No endpoint: a hosted account must NEVER fall back to the shared global agent. Classify hosted-ness
        # from the DURABLE, FLAG-INDEPENDENT provisioning field (``readiness_provider``, stamped once at
        # provisioning) — NOT the flag-gated execution classifier ``is_hosted_workspace_account`` (which
        # returns False whenever the DARK master flag ``HOSTED_PERSISTENT_MT5_ENABLED`` is off, BEFORE it ever
        # reads readiness_provider). A data-isolation boundary must not silently OPEN to the global/sibling
        # bridge just because an unrelated execution flag is off. This mirrors ``endpoint_service._derive_identity``,
        # which reads the durable provisioning identity precisely so it "must NOT depend on the runtime
        # transport flag being on".
        from execution.readiness import PERSISTENT_WORKSPACE
        if str(getattr(account, "readiness_provider", "") or "") == PERSISTENT_WORKSPACE:
            return SnapshotTransport(False, ST_HOSTED_NO_ENDPOINT)

        base = _clean(global_base_url)
        if not base:
            return SnapshotTransport(False, ST_ENDPOINT_UNCONFIGURED)
        return SnapshotTransport(True, ST_LEGACY_GLOBAL, base, per_tenant=False)
    except Exception:  # noqa: BLE001 — any resolution error on a customer-specific read fails closed
        return SnapshotTransport(False, ST_RESOLVE_ERROR)


def expected_identity(account) -> tuple[str, str]:
    """The account's authoritative expected MT5 identity: broker login == ``account_number`` (the immutable
    broker account id), server == the bound ``broker_server.server_name``. Endpoint snapshot fields are NOT
    trusted here (they can be blank / placeholders for a deferred-bound hosted workspace)."""
    login = _norm(getattr(account, "account_number", ""))
    server = ""
    bs = getattr(account, "broker_server", None)
    if bs is not None:
        server = _norm(getattr(bs, "server_name", ""))
    return login, server


def verify_snapshot_identity(account, observed_login, observed_server, *, require_server=False) -> IdentityCheck:
    """Downstream firewall: the observed MT5 session identity MUST match the account's expected identity.

    ``observed_*`` come from the bridge's own ``account_info`` in the SAME session that produced the payload.
    Fail-closed: a missing expected login, a missing observation, or ANY mismatch refuses the whole payload —
    the caller persists / returns ZERO customer rows. ``require_server`` additionally enforces the server
    name (kept optional because a hosted workspace's server may be a placeholder until broker bind; the login
    == account_number check is the load-bearing tenant discriminator)."""
    exp_login, exp_server = expected_identity(account)
    obs_login = _norm(observed_login)
    obs_server = _norm(observed_server)
    if not exp_login:
        return IdentityCheck(False, ID_NO_EXPECTED_LOGIN, exp_login, obs_login)
    if not obs_login:
        return IdentityCheck(False, ID_OBSERVED_MISSING, exp_login, obs_login)
    if obs_login != exp_login:
        return IdentityCheck(False, ID_LOGIN_MISMATCH, exp_login, obs_login)
    if require_server and exp_server and obs_server and obs_server != exp_server:
        return IdentityCheck(False, ID_SERVER_MISMATCH, exp_login, obs_login)
    return IdentityCheck(True, ID_OK, exp_login, obs_login)
