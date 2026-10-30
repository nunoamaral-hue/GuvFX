"""broker_catalogue.host_catalogue — Django-side client for the ACCOUNT-INDEPENDENT catalogue host operations.

Signs ``STAGE_CATALOGUE_ARTEFACT`` / ``ATTEST_CATALOGUE_ARTEFACT`` requests (the sentinel ``account_id == 0``) with
the SAME authenticated transport the per-account executor uses (``host_protocol`` HMAC-signing + the
``/hosted/provision`` endpoint), and verifies the signed response. It is NOT a shell and carries no path/command on
the wire: staging bytes ride in the signed ``payload`` (bound via ``payload_digest``); the broker/label are validated
slugs and the host derives + confines every path.

DARK / fail-closed: ``resolve_*`` return ``None`` unless ``HOSTED_HOST_EXECUTOR_ENABLED`` is on AND the keyring /
key_id / base_url are configured — so ``activate_catalogue_version --attest-host`` keeps refusing (no transport) until
the host executor is armed, and the repository-only phase contacts no host.
"""
from __future__ import annotations

import base64
import logging
import re
import secrets

from hosted_workspace.host_executor import _epoch_now, _http_transport
from hosted_workspace.host_protocol import HostProtocolError, sign_hosted_request, verify_hosted_response

logger = logging.getLogger("guvfx.broker_catalogue")

# host_relpath produced by the builder: versions/<label>/<broker>/servers.dat (either separator).
_RELPATH_RE = re.compile(
    r"^versions[\\/](?P<label>[A-Za-z0-9._\-]{1,32})[\\/](?P<broker>[a-z0-9_]{1,32})[\\/]servers\.dat$")


class CatalogueHostClient:
    """One client per host transport config. Stateless across calls except its config + correlation id."""

    def __init__(self, *, keyring, key_id, base_url, transport=_http_transport, clock=_epoch_now,
                 correlation_id=None):
        self.keyring = keyring
        self.key_id = key_id
        self.base_url = base_url
        self.transport = transport
        self._clock = clock
        self.correlation_id = str(correlation_id or secrets.token_hex(8))

    def _send(self, operation, *, params, payload=None) -> dict:
        nonce = secrets.token_hex(16)
        try:
            req = sign_hosted_request(
                account_id=0, operation=operation, correlation_id=self.correlation_id,
                keyring=self.keyring, key_id=self.key_id, now=int(self._clock()),
                params=params or {}, nonce=nonce, payload=payload)
        except HostProtocolError as e:
            return {"ok": False, "reason": e.reason_code}
        try:
            resp = self.transport(self.base_url, req)
        except Exception:  # noqa: BLE001 — network/timeout/transport is AMBIGUOUS -> fail closed, sanitised
            logger.warning("catalogue host-client: transport failed op=%s corr=%s", operation, self.correlation_id)
            return {"ok": False, "reason": "host_unavailable"}
        try:
            result = verify_hosted_response(resp, correlation_id=self.correlation_id, nonce=nonce,
                                            keyring=self.keyring)
        except HostProtocolError as e:
            return {"ok": False, "reason": e.reason_code}
        if "ok" not in result:
            result["ok"] = False
        return result

    def stage(self, *, broker_id, label, expected_sha256, size_bytes, data: bytes) -> dict:
        """Write the approved ``data`` bytes to versions\\<label>\\<broker>\\servers.dat on the host (atomic,
        read-back verified). The bytes ride in the signed payload; the host re-verifies len + SHA before writing."""
        b64 = base64.b64encode(bytes(data or b"")).decode("ascii")
        return self._send("STAGE_CATALOGUE_ARTEFACT",
                          params={"broker_id": str(broker_id), "label": str(label),
                                  "expected_sha256": str(expected_sha256).lower(), "size_bytes": int(size_bytes)},
                          payload={"artefact_b64": b64})

    def attest(self, *, broker_id, label, expected_sha256="") -> dict:
        """Read-only attest of ONE staged artefact's SHA/size (ok=match when expected_sha256 is given)."""
        return self._send("ATTEST_CATALOGUE_ARTEFACT",
                          params={"broker_id": str(broker_id), "label": str(label),
                                  "expected_sha256": str(expected_sha256 or "").lower()})

    def attest_version(self, *, label) -> dict:
        """Read-only inventory of ALL staged broker artefacts under versions\\<label> (for the no-extra check)."""
        return self._send("ATTEST_CATALOGUE_ARTEFACT",
                          params={"broker_id": "", "label": str(label), "expected_sha256": ""})


def _host_executor_config():
    """Resolve the shared host-executor transport config (keyring/key_id/base_url) from settings/env. Returns
    ``None`` if the executor is DARK or any piece is missing (fail-closed), mirroring host_executor's resolver."""
    import json
    import os

    from django.conf import settings

    from hosted_workspace.flags import hosted_host_executor_enabled
    if not hosted_host_executor_enabled():
        return None
    raw = getattr(settings, "HOSTED_EXECUTOR_KEYRING", None) or os.getenv("HOSTED_EXECUTOR_KEYRING", "")
    key_id = (getattr(settings, "HOSTED_EXECUTOR_KEY_ID", None) or os.getenv("HOSTED_EXECUTOR_KEY_ID", "")).strip()
    base_url = (getattr(settings, "HOSTED_EXECUTOR_BASE_URL", None)
                or os.getenv("HOSTED_EXECUTOR_BASE_URL", "")).strip()
    try:
        keyring = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return None
    if not keyring or key_id not in keyring or not base_url:
        return None
    return {"keyring": keyring, "key_id": key_id, "base_url": base_url}


def resolve_catalogue_host_client():
    """A live ``CatalogueHostClient`` iff the host executor is armed + configured; else ``None`` (DARK)."""
    cfg = _host_executor_config()
    if cfg is None:
        return None
    return CatalogueHostClient(**cfg)


class CatalogueAttestAdapter:
    """Adapts ``CatalogueHostClient`` to ``activate_catalogue_version``'s attest-executor contract:
    ``attest_broker_artefact(host_relpath, expected_sha256)`` (per-artefact) + ``attest_version_inventory(label,
    brokers)`` (the no-missing/no-extra guard). Every host/transport failure is reported as ``ok: False``."""

    def __init__(self, client: CatalogueHostClient):
        self._c = client

    def attest_broker_artefact(self, host_relpath, expected_sha256) -> dict:
        m = _RELPATH_RE.match(str(host_relpath or "").replace("\\", "/"))
        if not m:
            return {"ok": False, "reason": "host_relpath_malformed"}
        res = self._c.attest(broker_id=m.group("broker"), label=m.group("label"),
                             expected_sha256=str(expected_sha256 or "").lower())
        return {"ok": bool(res.get("ok")), "verified_sha256": str(res.get("verified_sha256", ""))}

    def attest_version_inventory(self, label, brokers) -> dict:
        res = self._c.attest_version(label=label)
        if not res.get("ok"):
            return {"ok": False, "reason": str(res.get("reason", "attest_failed")), "extra": [], "missing": []}
        present = {str(b.get("broker_id")) for b in (res.get("brokers") or []) if b.get("present")}
        want = {str(b) for b in (brokers or [])}
        extra = sorted(present - want)
        missing = sorted(want - present)
        return {"ok": (not extra and not missing), "extra": extra, "missing": missing}


def resolve_catalogue_attest_executor():
    """The attest executor for ``activate_catalogue_version._attest_executor`` — ``None`` (fail-closed) when DARK."""
    client = resolve_catalogue_host_client()
    return CatalogueAttestAdapter(client) if client is not None else None
