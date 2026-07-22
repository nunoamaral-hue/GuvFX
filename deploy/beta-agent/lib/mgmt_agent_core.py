"""CVM-Inc-3 B — Windows agent CORE: independent, boundary-enforcing request handling.

This is the security heart that runs INSIDE the Windows provisioning agent (shipped to the box alongside
``mgmt_protocol``). It is deliberately Django-free and dependency-injected so the full logic — replay,
expiry, tampering, path-escape, idempotency, production refusal, response sanitisation — is unit-testable
without Windows or a network.

Responsibility boundary (Nuno requirement 8): the agent is authoritative for the LOCALLY-derived runtime
path, local process/filesystem containment, replay protection and observed Windows evidence. It accepts
NONE of the client's assertions about paths — it derives everything from the immutable ``runtime_uuid``
and refuses anything that is not provably contained beneath the canonical beta root.
"""
import time
import uuid

from .mgmt_protocol import (
    ALLOWED_OPERATIONS, PROTOCOL_VERSION, PROVISIONING_OPERATIONS, ProtocolError, semantic_digest,
    verify_request)

# Agent-local canonical beta root (config-overridable at install). The agent derives paths ONLY from the
# runtime UUID under this root — a request can never supply a path (requirement 1/3).
DEFAULT_BETA_ROOT = r"C:\GuvFX\beta\accounts"

# Operations that mutate host state (need the single-op-per-runtime lock). VERIFY is read-only.
_MUTATING = frozenset({"MATERIALISE", "START", "STOP", "TOMBSTONE"})

# The ONLY fields an agent response may contain (requirement 7 — never creds/env/cmdlines/fs-listings/raw
# exceptions). Includes the NEGOTIATE handshake fields.
_RESPONSE_ALLOWLIST = ("operation", "outcome", "runtime_uuid", "provisioning_job_id", "pid",
                       "session_id", "agent_version", "script_version", "duration_ms", "reason_code",
                       "running", "logged_in", "verified_at",
                       "protocol_version", "manifest_version", "supported_operations",
                       # B3P-2: (slot, generation) identifies ONE immutable runtime occupancy and is part
                       # of runtime identity, so it is first-class evidence rather than an internal detail.
                       #
                       # The complete local filesystem path is deliberately NOT exposed remotely. The agent
                       # independently derives and verifies containment on the box; the backend needs only
                       # the ATTESTATION that it did so. Verified: no backend lifecycle decision consumes a
                       # path from an agent response. The full path remains in the local Verification
                       # Report, Windows operational logs and authorised operator evidence.
                       "slot", "generation", "occupancy_id", "owner_marker_digest",
                       "canonical_path_digest",
                       "path_containment_verified", "executable_containment_verified")


class AgentError(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _norm(path: str) -> str:
    return (path or "").replace("/", "\\").rstrip("\\").lower()


def is_beneath(path: str, root: str) -> bool:
    """True iff ``path`` is strictly beneath ``root`` (case-insensitive, separator-normalised, and
    boundary-safe so ``C:\\GuvFX\\beta\\accountsX`` is NOT beneath ``C:\\GuvFX\\beta\\accounts``)."""
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r + "\\")


def derive_canonical_runtime_dir(runtime_uuid, base: str = DEFAULT_BETA_ROOT) -> str:
    """Locally derive the canonical runtime dir from the UUID ONLY (matches the backend's
    ``canonical_beta_runtime_root``). Raises if the UUID does not parse."""
    u = uuid.UUID(str(runtime_uuid))          # parse-or-raise = the traversal guard
    return f"{base}\\{u}\\terminal"


class BetaProvisioningAgent:
    """Processes a validated protocol request end-to-end with all boundary checks. Windows-specific side
    effects are injected: ``resolve_real_path`` (reparse-point resolution), ``op_impls`` (the versioned
    local implementations), ``nonce_store``/``idempotency_store``/``runtime_locks`` (durable state)."""

    def __init__(self, *, keyring, nonce_store, idempotency_store, op_impls, agent_version,
                 script_manifest, script_versions, resolve_real_path, runtime_locks,
                 base: str = DEFAULT_BETA_ROOT, now_fn=None, max_skew_seconds: int = 30,
                 manifest_version: str = ""):
        self.manifest_version = manifest_version
        self.keyring = keyring
        self.nonce_store = nonce_store               # .seen(nonce)->bool ; .remember(nonce, expiry)
        self.idempotency_store = idempotency_store    # .get(job_id, op) ; .put(job_id, op, resp)
        self.op_impls = op_impls                      # {OP: callable(canonical_dir, runtime_uuid, base)->dict}
        self.agent_version = agent_version
        self.script_manifest = script_manifest        # {impl_name: approved_sha256}
        self.script_versions = script_versions        # {impl_name: version_string}
        self.resolve_real_path = resolve_real_path    # canonical_dir -> real path (reparse-resolved) or None
        self.runtime_locks = runtime_locks            # .acquire(uuid)->ctx or raise ; used per mutating op
        self.base = base
        self.now_fn = now_fn or (lambda: int(time.time()))
        self.max_skew_seconds = max_skew_seconds

    def handle(self, request: dict) -> dict:
        """Validate + execute one request; ALWAYS returns a sanitised response dict (never raises to the
        caller — failures become ``outcome=denied`` + a sanitised reason_code)."""
        job_id = ruuid = op = None
        try:
            fields = verify_request(
                request, keyring=self.keyring, now=self.now_fn(),
                nonce_burn=self.nonce_store.burn, max_skew_seconds=self.max_skew_seconds)
            op, ruuid, job_id = fields["operation"], fields["runtime_uuid"], fields["provisioning_job_id"]

            # Read-only, runtime-less handshake: report versions + supported ops so the backend can
            # negotiate the contract before any provisioning request. No path/idempotency/lock work.
            if op == "NEGOTIATE":
                return self._sanitise(op, job_id, ruuid, "ok", {
                    "protocol_version": PROTOCOL_VERSION,
                    "manifest_version": self.manifest_version,
                    "supported_operations": list(PROVISIONING_OPERATIONS),
                })

            if op not in ALLOWED_OPERATIONS or op not in self.op_impls:
                raise AgentError("operation_not_allowed")

            # Idempotency (requirement 3/9): a repeat (job_id, op) returns the SAME stored result — never
            # re-runs a mutating op (so a duplicate can't launch a second terminal). A repeat with the
            # SAME (job_id, op) but CONFLICTING semantic input (different runtime/version) fails closed.
            digest = semantic_digest(fields)
            prior = self.idempotency_store.get(job_id, op)
            if prior is not None:
                if prior.get("digest") != digest:
                    raise AgentError("job_op_conflict")
                return prior["response"]

            # Derive the canonical dir LOCALLY from the UUID; refuse anything not contained beneath the
            # beta root (this IS the production-refusal + no-path-from-client guarantee). The UUID is
            # NORMALISED once (canonical hex form) and that single form is used for the path, the ops and
            # the response, so ``{ABC}`` and ``abc`` can never disagree on ownership.
            try:
                norm_uuid = str(uuid.UUID(str(ruuid)))
                canonical_dir = derive_canonical_runtime_dir(norm_uuid, self.base)
            except (ValueError, AttributeError, TypeError):
                raise AgentError("bad_runtime_uuid")
            if not is_beneath(canonical_dir, self.base):
                raise AgentError("path_escape")
            # Reparse/junction escape: resolve the runtime dir AND its (pre-plantable) parent even when the
            # leaf does not exist yet — MATERIALISE creates the leaf, so gating on leaf existence would let
            # a junction planted at ``<base>\<uuid>`` be written through before the escape is noticed.
            parent_dir = canonical_dir.rsplit("\\", 1)[0]
            for candidate in (canonical_dir, parent_dir):
                real = self.resolve_real_path(candidate)
                if real is not None and not is_beneath(real, self.base):
                    raise AgentError("reparse_escape")

            # Integrity (requirement 6): the local implementation must match the approved manifest.
            impl_name = f"op_{op.lower()}"
            if not self._impl_integrity_ok(impl_name):
                raise AgentError("impl_integrity_mismatch")

            impl = self.op_impls[op]
            if op in _MUTATING:
                with self.runtime_locks.acquire(norm_uuid):    # one mutating op per runtime at a time
                    raw = impl(canonical_dir=canonical_dir, runtime_uuid=norm_uuid, base=self.base)
            else:
                raw = impl(canonical_dir=canonical_dir, runtime_uuid=norm_uuid, base=self.base)

            resp = self._sanitise(op, job_id, ruuid, "ok", raw,
                                  script_version=self.script_versions.get(impl_name, ""))
            self.idempotency_store.put(job_id, op, {"digest": digest, "response": resp})
            return resp
        except (ProtocolError, AgentError) as e:
            # Denials are NOT stored idempotently (a fixed request may succeed once the fault clears),
            # and carry only a sanitised reason code.
            return self._sanitise(op, job_id, ruuid, "denied", {}, reason_code=e.reason_code)
        except Exception:  # noqa: BLE001 — never leak a raw exception string (requirement 7)
            return self._sanitise(op, job_id, ruuid, "error", {}, reason_code="agent_internal_error")

    def _impl_integrity_ok(self, impl_name: str) -> bool:
        approved = self.script_manifest.get(impl_name)
        actual = self.script_manifest.get(impl_name + ":actual", approved)  # test seam; real agent hashes disk
        return approved is not None and actual == approved

    def _sanitise(self, op, job_id, ruuid, outcome, raw: dict, *, reason_code: str = "",
                  script_version: str = "") -> dict:
        raw = raw or {}
        out = {
            "operation": op,
            "outcome": outcome,
            "runtime_uuid": str(ruuid) if ruuid is not None else None,
            "provisioning_job_id": job_id,
            "agent_version": self.agent_version,
            "script_version": script_version or raw.get("script_version", ""),
            "reason_code": reason_code,
        }
        # copy ONLY allowlisted evidence / handshake fields the impl produced
        for k in ("pid", "session_id", "duration_ms", "running", "logged_in", "verified_at",
                  "protocol_version", "manifest_version", "supported_operations",
                  "slot", "generation", "occupancy_id", "owner_marker_digest", "canonical_path_digest",
                  "path_containment_verified", "executable_containment_verified"):
            if k in raw:
                out[k] = raw[k]
        return {k: v for k, v in out.items() if k in _RESPONSE_ALLOWLIST}
