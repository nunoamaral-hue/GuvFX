"""Controlled, owner-scoped ERASURE for Withdrawal Intelligence (ADR-0054; GDPR/UK-GDPR Art 17).

POST-ADVERSARIAL-REVIEW state. This module is the GOVERNED, fail-closed CONTROL PLANE: the pure dry-run PLAN, the
two-person separation-of-duties lifecycle, the TOCTOU confirmation token, a comprehensive privileged-backend
READINESS check, the validated+concurrency-safe EXECUTE gate, and the idempotent file-deletion OUTBOX worker.

The actual destructive DELETION runs ONLY through a privileged database backend that is **provisioned separately
under Sponsor authorization** (ops/erasure/provision_erasure_privilege.sql): a dedicated NOLOGIN role
``guvfx_erasure`` owning a SECURITY DEFINER procedure, with table ownership moved to ``guvfx_schema_owner`` and
DELETE/TRUNCATE revoked from the application role. Why role-enforced, not a GUC (Finding 2): the
``EvidenceBlob``/``BrokerEvent`` BEFORE-DELETE triggers (migration 0017) permit a delete only when the session's
``current_user`` is ``guvfx_erasure`` — a forgeable session GUC was rejected. The app role is not a member of any
privileged role and cannot ``SET ROLE``, so EVERY ordinary DML DELETE (ORM/bulk/raw) is blocked; TRUNCATE/DDL
bypasses are closed by the provisioning ownership move + revocations.

``execute`` is **fail-closed in every unprovisioned/unconfigured environment** — web, workers, CI, local, and
production until a Sponsor-authorised provisioning — because the flag is off, the dedicated executor connection
(``erasure_executor``) is absent, and/or the full readiness probe fails. It performs real owner-scoped deletion
ONLY when all three hold, which in this programme means the disposable PostgreSQL-15 rehearsal
(``ops/erasure/rehearse_erasure.sh``; evidence in ``ops/erasure/REHEARSAL_EVIDENCE.md``) and, only after a
separate Sponsor GO, production. See ADR-0054 and docs/WV1_ERASURE_REWORK_PACKET.md.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.conf import settings
from django.db import connection, connections, transaction
from django.utils import timezone

from . import flags
from .models import (
    BrokerEmailIdentity, BrokerEvent, BrokerMessageObservation, ConnectedMailbox, DeletionTask, ErasureHold,
    ErasureRequest, EvidenceBlob, TradingAccountBrokerContact, Withdrawal,
)

logger = logging.getLogger("guvfx.broker_intelligence.erasure")

# Privileged identities the provisioning script (ops/erasure/provision_erasure_privilege.sql) creates. The
# readiness check re-verifies the whole boundary against these before any execute could proceed.
_PRIVILEGED_ROLE = "guvfx_erasure"          # NOLOGIN; owns the SECURITY DEFINER proc; the trigger identity
_EXECUTOR_ROLE = "guvfx_erasure_exec"       # NOLOGIN GROUP holding the sole EXECUTE grant on the proc; individual
                                            # short-lived operator LOGIN roles join it for one governed operation,
                                            # and the proc authenticates the human operator via session_user
_SCHEMA_OWNER_ROLE = "guvfx_schema_owner"   # NOLOGIN; owns the evidence tables + ordinary trigger functions
_MIGRATE_ROLE = "guvfx_migrate"             # LOGIN; member of the schema owner; performs schema migrations
_EXECUTOR_ALIAS = "erasure_executor"        # Django DATABASES alias for the executor login (absent => fail-closed)
_PROC_NAME = "bi_governed_erase_owner"
_PROC_SCHEMA = "public"
_PROC_SIG = "bigint, text"                  # (request_id, confirmation_token) — executor is resolved from session
_PROC_VERSION = "ADR-0054/erasure-proc/v2"  # version marker stored as the function COMMENT; readiness pins it
_PROC_SEARCH_PATH = "pg_catalog, public"    # exact pinned search_path (not merely "some setting present")
_EVIDENCE_TABLES = ("broker_intelligence_evidenceblob", "broker_intelligence_brokerevent")
_GRANT_TABLE = "broker_intelligence_erasureexecutiongrant"   # executor trust anchor — must be off the app role
_EVIDENCE_SEQUENCES = ("broker_intelligence_evidenceblob_id_seq", "broker_intelligence_brokerevent_id_seq")
_TRIGGER_FUNCTIONS = ("bi_evidenceblob_block_delete", "bi_brokerevent_block_delete")
_DELETE_TRIGGERS = ("bi_evidenceblob_no_delete", "bi_brokerevent_no_delete")
_REQUIRED_MIGRATIONS = (                     # exact names — EVERY safety-critical migration of the accepted design
    "0017_erasurehold_erasurerequest",          # role-enforced BEFORE-DELETE triggers
    "0018_erasure_lifecycle_and_deletion_outbox",  # two-person lifecycle + durable outbox
    "0019_erasurerequest_consistency_checks",   # CHECK constraints
    "0020_audit_anonymisation_carveout",        # audit-anonymisation UPDATE carve-out
    "0021_erasure_execution_grant",             # non-forgeable single-use executor grant (P0)
    "0022_connectedmailbox_generation",         # mailbox-generation stale-ingestion protection (P0)
)
_ADVISORY_NS = 0x6775                        # fixed namespace key for pg_advisory_xact_lock(ns, owner_id)
_MAX_DELETION_ATTEMPTS = 5                   # a file-deletion task that fails this many times is dead-lettered
# Allowed data-subject identity-verification method CATEGORIES (never ID documents). A verification record must
# carry one of these (Finding A): a non-empty, recognised code — not a free-text or empty string.
_ALLOWED_VERIFICATION_METHODS = frozenset({
    "signed-request", "video-call", "gov-id-checked", "account-control-proof", "support-ticket-verified",
})


def erasure_executor_alias():
    """Return the configured executor DATABASES alias name, or None if it is not configured (fail-closed).

    The alias exists ONLY when the dedicated executor credential is present in THIS process's environment
    (settings builds it from ERASURE_EXECUTOR_DB_*). Web/worker/CI/local runtimes must NOT carry that credential,
    so this returns None there and governed execution refuses. Never falls back to the application connection."""
    from django.conf import settings
    return _EXECUTOR_ALIAS if _EXECUTOR_ALIAS in settings.DATABASES else None


class ErasureRefused(Exception):
    """Raised (fail-closed) when a governed erasure must not proceed. The message is sanitised (no content)."""


def _ids(qs) -> list[int]:
    return sorted(qs.values_list("id", flat=True))


def _ref_digest(value: str) -> str:
    """Stable, NON-secret digest of a credential reference. The raw ``credential_ref`` is a filesystem pointer to
    an encrypted token; we never put it in the plan/token basis in the clear — only its sha256, so the digest
    still changes if the subject re-authenticates to a new credential file (Finding 5: credential-ref drift)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def plan_owner_erasure(owner_id: int) -> dict:
    """PURE dry-run: enumerate exactly what an erasure of ``owner_id`` would delete/retain. NO mutation.
    The candidate evidence set is the UNION of blobs referenced by this owner's BrokerEvents AND this owner's
    BrokerMessageObservations (Finding 5a). A blob is classified DELETE only if NO reference remains once this
    owner's events + observations are gone (NULL-safe: other = total - this_owner). The returned plan carries a
    deterministic confirmation ``token`` over ALL mutable facts, including stable credential-ref digests."""
    wd = _ids(Withdrawal.objects.filter(owner_id=owner_id))
    ev = _ids(BrokerEvent.objects.filter(owner_id=owner_id))
    obs = _ids(BrokerMessageObservation.objects.filter(owner_id=owner_id))
    mbx = _ids(ConnectedMailbox.objects.filter(user_id=owner_id))
    idn = _ids(BrokerEmailIdentity.objects.filter(user_id=owner_id))
    # The subject's account<->broker-identity bindings (PROTECT the identities; are the subject's personal data).
    con = _ids(TradingAccountBrokerContact.objects.filter(broker_email_identity__user_id=owner_id))
    cred_refs = sorted(ConnectedMailbox.objects.filter(user_id=owner_id)
                       .exclude(credential_ref="").values_list("credential_ref", flat=True))
    cred_ref_digests = sorted(_ref_digest(r) for r in cred_refs)

    candidate_blob_ids = sorted(set(
        list(BrokerEvent.objects.filter(owner_id=owner_id, evidence__isnull=False)
             .values_list("evidence_id", flat=True))
        + list(BrokerMessageObservation.objects.filter(owner_id=owner_id, evidence__isnull=False)
               .values_list("evidence_id", flat=True))
    ))
    to_delete, to_retain = [], []
    for bid in candidate_blob_ids:
        total_ev = BrokerEvent.objects.filter(evidence_id=bid).count()
        mine_ev = BrokerEvent.objects.filter(evidence_id=bid, owner_id=owner_id).count()
        total_obs = BrokerMessageObservation.objects.filter(evidence_id=bid).count()
        mine_obs = BrokerMessageObservation.objects.filter(evidence_id=bid, owner_id=owner_id).count()
        other_refs = (total_ev - mine_ev) + (total_obs - mine_obs)
        (to_delete if other_refs == 0 else to_retain).append(bid)

    plan = {
        "subject_user_id": owner_id,
        "withdrawals": wd,
        "broker_events": ev,
        "observations": obs,
        "connected_mailboxes": mbx,
        "credential_refs_count": len(cred_refs),
        "credential_ref_digests": cred_ref_digests,
        "identities": idn,
        "contacts": con,
        "evidence_blobs_delete": to_delete,
        "evidence_blobs_retain_shared": to_retain,
    }
    plan["token"] = _plan_token(plan)
    return plan


def _plan_token(plan: dict) -> str:
    """Deterministic digest over the FULL owner-scoped mutable fact set — the second-confirmation token + TOCTOU
    guard. Includes credential-ref digests so a re-auth (new credential file) between plan and execute invalidates
    the token even when every id set is unchanged (Finding 5b)."""
    basis = json.dumps({k: plan[k] for k in (
        "subject_user_id", "withdrawals", "broker_events", "observations", "connected_mailboxes",
        "credential_ref_digests", "identities", "contacts", "evidence_blobs_delete",
        "evidence_blobs_retain_shared")},
        sort_keys=True)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def privileged_backend_readiness(using: str = "default") -> tuple[bool, list[str]]:
    """Comprehensive, fail-closed readiness probe for the separately-provisioned privileged backend (Finding D).
    Returns ``(ready, reasons)``; ``ready`` is True ONLY when EVERY invariant holds. Any missing object, wrong
    schema/signature/owner/SECURITY DEFINER/search_path/version, any unexpected or PUBLIC EXECUTE grantee, any
    app-role membership/privilege, a missing migration, a missing/disabled trigger, an ambiguous overload, or any
    probe error => NOT READY with a sanitised reason. Every query is guarded so it cannot raise on an
    unprovisioned database. ``using`` selects the connection alias (the executor alias at execute time)."""
    reasons: list[str] = []
    # The APPLICATION role is the configured default-connection user, regardless of which alias we probe over.
    app_role = settings.DATABASES["default"]["USER"]
    try:
        with connections[using].cursor() as cur:
            def _exists(sql, params):
                cur.execute(sql, params)
                return cur.fetchone() is not None

            # (0) When probing over the executor alias, confirm the connected login is a MEMBER of the
            # guvfx_erasure_exec group (each operator connects as a distinct short-lived login that is a member;
            # the group itself is NOLOGIN). Belt-and-suspenders against a misconfigured ERASURE_EXECUTOR_DB_USER.
            if using == _EXECUTOR_ALIAS:
                cur.execute("SELECT session_user")
                su = cur.fetchone()[0]
                cur.execute("SELECT pg_has_role(%s, %s, 'USAGE')", [su, _EXECUTOR_ROLE])
                if not cur.fetchone()[0]:
                    reasons.append(f"executor session '{su}' is not a member of '{_EXECUTOR_ROLE}'")

            # (1) Required roles exist.
            for role in (_PRIVILEGED_ROLE, _EXECUTOR_ROLE, _SCHEMA_OWNER_ROLE, _MIGRATE_ROLE):
                if not _exists("SELECT 1 FROM pg_roles WHERE rolname = %s", [role]):
                    reasons.append(f"role '{role}' absent")
            roles_ok = not reasons

            # (1b) Schema 'public' must be owned by the schema owner (not the app role) and the app role must not
            # hold CREATE on it — closes the `DROP SCHEMA public CASCADE` owner-level destruction vector (HIGH).
            cur.execute("SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'")
            srow = cur.fetchone()
            if srow is None or srow[0] == app_role:
                reasons.append("schema 'public' must not be owned by the application role")
            elif srow[0] != _SCHEMA_OWNER_ROLE:
                reasons.append(f"schema 'public' not owned by '{_SCHEMA_OWNER_ROLE}' (owner '{srow[0]}')")
            cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", [app_role])
            if cur.fetchone()[0]:
                reasons.append("application role must not have CREATE on schema 'public'")

            # (2) EXACTLY ONE procedure of this name in the pinned schema (ambiguous overload => NOT READY).
            cur.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE p.proname = %s AND n.nspname = %s", [_PROC_NAME, _PROC_SCHEMA])
            n_proc = cur.fetchone()[0]
            proc_oid = None
            if n_proc == 0:
                reasons.append(f"procedure '{_PROC_SCHEMA}.{_PROC_NAME}' absent")
            elif n_proc > 1:
                reasons.append(f"{n_proc} functions named '{_PROC_NAME}' in '{_PROC_SCHEMA}' — ambiguous, NOT READY")
            else:
                cur.execute(
                    "SELECT p.oid, p.prosecdef, pg_get_userbyid(p.proowner), p.proconfig, "
                    "(SELECT string_agg(format_type(t, NULL), ', ') FROM unnest(p.proargtypes) AS t), "
                    "obj_description(p.oid, 'pg_proc') "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE p.proname = %s AND n.nspname = %s", [_PROC_NAME, _PROC_SCHEMA])
                proc_oid, prosecdef, proc_owner, proconfig, proc_args, proc_desc = cur.fetchone()
                if (proc_args or "").replace(" ", "") != _PROC_SIG.replace(" ", ""):
                    reasons.append(f"procedure signature mismatch (got '{proc_args}')")
                if not prosecdef:
                    reasons.append("procedure is not SECURITY DEFINER")
                if proc_owner != _PRIVILEGED_ROLE:
                    reasons.append(f"procedure not owned by '{_PRIVILEGED_ROLE}' (owner '{proc_owner}')")
                sp = next((c.split("=", 1)[1] for c in (proconfig or []) if c.lower().startswith("search_path=")), None)
                if sp is None:
                    reasons.append("procedure search_path not pinned")
                elif sp.strip() != _PROC_SEARCH_PATH:
                    reasons.append(f"procedure search_path is not exactly '{_PROC_SEARCH_PATH}' (got '{sp}')")
                if not proc_desc or _PROC_VERSION not in proc_desc:
                    reasons.append(f"procedure version marker missing/mismatched (want '{_PROC_VERSION}')")

            # (3) EXECUTE ACL: executor only; PUBLIC and the app role NO; no unexpected grantees; ACL not default.
            if proc_oid is not None:
                cur.execute("SELECT has_function_privilege('public', %s, 'EXECUTE')", [proc_oid])
                if cur.fetchone()[0]:
                    reasons.append("PUBLIC must not have EXECUTE on the procedure")
                cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", [app_role, proc_oid])
                if cur.fetchone()[0]:
                    reasons.append("application role must not have EXECUTE on the procedure")
                if roles_ok:
                    cur.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE')", [_EXECUTOR_ROLE, proc_oid])
                    if not cur.fetchone()[0]:
                        reasons.append(f"'{_EXECUTOR_ROLE}' lacks EXECUTE on the procedure")
                cur.execute("SELECT proacl IS NULL FROM pg_proc WHERE oid = %s", [proc_oid])
                if cur.fetchone()[0]:
                    reasons.append("procedure ACL is default (PUBLIC retains EXECUTE); REVOKE not applied")
                else:
                    cur.execute(
                        "SELECT coalesce(array_agg(grantee::regrole::text), ARRAY[]::text[]) "
                        "FROM aclexplode((SELECT proacl FROM pg_proc WHERE oid = %s)) "
                        "WHERE privilege_type = 'EXECUTE' AND grantee <> 0 "
                        "AND grantee <> (SELECT proowner FROM pg_proc WHERE oid = %s)", [proc_oid, proc_oid])
                    grantees = cur.fetchone()[0] or []
                    unexpected = sorted({g for g in grantees if g != _EXECUTOR_ROLE})
                    if unexpected:
                        reasons.append(f"unexpected EXECUTE grantees on the procedure: {unexpected}")

            # (4) Evidence tables + sequences owned by the schema owner (closes TRUNCATE/DDL on the app role).
            for tbl in _EVIDENCE_TABLES:
                cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = %s", [tbl])
                r = cur.fetchone()
                if r is None:
                    reasons.append(f"evidence table '{tbl}' absent")
                elif r[0] != _SCHEMA_OWNER_ROLE:
                    reasons.append(f"table '{tbl}' not owned by '{_SCHEMA_OWNER_ROLE}' (owner '{r[0]}')")
            for seq in _EVIDENCE_SEQUENCES:
                cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = %s AND relkind = 'S'", [seq])
                r = cur.fetchone()
                if r and r[0] != _SCHEMA_OWNER_ROLE:
                    reasons.append(f"sequence '{seq}' not owned by '{_SCHEMA_OWNER_ROLE}' (owner '{r[0]}')")

            # (5) Trigger FUNCTIONS owned by the schema owner (app role cannot CREATE OR REPLACE them).
            for fn in _TRIGGER_FUNCTIONS:
                cur.execute("SELECT pg_get_userbyid(proowner) FROM pg_proc WHERE proname = %s", [fn])
                r = cur.fetchone()
                if r is None:
                    reasons.append(f"trigger function '{fn}' absent")
                elif r[0] != _SCHEMA_OWNER_ROLE:
                    reasons.append(f"trigger function '{fn}' not owned by '{_SCHEMA_OWNER_ROLE}' (owner '{r[0]}')")

            # (6) Required DELETE-blocker triggers installed AND firing in an origin session (tgenabled O or A;
            # 'D'=disabled and 'R'=replica-only both fail to fire here).
            for trg in _DELETE_TRIGGERS:
                cur.execute("SELECT tgenabled FROM pg_trigger WHERE tgname = %s AND NOT tgisinternal", [trg])
                r = cur.fetchone()
                if r is None:
                    reasons.append(f"trigger '{trg}' not installed")
                elif r[0] not in ("O", "A"):
                    reasons.append(f"trigger '{trg}' does not fire in an origin session (tgenabled='{r[0]}')")

            # (7) App role: LEAST PRIVILEGE. The ordinary runtime identity must carry NO elevated cluster
            # attributes and must NOT be a member of — or able to SET ROLE to — any privileged role. A SUPERUSER
            # (or BYPASSRLS / CREATEROLE / CREATEDB) application role would bypass the REVOKEs AND the
            # BEFORE-DELETE immutability triggers entirely, making the whole erasure boundary cosmetic (P0 — the
            # production superuser blocker). Each forbidden attribute/membership is an INDEPENDENT fail-closed
            # reason so a probe pinpoints exactly which precondition is unmet.
            cur.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
                        "FROM pg_roles WHERE rolname = %s", [app_role])
            attr = cur.fetchone()
            if attr is None:
                reasons.append(f"application role '{app_role}' not found")
            else:
                if attr[0]:
                    reasons.append(f"application role '{app_role}' must be NOSUPERUSER "
                                   "(a superuser bypasses the REVOKEs and the immutability triggers)")
                if attr[1]:
                    reasons.append(f"application role '{app_role}' must be NOCREATEROLE "
                                   "(could grant itself into a privileged role)")
                if attr[2]:
                    reasons.append(f"application role '{app_role}' must be NOCREATEDB")
                if attr[3]:
                    reasons.append(f"application role '{app_role}' must be NOBYPASSRLS")
            # Not a member of, and unable to SET ROLE to, ANY privileged role (incl. the migration role).
            if roles_ok:
                for role in (_PRIVILEGED_ROLE, _EXECUTOR_ROLE, _SCHEMA_OWNER_ROLE, _MIGRATE_ROLE):
                    cur.execute("SELECT pg_has_role(%s, %s, 'USAGE') OR pg_has_role(%s, %s, 'MEMBER')",
                                [app_role, role, app_role, role])
                    if cur.fetchone()[0]:
                        reasons.append(f"application role '{app_role}' must not be a member of or able to assume '{role}'")
            for tbl in _EVIDENCE_TABLES:
                for priv in ("DELETE", "TRUNCATE"):
                    cur.execute("SELECT has_table_privilege(%s, %s, %s)", [app_role, tbl, priv])
                    if cur.fetchone()[0]:
                        reasons.append(f"application role '{app_role}' must not have {priv} on '{tbl}'")

            # (7b) Execution-grant table (the P0 executor trust anchor): owned by the schema owner, the app role
            # has NO privilege on it (cannot forge a grant), PUBLIC has none, and guvfx_erasure may only
            # SELECT/UPDATE (consume) — never INSERT a grant. A later migration/GRANT that re-opens app access
            # must flip readiness to NOT READY.
            cur.execute("SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = %s", [_GRANT_TABLE])
            gr = cur.fetchone()
            if gr is None:
                reasons.append(f"execution-grant table '{_GRANT_TABLE}' absent")
            elif gr[0] != _SCHEMA_OWNER_ROLE:
                reasons.append(f"execution-grant table not owned by '{_SCHEMA_OWNER_ROLE}' (owner '{gr[0]}')")
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                cur.execute("SELECT has_table_privilege(%s, %s, %s)", [app_role, _GRANT_TABLE, priv])
                if cur.fetchone()[0]:
                    reasons.append(f"application role must not have {priv} on the execution-grant table")
            cur.execute("SELECT has_table_privilege('public', %s, 'SELECT')", [_GRANT_TABLE])
            if cur.fetchone()[0]:
                reasons.append("PUBLIC must not have SELECT on the execution-grant table")
            if roles_ok:
                cur.execute("SELECT has_table_privilege(%s, %s, 'INSERT')", [_PRIVILEGED_ROLE, _GRANT_TABLE])
                if cur.fetchone()[0]:
                    reasons.append("guvfx_erasure must not have INSERT on the execution-grant table (consume-only)")

            # (8) Required migrations applied (exact names — a differently-named 00NN_* must not false-pass).
            for mig in _REQUIRED_MIGRATIONS:
                if not _exists("SELECT 1 FROM django_migrations WHERE app = 'broker_intelligence' AND name = %s",
                               [mig]):
                    reasons.append(f"required migration '{mig}' not applied")
    except Exception:  # noqa: BLE001 — any probe error is treated as NOT READY (fail-closed), never as ready.
        logger.warning("privileged backend readiness probe errored; treating as NOT READY", exc_info=True)
        return False, ["readiness probe error (treated as not ready)"]
    return (len(reasons) == 0), reasons


def privileged_backend_ready(using: str = "default") -> bool:
    """True iff the separately-provisioned privileged erasure backend satisfies EVERY readiness invariant
    (Finding D). Until then, execute fails closed. Provisioning is superuser + Sponsor-gated."""
    ready, _ = privileged_backend_readiness(using=using)
    return ready


@transaction.atomic
def verify_identity(request: ErasureRequest, verifier, method: str) -> ErasureRequest:
    """Record out-of-band data-subject identity verification (lifecycle step 1; Finding 7). Stores the verifier,
    timestamp and a METHOD CATEGORY only (never ID documents). Separate invocation from approve/execute."""
    req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
    # Finding (round-3 adversarial): verification is only meaningful before approval; refuse to overwrite the
    # recorded verifier once a plan has been approved or executed.
    if req.status not in (ErasureRequest.Status.REQUESTED, ErasureRequest.Status.PLANNED):
        raise ErasureRefused(f"cannot verify identity once request status is '{req.status}'")
    verifier_id = getattr(verifier, "pk", None)
    if verifier_id is None:
        raise ErasureRefused("a verifier identity is required")
    if (method or "") not in _ALLOWED_VERIFICATION_METHODS:
        raise ErasureRefused("verification method must be a recognised non-empty category code")
    if req.requested_by_id is None:
        raise ErasureRefused("cannot verify: no requester recorded (two-person cannot be enforced)")
    if verifier_id == req.requested_by_id:
        raise ErasureRefused("two-person rule: the verifier must differ from the requester")
    req.identity_verified = True
    req.verified_by = verifier
    req.verified_at = timezone.now()
    req.verification_method = method[:60]
    req.save(update_fields=["identity_verified", "verified_by", "verified_at", "verification_method"])
    logger.info("erasure request %s identity verified (method=%s)", req.id, req.verification_method)
    return req


@transaction.atomic
def approve_request(request: ErasureRequest, approver, assigned_executor,
                    *, approval_ttl_minutes: int = 60) -> ErasureRequest:
    """Independent two-person APPROVAL (lifecycle step 2). Requires a produced plan (PLANNED), verified identity,
    an approver DIFFERENT from the requester AND verifier, and an ASSIGNED EXECUTOR different from the approver.
    The assigned executor designates WHO may later execute; at execution the governed procedure resolves the
    DB-authenticated session to an operator and requires it to equal this assigned executor (P0). Sets an approval
    expiry; a separate invocation from execute. Fail-closed via ErasureRefused on any violation."""
    req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
    if req.status != ErasureRequest.Status.PLANNED:
        raise ErasureRefused(f"request status '{req.status}' is not approvable (need a produced plan)")
    if not req.identity_verified:
        raise ErasureRefused("cannot approve: data-subject identity not verified")
    approver_id = getattr(approver, "pk", None)
    executor_id = getattr(assigned_executor, "pk", None)
    if approver_id is None:
        raise ErasureRefused("an approver identity is required")
    if executor_id is None:
        raise ErasureRefused("an assigned executor identity is required")
    if req.requested_by_id is None:
        # Finding (round-3 adversarial HIGH): without a recorded requester the two-person rule cannot be
        # enforced, so approval must fail closed rather than vacuously pass.
        raise ErasureRefused("two-person rule: no requester recorded; cannot enforce separation of duties")
    if req.verified_by_id is None:
        raise ErasureRefused("cannot approve: no verifier recorded")
    if approver_id == req.requested_by_id:
        raise ErasureRefused("two-person rule: the approver must differ from the requester")
    if approver_id == req.verified_by_id:
        raise ErasureRefused("two-person rule: the approver must differ from the verifier")
    if executor_id == approver_id:
        raise ErasureRefused("two-person rule: the assigned executor must differ from the approver")
    now = timezone.now()
    req.approved_by = approver
    req.assigned_executor = assigned_executor
    req.approved_at = now
    req.approval_revoked = False
    req.approval_expires_at = now + timezone.timedelta(minutes=max(1, approval_ttl_minutes))
    req.status = ErasureRequest.Status.APPROVED
    req.save(update_fields=["approved_by", "assigned_executor", "approved_at", "approval_revoked",
                            "approval_expires_at", "status"])
    logger.info("erasure request %s APPROVED (two-person; executor assigned)", req.id)
    return req


def assert_erasure_allowed(request: ErasureRequest) -> int:
    """Fail-closed gate. Returns the resolved subject owner id, or raises ErasureRefused. Checks: flag ON; request
    APPROVED; identity verified; a valid, non-revoked, non-expired two-person approval (approver != requester);
    an owner id is present; and no ACTIVE hold (Findings 6/7)."""
    if not flags.broker_intelligence_erasure_enabled():
        raise ErasureRefused("controlled erasure is disabled (BROKER_INTELLIGENCE_ERASURE_ENABLED off)")
    if request.status != ErasureRequest.Status.APPROVED:
        raise ErasureRefused(f"request status '{request.status}' is not executable (needs two-person approval)")
    # Finding A — a COMPLETE, internally-consistent verification record (not just the boolean).
    if not request.identity_verified:
        raise ErasureRefused("data-subject identity not verified for this request")
    if request.verified_by_id is None or request.verified_at is None:
        raise ErasureRefused("incomplete verification record (missing verifier or timestamp)")
    if (request.verification_method or "") not in _ALLOWED_VERIFICATION_METHODS:
        raise ErasureRefused("incomplete verification record (missing/invalid method category)")
    if request.approved_by_id is None or request.approved_at is None:
        raise ErasureRefused("request has no recorded approval")
    if request.approval_revoked:
        raise ErasureRefused("the approval for this request has been revoked")
    if request.approval_expires_at is not None and request.approval_expires_at < timezone.now():
        raise ErasureRefused("the approval for this request has expired")
    # Finding B / P0 — separation of duties: requester/verifier/approver recorded + pairwise-distinct, and an
    # assigned executor distinct from the approver. The ACTUAL executor identity is resolved + re-checked in the
    # governed procedure from the DB-authenticated session (never a caller argument); this pre-check only asserts
    # the DB-recorded designation so a request with no/invalid assigned executor cannot reach the backend.
    if request.requested_by_id is None:
        raise ErasureRefused("two-person rule: no requester recorded; cannot enforce separation of duties")
    if request.verified_by_id == request.requested_by_id:
        raise ErasureRefused("two-person rule violated: verifier equals requester")
    if request.approved_by_id == request.requested_by_id:
        raise ErasureRefused("two-person rule violated: approver equals requester")
    if request.approved_by_id == request.verified_by_id:
        raise ErasureRefused("two-person rule violated: approver equals verifier")
    if request.assigned_executor_id is None:
        raise ErasureRefused("no assigned executor on the request")
    if request.assigned_executor_id == request.approved_by_id:
        raise ErasureRefused("two-person rule violated: assigned executor equals approver")
    owner_id = request.subject_user_id_snapshot
    if not owner_id:
        raise ErasureRefused("request has no subject owner id")
    if ErasureHold.objects.filter(subject_user_id_snapshot=owner_id, active=True).exists():
        raise ErasureRefused("an active legal/security hold blocks erasure for this subject")
    return owner_id


def execute_owner_erasure(request: ErasureRequest, confirm_token: str) -> dict:
    """GOVERNED erasure entry point. Fail-closed gates before anything destructive can happen:
      1. the flag + the full lifecycle gate (``assert_erasure_allowed``), incl. an assigned executor != approver;
      2. the dedicated executor connection must be configured (``erasure_executor_alias``) — absent in web/worker/
         CI/local, so execution refuses there. The destructive proc is invoked ONLY over this connection, whose
         login is a MEMBER of the NOLOGIN ``guvfx_erasure_exec`` group; the ordinary app connection has no EXECUTE;
      3. the privileged backend must pass the FULL readiness probe, checked over the executor connection.
    Only then is ``bi_governed_erase_owner(request_id, token)`` invoked. **The executor identity is NOT passed**
    (P0 non-forgeability): the procedure (as ``guvfx_erasure``) resolves the human operator from the DB-authenticated
    ``session_user`` via a single-use ``ErasureExecutionGrant``, requires it to equal the request's assigned
    executor and differ from the approver, consumes the grant, re-checks every gate + TOCTOU under its own row +
    advisory lock, performs the owner-scoped deletes, enqueues the outbox, anonymises audit PII, and records the
    terminal state — all in ONE transaction. Returns the procedure's JSON outcome (counts only; no content).

    In normal environments the executor connection is unconfigured and/or the backend is unprovisioned, so this
    refuses and nothing is deleted. Real deletion runs only where all gates hold: the authorised rehearsal, and
    (only after a separate Sponsor GO) production."""
    assert_erasure_allowed(request)   # fast pre-check (flag/status/identity/approval/SoD/assigned-exec/hold)
    alias = erasure_executor_alias()
    if alias is None:
        raise ErasureRefused(
            "erasure executor connection not configured (fail-closed): the dedicated per-operator executor "
            "credential is not present in this environment, so governed deletion cannot run. It is injected only "
            "into the manually-invoked governed job, never the web/worker runtime.")
    if not confirm_token:
        raise ErasureRefused("a confirmation token is required")
    ready, reasons = privileged_backend_readiness(using=alias)
    if not ready:
        raise ErasureRefused(
            "privileged erasure backend not ready (ADR-0054): the guvfx_erasure / guvfx_erasure_exec / "
            "guvfx_schema_owner roles + the governed SECURITY DEFINER procedure must be provisioned by a "
            "superuser under Sponsor authorization (ops/erasure/provision_erasure_privilege.sql) before any "
            f"destructive erasure can run. Fail-closed. Unmet: {'; '.join(reasons[:4])}")
    # Invoke the governed procedure over the EXECUTOR connection only. Autocommit: the function body is one
    # atomic transaction; on return its deletes + outbox writes + audit anonymisation are committed together.
    exec_conn = connections[alias]
    try:
        with exec_conn.cursor() as cur:
            cur.execute("SELECT bi_governed_erase_owner(%s, %s)", [request.pk, confirm_token])
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — a proc-side RAISE (gate/TOCTOU/grant failure) is a sanitised refusal.
        raise ErasureRefused(f"governed erasure procedure refused or failed: {str(exc).splitlines()[0][:200]}")
    outcome = row[0] if row else None
    if isinstance(outcome, str):
        outcome = json.loads(outcome)
    logger.info("governed erasure executed for request %s", request.pk)
    return outcome or {}


def process_deletion_tasks(limit: int = 100) -> dict:
    """Idempotent FILE-deletion OUTBOX worker (ADR-0054 Findings 3/4). Runs AFTER the governed transaction has
    committed its row deletes + enqueued tasks. Drains PENDING/FAILED ``DeletionTask`` rows, removing each target
    file: a missing/already-removed file is SUCCESS (idempotent); a removal error marks the task FAILED (retryable)
    until ``_MAX_DELETION_ATTEMPTS``, after which it is DEAD-lettered (no retry storm). An EXECUTED request whose
    outbox is fully drained is advanced to COMPLETED; if any task dead-lettered it is moved to FAILED instead
    (a real file could not be removed, so erasure is incomplete — operator intervention). A crash after commit
    leaves a durable PENDING task (not an orphan file). ``SELECT ... FOR UPDATE SKIP LOCKED`` lets concurrent
    workers process disjoint tasks; the request row is locked BEFORE the drained-check so the last committer
    observes the true task state (no completion race). In this build execute is fail-closed → normally a no-op."""
    from .credential_store import delete_token
    from .evidence import EvidenceStore

    store = EvidenceStore()
    processed = {"completed": 0, "failed": 0, "dead": 0}
    touched_request_ids: set[int] = set()

    with transaction.atomic():
        tasks = list(DeletionTask.objects
                     .select_for_update(skip_locked=True)
                     .filter(status__in=[DeletionTask.Status.PENDING, DeletionTask.Status.FAILED])
                     .order_by("id")[:limit])
        for task in tasks:
            task.attempts += 1
            try:
                if task.kind == DeletionTask.Kind.EVIDENCE_FILE:
                    # Re-ingestion race guard (adversarial-review LOW): if a LIVE blob now re-uses this exact
                    # content (content-addressed store, cross-owner), do NOT unlink — that file backs a live row.
                    if EvidenceBlob.objects.filter(sha256=task.target_ref).exists():
                        removed = None   # retained: still referenced
                    else:
                        removed = store.delete_file_by_sha256(task.target_ref)   # missing => False, never raises
                elif task.kind == DeletionTask.Kind.CREDENTIAL_FILE:
                    removed = delete_token(task.target_ref)                  # missing => False (bad ref => raises)
                else:
                    raise ValueError(f"unknown deletion task kind: {task.kind}")
                task.status = DeletionTask.Status.COMPLETED
                task.completed_at = timezone.now()
                task.last_error = ("content re-referenced by a live blob; file retained" if removed is None
                                   else "" if removed else "file already absent at processing time")
                processed["completed"] += 1
            except Exception as exc:  # noqa: BLE001 — a failed unlink is retryable, never fatal to the worker.
                if task.attempts >= _MAX_DELETION_ATTEMPTS:
                    task.status = DeletionTask.Status.DEAD
                    processed["dead"] += 1
                else:
                    task.status = DeletionTask.Status.FAILED
                    processed["failed"] += 1
                task.last_error = str(exc)[:300]
                logger.warning("deletion task %s -> %s (attempt %s): %s",
                               task.id, task.status, task.attempts, task.last_error)
            task.save(update_fields=["status", "attempts", "completed_at", "last_error"])
            touched_request_ids.add(task.request_id)

        # Advance/flag each touched EXECUTED request. Lock the request row BEFORE reading the drained state so two
        # concurrent workers serialize on it and whichever commits last sees every task (round-3 adversarial MED).
        for req_id in touched_request_ids:
            req = ErasureRequest.objects.select_for_update().get(pk=req_id)
            if req.status != ErasureRequest.Status.EXECUTED:
                continue
            if DeletionTask.objects.filter(
                    request_id=req_id,
                    status__in=[DeletionTask.Status.PENDING, DeletionTask.Status.FAILED]).exists():
                continue   # still retryable work to do
            if DeletionTask.objects.filter(request_id=req_id, status=DeletionTask.Status.DEAD).exists():
                req.status = ErasureRequest.Status.FAILED
                req.error = "one or more file deletions dead-lettered; operator intervention required"[:300]
                req.save(update_fields=["status", "error"])
            else:
                req.status = ErasureRequest.Status.COMPLETED
                req.save(update_fields=["status"])
    return processed


@transaction.atomic
def mark_request_held(request: ErasureRequest, reason: str) -> None:
    """Explicit status transition when a hold blocks a request (sanitised reason; no content)."""
    req = ErasureRequest.objects.select_for_update().get(pk=request.pk)
    req.status = ErasureRequest.Status.HELD
    req.error = reason[:300]
    req.save(update_fields=["status", "error"])
    logger.info("erasure request %s HELD: %s", req.id, reason[:80])
