"""ADR-0034 Workspace Delivery — the owner-authorised RemoteApp delivery service (DARK).

This is the SECURITY LINCHPIN of the delivery subsystem. Given an authenticated user and a workspace
identifier, it decides whether that user may open THEIR OWN persistent MT5 as a RemoteApp, and if so mints
the exact signed Guacamole connection descriptor the future RDS/RemoteApp host will consume.

Two hard properties, both fail-closed:

- **Owner-bound.** A user may only deliver a workspace whose ``trading_account.user`` is themselves. There
  is DELIBERATELY no staff/superuser cross-user bypass here — minting a delivery descriptor embeds the
  workspace's Windows credential and opens a live credentialed session, so it requires actual ownership
  (least privilege). Staff retain a READ-ONLY bypass on the delivery-*state* API only (parity with M3c),
  never on minting. Any cross-user / missing / malformed identifier fails closed with a stable reason code.

- **Everything server-derived.** The ONLY client-supplied value is the workspace identifier. The RDP host,
  the Windows username, the RemoteApp program + working dir + args, the connection id, and the credential
  are ALL derived server-side from durable records (``HostedMt5Workspace.workspace_node`` /
  ``AccountProvisioning``). The client can influence none of them. The client receives ONLY the 4-field
  safe descriptor (``transport_type``/``embed_url``/``session_token=''``/``expiry``); host, username,
  program and the Windows password ride ONLY inside the server-minted AES-encrypted guacamole-auth-json
  token and never appear in the return value, a log line, or a persisted field.

DARK: both ``hosted_persistent_mt5_enabled()`` (master) AND ``hosted_mt5_remoteapp_enabled()`` (delivery)
must be ON or the service denies before any DB read — the subsystem is invisible while OFF. This module
performs NO order, NO attach, NO login; it consumes durable records and returns a descriptor. State
writes + telemetry live in ``delivery_persistence`` (the single delivery-state writer), not here.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from hosted_workspace.flags import hosted_mt5_remoteapp_enabled, hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace

logger = logging.getLogger("guvfx.hosted_workspace")

# The RemoteApp program alias published on the host's RemoteApp allow-list (a HOST/deploy constant, never
# per-user, never client-supplied). ``build_remoteapp_rdp_payload`` prefixes it with ``||`` for FreeRDP.
REMOTEAPP_ALIAS = "terminal64"
# The MT5 command line for a portable, view-managed launch (server constant).
REMOTEAPP_ARGS = "/portable"


class DeliveryReason:
    """Stable, secret-free outcome codes (parity with the execution engine's ``ER_*`` taxonomy)."""
    OK = "DA_OK"
    SUBSYSTEM_DISABLED = "DA_SUBSYSTEM_DISABLED"        # master or delivery flag OFF (DARK)
    INVALID_REQUEST = "DA_INVALID_REQUEST"             # unauthenticated / malformed workspace id
    WORKSPACE_MISSING = "DA_WORKSPACE_MISSING"         # no workspace with that uuid
    NOT_OWNER = "DA_NOT_OWNER"                         # workspace owned by another user (IDOR-safe)
    NODE_UNASSIGNED = "DA_NODE_UNASSIGNED"             # no workspace_node → cannot derive host
    NODE_TRANSPORT_UNCONFIGURED = "DA_NODE_TRANSPORT_UNCONFIGURED"  # node has no rdp_host → no delivery transport
    IDENTITY_MISSING = "DA_IDENTITY_MISSING"           # no AccountProvisioning / no windows_username
    IDENTITY_ADMIN = "DA_IDENTITY_ADMIN"               # identity is admin — MUST be non-admin (hard fail)
    IDENTITY_NOT_PROVISIONED = "DA_IDENTITY_NOT_PROVISIONED"  # provisioning status not usable
    IDENTITY_NO_CREDENTIAL = "DA_IDENTITY_NO_CREDENTIAL"  # no password_enc → would mint a dud descriptor
    RUNTIME_MISSING = "DA_RUNTIME_MISSING"             # no runtime_root → cannot derive RemoteApp dir
    GUAC_UNCONFIGURED = "DA_GUAC_UNCONFIGURED"         # GUAC_BASE_URL / GUAC_JSON_SECRET_KEY_HEX unset
    ERROR = "DA_ERROR"                                 # unexpected error, fail-closed


@dataclass(frozen=True)
class DeliveryAuthorization:
    """The delivery decision — a description, never itself an action. ``descriptor`` (when authorised) holds
    ONLY the 4 client-safe fields; it never carries the Windows password, which lives solely inside the AES
    token embedded in ``descriptor['embed_url']``. ``workspace_pk`` is set once the workspace is resolved AND
    owned by the caller — it gates the delivery-state write (never write another user's workspace)."""
    authorized: bool
    reason: str
    workspace_uuid: str = ""
    workspace_pk: Optional[int] = None
    descriptor: Optional[dict] = None


def _deny(reason: str, *, workspace_uuid: str = "", workspace_pk: Optional[int] = None) -> DeliveryAuthorization:
    return DeliveryAuthorization(
        authorized=False, reason=reason, workspace_uuid=workspace_uuid,
        workspace_pk=workspace_pk, descriptor=None)


def _coerce_workspace_uuid(workspace_id) -> Optional[uuid.UUID]:
    """A usable workspace identifier is a UUID (the immutable, non-enumerable logical identity). Anything
    else -> None so the caller maps it to INVALID_REQUEST. Rejecting a raw sequential PK here is deliberate:
    delivery is keyed on the unguessable ``workspace_uuid``, not on a guessable row id."""
    if isinstance(workspace_id, uuid.UUID):
        return workspace_id
    try:
        return uuid.UUID(str(workspace_id))
    except (ValueError, TypeError, AttributeError):
        return None


def authorize_workspace_delivery(user, workspace_id) -> DeliveryAuthorization:
    """Decide whether ``user`` may deliver workspace ``workspace_id`` as a RemoteApp, and if so mint the
    signed descriptor. Owner-bound, fail-closed, server-derived. Performs NO state write (see
    ``delivery_persistence``). Returns a :class:`DeliveryAuthorization`."""
    # DARK gate FIRST — before any DB read — so the subsystem is invisible while OFF.
    if not (hosted_persistent_mt5_enabled() and hosted_mt5_remoteapp_enabled()):
        return _deny(DeliveryReason.SUBSYSTEM_DISABLED)

    if user is None or not getattr(user, "is_authenticated", False):
        return _deny(DeliveryReason.INVALID_REQUEST)

    wuuid = _coerce_workspace_uuid(workspace_id)
    if wuuid is None:
        return _deny(DeliveryReason.INVALID_REQUEST)

    try:
        workspace = (HostedMt5Workspace.objects
                     .select_related("trading_account", "workspace_node")
                     .filter(workspace_uuid=wuuid).first())
        if workspace is None:
            return _deny(DeliveryReason.WORKSPACE_MISSING)

        wuuid_str = str(workspace.workspace_uuid)

        # OWNER CHECK — the security linchpin. Strict equality, NO staff bypass (see module docstring).
        # A non-owner gets NOT_OWNER without any workspace_pk, so nothing downstream can write their row.
        owner_id = workspace.trading_account.user_id
        if owner_id is None or owner_id != getattr(user, "id", None):
            return _deny(DeliveryReason.NOT_OWNER, workspace_uuid=wuuid_str)

        # From here the caller owns the workspace — carry workspace_pk so a FAILED attempt can be recorded
        # on THEIR OWN workspace (never on anyone else's).
        wpk = workspace.pk

        node = workspace.workspace_node
        if node is None or not node.hostname:
            return _deny(DeliveryReason.NODE_UNASSIGNED, workspace_uuid=wuuid_str, workspace_pk=wpk)
        # Delivery TRANSPORT endpoint. ``node.hostname`` is the logical execution-node IDENTITY and is
        # NEVER the RDP address (guacd cannot necessarily reach a logical node name). The RemoteApp
        # descriptor host is the DEDICATED ``node.rdp_host``. Fail closed on a missing transport rather than
        # silently substituting the identity (which would send guacd to an unroutable/incorrect host).
        if not node.rdp_host:
            return _deny(DeliveryReason.NODE_TRANSPORT_UNCONFIGURED, workspace_uuid=wuuid_str, workspace_pk=wpk)

        from terminal_provisioning.models import AccountProvisioning

        prov = AccountProvisioning.objects.filter(
            trading_account_id=workspace.trading_account_id).first()
        if prov is None or not prov.windows_username:
            return _deny(DeliveryReason.IDENTITY_MISSING, workspace_uuid=wuuid_str, workspace_pk=wpk)
        # A delivered identity MUST be non-administrator. This is an invariant, not a preference.
        if prov.is_admin:
            return _deny(DeliveryReason.IDENTITY_ADMIN, workspace_uuid=wuuid_str, workspace_pk=wpk)
        if prov.status != AccountProvisioning.Status.PROVISIONED:
            return _deny(DeliveryReason.IDENTITY_NOT_PROVISIONED, workspace_uuid=wuuid_str, workspace_pk=wpk)
        # A provisioning record with no encrypted credential would mint a descriptor whose AES token carries
        # an EMPTY Windows password — a dud that cannot authenticate. Fail closed here with a clear reason
        # rather than relying on decrypt_password to raise (which would surface as an opaque DA_ERROR).
        if not prov.password_enc:
            return _deny(DeliveryReason.IDENTITY_NO_CREDENTIAL, workspace_uuid=wuuid_str, workspace_pk=wpk)
        if not prov.runtime_root:
            return _deny(DeliveryReason.RUNTIME_MISSING, workspace_uuid=wuuid_str, workspace_pk=wpk)

        base_url = os.getenv("GUAC_BASE_URL", "").rstrip("/")
        secret_hex = os.getenv("GUAC_JSON_SECRET_KEY_HEX", "").strip()
        if not base_url or not secret_hex:
            return _deny(DeliveryReason.GUAC_UNCONFIGURED, workspace_uuid=wuuid_str, workspace_pk=wpk)

        descriptor = _build_signed_descriptor(
            workspace=workspace, prov=prov, node=node,
            base_url=base_url, secret_hex=secret_hex)

        return DeliveryAuthorization(
            authorized=True, reason=DeliveryReason.OK,
            workspace_uuid=wuuid_str, workspace_pk=wpk, descriptor=descriptor)

    except Exception as e:  # noqa: BLE001 — fail closed; log TYPE ONLY (never str(e): could echo config)
        logger.error("authorize_workspace_delivery failed closed: %s", type(e).__name__)
        return _deny(DeliveryReason.ERROR)


def _build_signed_descriptor(*, workspace, prov, node, base_url, secret_hex) -> dict:
    """Server-derive every connection value, mint the AES-signed RemoteApp token, and return ONLY the
    4-field safe descriptor. The Windows password is decrypted into a local, embedded into the token, and
    never returned/logged/persisted. Reuses the canonical guac primitives (``build_remoteapp_rdp_payload``
    / ``sign_and_encrypt_json`` / ``build_guac_data_url``) — the same envelope the SessionAdapter uses — so
    the delivery pipeline does not fork the token machinery."""
    from trading.crypto import decrypt_password

    from mt5.guac_json import (
        build_guac_data_url,
        build_remoteapp_rdp_payload,
        sign_and_encrypt_json,
    )

    # Stable per-workspace connection id → a reconnect deep-links to the SAME persistent Windows session.
    conn_id = f"mt5-workspace-{workspace.workspace_uuid}"
    windows_username = prov.windows_username
    remote_app_dir = rf"{prov.runtime_root}\terminal"

    payload = build_remoteapp_rdp_payload(
        username=f"ws-{windows_username}",
        windows_username=windows_username,
        windows_password=decrypt_password(prov.password_enc),
        host=node.rdp_host,
        remote_app=REMOTEAPP_ALIAS,
        remote_app_dir=remote_app_dir,
        remote_app_args=REMOTEAPP_ARGS,
        conn_id=conn_id,
    )  # ``host`` above is node.rdp_host (delivery transport), NEVER node.hostname (execution identity)
    data_b64 = sign_and_encrypt_json(payload, secret_hex=secret_hex)
    embed_url = build_guac_data_url(base_url=base_url, data_b64=data_b64, conn_id=conn_id)

    expiry = payload.get("expires")
    return {
        "transport_type": "rdp_remoteapp",
        "embed_url": embed_url,
        "session_token": "",  # always empty — everything rides inside the AES-encrypted token
        "expiry": int(expiry) if isinstance(expiry, int) else None,
    }
