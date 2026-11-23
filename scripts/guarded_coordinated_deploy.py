#!/usr/bin/env python3
"""guarded_coordinated_deploy — ONE lease-guarded, rollback-first, coordinated BACKEND+FRONTEND deploy (GFX-167).

WHY THIS EXISTS
---------------
The authorized GFX-165 consolidated Dashboard deploy correctly STOPPED before mutation: ``scripts/guarded_deploy.py``
is **backend-only** and **self-releasing** (it acquires a ``KIND_DEPLOY`` lease at the start of its run and releases it
in its own ``finally``), the lease is a **strict single-lease mutual-exclusion** model that refuses a second same-kind
acquire (``recovery_lease.acquire`` ⇒ ``held:deploy:…``), and no frontend/coordinated lease-aware wrapper existed. A
coordinated release therefore could not hold ONE continuous exclusion across both the backend and the frontend phases
without improvising (splitting into two lease windows, leaving the frontend unguarded, or abusing the emergency
override) — all forbidden. This wrapper is the smallest durable correction: it holds **exactly one** ``KIND_DEPLOY``
lease continuously across BOTH services' ``tag → build → recreate → verify → combined accept/rollback``.

GUARANTEES (unit-tested with an injected ``CoordinatedDeployOps`` fake — no production command runs in the tests):
  * NO mutation begins before the single deploy lease is acquired; a live/malformed/same-kind lease REFUSES fail-closed
    (there is **no** emergency override and **no** nested same-kind bypass — contract §11);
  * the ONE lease is held continuously across both phases (never released between backend and frontend);
  * rollback-FIRST: both current images are captured + tagged BEFORE any build/recreate; a failure at ANY step rolls
    BOTH services back to the captured baseline and re-verifies both identities + health;
  * the lease is released ONLY after a clean success OR a fully-verified combined rollback; if rollback cannot restore a
    healthy known baseline the lease is RETAINED fail-closed and a sanitized operator alert with exact recovery steps is
    emitted (never a false cleanup) — it self-expires via TTL;
  * structurally impossible to pass ``--remove-orphans`` or a ``NEXT_PUBLIC_*_ENABLED`` capability flag, and the
    frontend build fails closed without ``NEXT_PUBLIC_API_BASE_URL`` (pure command builders, asserted by tests);
  * deploys an EXACT target SHA independent of the wrapper's own repository revision;
  * no secret / environment dump / credential in argv, logs, result, audit or alerts;
  * usable with the backend/Django DOWN — it uses only the stdlib ``recovery_lease`` file primitives.

This wrapper never enables any flag, never touches Copy-Trading / broker / MT5 / credentials, never passes
``--remove-orphans``, and recreates ONLY ``guvfx-backend`` and ``guvfx-frontend`` (``--no-deps``), preserving every
unrelated service and standalone container. The real host ops shell to git + docker; it is NOT exercised in CI.
"""
from __future__ import annotations

import abc
import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Single-source lease primitives. recovery_lease is stdlib-only at import, so this works even when the backend
# CONTAINER is down (the deploy host always has the code checked out).
try:
    from hosted_workspace import recovery_lease as RL
except Exception:  # noqa: BLE001
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
    from hosted_workspace import recovery_lease as RL

OUTCOME_DEPLOYED = "DEPLOYED"
OUTCOME_ROLLED_BACK = "ROLLED_BACK"
OUTCOME_VERIFY_FAILED = "VERIFY_FAILED"
OUTCOME_ERROR = "ERROR"
OUTCOME_REFUSED = "REFUSED"
OUTCOME_INDETERMINATE = "INDETERMINATE"   # deploy failed AND rollback could not restore a healthy baseline ⇒ lease RETAINED

BACKEND_SERVICE = "guvfx-backend"
FRONTEND_SERVICE = "guvfx-frontend"
BACKEND_IMAGE = "guvfx-prod-guvfx-backend:latest"
FRONTEND_IMAGE = "guvfx-prod-guvfx-frontend:latest"

# Contract §3: the explicit, required lease path (the single source of truth the deploy/recovery pair share).
DEFAULT_LEASE_FILE = "/srv/guvfx/mt5_handoff/recovery_operational_lease.json"
DEFAULT_FRONTEND_API_BASE = "https://api.guvfx.com"
DEFAULT_HEALTH_URL = "https://api.guvfx.com/api/health/"      # backend readiness oracle (real 200, DB-touching)
DEFAULT_FRONTEND_URL = "https://guvfx.com/"                   # public frontend (200) + /build-info.json identity

# Capability flags that MUST NEVER appear in the frontend build (DARK is the default-unset build). Defence in depth:
# the builder never emits them, and a guard rejects any arg that matches, so a future edit cannot smuggle one in.
_CAP_FLAG_RE = re.compile(r"NEXT_PUBLIC_[A-Z0-9_]*_ENABLED")
_REMOVE_ORPHANS = "--remove-orphans"


# ── pure command builders (no I/O) — the structural contract, unit-tested without docker ─────────────────────────
def _compose(compose_dir: str, *args: str) -> list:
    return ["docker", "compose", "--project-directory", compose_dir, *args]


def recreate_cmd(compose_dir: str, service: str) -> list:
    """Recreate exactly ONE service: ``--no-deps --force-recreate <service>``; NEVER ``--remove-orphans`` (contract §7)."""
    if service not in (BACKEND_SERVICE, FRONTEND_SERVICE):
        raise ValueError("recreate_cmd refuses unknown service: %r" % service)
    cmd = _compose(compose_dir, "up", "-d", "--no-deps", "--force-recreate", service)
    assert _REMOVE_ORPHANS not in cmd  # structural guarantee (defence in depth)
    return cmd


def backend_build_cmd(app_dir: str, sha: str, ts: str, release_prefix: str) -> list:
    """Build the stamped backend image from the EXACT target SHA (context: ``<app_dir>/backend``)."""
    return [
        "docker", "build", "-t", BACKEND_IMAGE,
        "--build-arg", "GIT_COMMIT=%s" % sha,
        "--build-arg", "BUILD_TIMESTAMP=%s" % ts,
        "--build-arg", "RELEASE_ID=%s-%s-%s" % (release_prefix, sha[:12], ts),
        "--label", "org.opencontainers.image.revision=%s" % sha,
        os.path.join(app_dir, "backend"),
    ]


def frontend_build_cmd(app_dir: str, sha: str, ts: str, api_base: str) -> list:
    """Build the DARK frontend image from the EXACT target SHA. REQUIRES ``NEXT_PUBLIC_API_BASE_URL`` (contract §6/§13);
    NEVER passes a ``NEXT_PUBLIC_*_ENABLED`` capability flag (contract §6)."""
    if not api_base or not str(api_base).strip():
        raise ValueError("frontend_api_base_required_fail_closed")
    cmd = [
        "docker", "build", "-t", FRONTEND_IMAGE,
        "--build-arg", "NEXT_PUBLIC_API_BASE_URL=%s" % api_base,
        "--build-arg", "GIT_COMMIT=%s" % sha,
        "--build-arg", "BUILD_TIMESTAMP=%s" % ts,
        "--label", "org.guvfx.git_commit=%s" % sha,
        "--label", "org.guvfx.build_timestamp=%s" % ts,
        os.path.join(app_dir, "frontend"),
    ]
    # Defence in depth: no capability flag may ever reach the frontend build (keeps the DARK build reproducible).
    for tok in cmd:
        if _CAP_FLAG_RE.search(tok):
            raise ValueError("capability_flag_forbidden_in_frontend_build: %r" % tok)
    assert _REMOVE_ORPHANS not in cmd
    return cmd


def _default_lease_file() -> str:
    """Resolve the lease file: explicit env first (tests), else the contract's canonical path. NEVER a temp fallback."""
    f = os.getenv("GUVFX_RECOVERY_LEASE_FILE")
    if f:
        return f
    d = os.getenv("GUVFX_RECOVERY_LEASE_DIR")
    if d:
        return os.path.join(d, RL._LEASE_FILENAME)
    return DEFAULT_LEASE_FILE


def inventory_unchanged(before: dict, after: dict) -> bool:
    """True iff the coordinated deploy did not DISRUPT the MANAGED compose project's inventory: every project container
    running before the deploy is still present and in the same state afterwards (the two intended recreates keep their
    name + running state). A NEW container appearing during the (multi-minute) build window does NOT fail the deploy; a
    vanished or state-changed prior MANAGED container DOES (fail-closed on real disruption). The caller scopes
    ``before``/``after`` to the managed compose project (``docker compose ps``), so unrelated transient
    ``docker run --rm`` jobs in other projects never enter this comparison and cannot false-positive it."""
    return all(after.get(name) == state for name, state in before.items())


class PreflightError(Exception):
    """A fail-closed pre-mutation condition (contract §13): unavailable/ambiguous SHA, dirty/untrusted source, missing
    compose/service/image, or a missing frontend API-base. Raised BEFORE any image is tagged/built/recreated."""


class CoordinatedDeployOps(abc.ABC):
    """Injected production I/O. The real implementation shells to git + docker; the test fake records calls."""

    @abc.abstractmethod
    def now(self) -> datetime: ...

    @abc.abstractmethod
    def preflight(self) -> None:
        """Fail-closed pre-mutation checks (no mutation): deployable source (fetch + checkout EXACT sha + HEAD==sha +
        clean tree), compose file + both services present, backend+frontend baseline images present, API-base present.
        Raise :class:`PreflightError` on any violation."""

    @abc.abstractmethod
    def snapshot_inventory(self) -> dict:
        """Return a ``{container_name: state}`` map of ALL running containers (managed + standalone), for the
        before/after unexpected-inventory-change guard."""

    @abc.abstractmethod
    def tag_rollback_backend(self) -> None: ...

    @abc.abstractmethod
    def tag_rollback_frontend(self) -> None: ...

    @abc.abstractmethod
    def build_backend(self) -> None: ...

    @abc.abstractmethod
    def build_frontend(self) -> None: ...

    @abc.abstractmethod
    def recreate_backend(self) -> None: ...

    @abc.abstractmethod
    def recreate_frontend(self) -> None: ...

    @abc.abstractmethod
    def verify_backend(self) -> bool:
        """True iff backend is at the target revision (GUVFX_GIT_COMMIT == sha AND OCI revision label == sha) AND
        ``/api/health/`` returns a real 200."""

    @abc.abstractmethod
    def verify_frontend(self) -> bool:
        """True iff the served ``/build-info.json`` gitCommit == sha AND the image ``org.guvfx.git_commit`` label == sha
        AND both capability flags are false (DARK) AND the public frontend returns 200."""

    @abc.abstractmethod
    def verify_restored_backend(self) -> bool:
        """True iff, after a rollback, the backend is running the CAPTURED BASELINE image (the pre-deploy image id,
        NOT the target SHA) AND ``/api/health/`` returns a real 200. The rolled-back baseline is the PREVIOUS commit,
        so it must never be checked against the deploy target."""

    @abc.abstractmethod
    def verify_restored_frontend(self) -> bool:
        """True iff, after a rollback, the frontend is running the CAPTURED BASELINE image (the pre-deploy image id,
        NOT the target SHA) AND the public frontend returns 200."""

    @abc.abstractmethod
    def verify_inventory_unchanged(self, before: dict) -> bool:
        """True iff the running-container inventory is unchanged except for the two intended recreates (both still
        present + running). Any vanished/new/other-state-changed container ⇒ False (fail-closed, contract §13)."""

    @abc.abstractmethod
    def restore_backend(self) -> bool: ...

    @abc.abstractmethod
    def restore_frontend(self) -> bool: ...

    @abc.abstractmethod
    def alert(self, title: str, detail: dict) -> None: ...


@dataclass
class CoordinatedDeployResult:
    outcome: str
    lease_held: bool
    lease_released: bool
    rolled_back: object            # bool | None
    reason: str
    detail: dict = field(default_factory=dict)

    def ok(self) -> bool:
        return self.outcome == OUTCOME_DEPLOYED and self.lease_released


def run_guarded_coordinated_deploy(ops: CoordinatedDeployOps, *, correlation_id: str, owner: str, ttl_s: int,
                                   lease_path: str, purpose: str = "coordinated_production_deploy"
                                   ) -> CoordinatedDeployResult:
    """Acquire exactly ONE ``KIND_DEPLOY`` lease (gate — no override, no nested bypass), hold it continuously across the
    rollback-first coordinated backend+frontend deploy, and release only after clean success or a fully-verified
    combined rollback (else RETAIN fail-closed + alert)."""
    now = ops.now()
    # GATE — a single KIND_DEPLOY lease. A live (recovery OR same-kind deploy) or MALFORMED lease REFUSES before any
    # mutation. There is NO emergency override and NO nested same-kind bypass (contract §11).
    blocked, block_reason = RL.blocks(RL.KIND_DEPLOY, now, lease_path)
    ok_acq, lease_or_reason = RL.acquire(RL.KIND_DEPLOY, purpose, owner, correlation_id, ttl_s, now, lease_path)
    if not ok_acq:
        reason = block_reason if blocked else str(lease_or_reason)
        return CoordinatedDeployResult(OUTCOME_REFUSED, lease_held=False, lease_released=False, rolled_back=None,
                                       reason="deploy_refused:%s" % reason)

    mutated = False                 # True once the first image tag/build/recreate has run (⇒ rollback is required)
    outcome = OUTCOME_ERROR
    rolled_back: object = None
    detail: dict = {}
    try:
        # ── pre-mutation fail-closed checks (contract §13) — NO image is touched here ──
        ops.preflight()
        before = ops.snapshot_inventory()
        # ── ROLLBACK-FIRST: capture BOTH current images before any build/recreate (contract §9) ──
        ops.tag_rollback_backend()
        ops.tag_rollback_frontend()
        mutated = True
        # ── builds (exact target) → recreates (one service each, --no-deps --force-recreate) → verify both ──
        ops.build_backend()
        ops.build_frontend()
        ops.recreate_backend()
        ops.recreate_frontend()
        backend_ok = bool(ops.verify_backend())
        frontend_ok = bool(ops.verify_frontend())
        inventory_ok = bool(ops.verify_inventory_unchanged(before))
        if backend_ok and frontend_ok and inventory_ok:
            outcome = OUTCOME_DEPLOYED
        else:
            outcome = OUTCOME_VERIFY_FAILED
            detail["verify"] = {"backend": backend_ok, "frontend": frontend_ok, "inventory": inventory_ok}
    except PreflightError as exc:
        # Fail-closed BEFORE any mutation ⇒ clean refuse-after-acquire; no rollback needed.
        outcome = OUTCOME_REFUSED
        detail["preflight"] = str(exc)[:200]
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 — incl. interrupt path (contract test matrix)
        outcome = OUTCOME_ERROR
        detail["exception"] = type(exc).__name__
    finally:
        lease_released = False
        try:
            if not mutated:
                # No build/recreate ran (gate refuse, preflight fail-closed, OR a pre-mutation error such as a
                # transient docker-ps / git-status / tag failure) → production was NEVER touched → release the single
                # lease; no rollback is needed or possible, and retaining it here would falsely block all deploys +
                # recovery for the whole TTL over an untouched production.
                rel, rel_reason = RL.release(correlation_id, ops.now(), lease_path)
                lease_released = bool(rel)
                if not rel:
                    ops.alert("coordinated_deploy lease release failed (pre-mutation)",
                              {"reason": rel_reason, "correlation_id": correlation_id})
            elif outcome == OUTCOME_DEPLOYED:
                rel, rel_reason = RL.release(correlation_id, ops.now(), lease_path)
                lease_released = bool(rel)
                if not rel:
                    ops.alert("coordinated_deploy lease release failed",
                              {"reason": rel_reason, "correlation_id": correlation_id})
            else:
                # Deploy did not cleanly verify (or errored AFTER mutation) → COMBINED rollback: restore BOTH services.
                # Attempt each restore INDEPENDENTLY so one raising never skips the other (the other service must
                # still be reverted).
                def _try_restore(fn, key):
                    try:
                        return bool(fn())
                    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                        detail.setdefault("rollback_exceptions", {})[key] = type(exc).__name__
                        return False
                rb_be = _try_restore(ops.restore_backend, "backend")
                rb_fe = _try_restore(ops.restore_frontend, "frontend")
                restored_ok = False
                if rb_be and rb_fe:
                    # Confirm BOTH restored to the CAPTURED BASELINE image (the PRIOR commit, NEVER the deploy target)
                    # plus health, before claiming a verified rollback. Reusing the target-SHA deploy oracle here would
                    # misread every real rollback as a failure (the baseline is the previous commit).
                    try:
                        restored_ok = bool(ops.verify_restored_backend()) and bool(ops.verify_restored_frontend())
                    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
                        detail["restore_verify_exception"] = type(exc).__name__
                        restored_ok = False
                rolled_back = restored_ok
                if restored_ok:
                    outcome = OUTCOME_ROLLED_BACK
                    rel, rel_reason = RL.release(correlation_id, ops.now(), lease_path)
                    lease_released = bool(rel)
                    if not rel:
                        ops.alert("coordinated_deploy lease release failed after rollback",
                                  {"reason": rel_reason, "correlation_id": correlation_id})
                else:
                    # Rollback could NOT restore a healthy baseline → RETAIN the lease fail-closed (keep blocking further
                    # deploys AND recovery) and ALERT with exact recovery steps; it self-expires via TTL (contract §10).
                    outcome = OUTCOME_INDETERMINATE
                    ops.alert(
                        "coordinated_deploy FAILED and rollback could not restore a healthy baseline — PRODUCTION "
                        "INDETERMINATE; deploy lease RETAINED (self-expires via TTL)",
                        {"correlation_id": correlation_id, "rolled_back_backend": rb_be,
                         "rolled_back_frontend": rb_fe,
                         "recovery": [
                             "1) Do NOT start another deploy — the KIND_DEPLOY lease is intentionally retained.",
                             "2) Compare guvfx-backend + guvfx-frontend health/identity against the captured "
                             "rollback-pre-* image tags (docker images | grep rollback-pre).",
                             "3) Manually restore both: docker tag <rollback-pre-tag> <:latest> then "
                             "docker compose up -d --no-deps --force-recreate <service> for each; verify "
                             "/api/health/ 200 and the public frontend 200.",
                             "4) Only after both are verified healthy, clear the lease via "
                             "'recovery_ops lease-reclaim' (or let it self-expire via TTL)."]})
        except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001
            ops.alert("coordinated_deploy cleanup error", {"error": type(exc).__name__, "correlation_id": correlation_id})
    return CoordinatedDeployResult(outcome, lease_held=True, lease_released=lease_released, rolled_back=rolled_back,
                                   reason="acquired", detail=detail)


# ── Real host CoordinatedDeployOps + CLI (operator-run; NOT exercised in CI) ──────────────────────────────────────
import subprocess  # noqa: E402


def _run(cmd, capture=False):
    return subprocess.run(cmd, check=True, text=True,
                          stdout=(subprocess.PIPE if capture else None),
                          stderr=(subprocess.PIPE if capture else None))


class _RealCoordinatedDeployOps(CoordinatedDeployOps):
    def __init__(self, *, sha, compose_dir, app_dir, frontend_api_base, release_prefix="coordinated-deploy",
                 health_url=DEFAULT_HEALTH_URL, frontend_url=DEFAULT_FRONTEND_URL,
                 health_retries=12, health_sleep_s=5):
        self.sha = sha
        self.compose_dir = compose_dir
        self.app_dir = app_dir
        self.frontend_api_base = frontend_api_base
        self.release_prefix = release_prefix
        self.health_url = health_url
        self.frontend_url = frontend_url.rstrip("/") + "/"
        self.health_retries = health_retries
        self.health_sleep_s = health_sleep_s
        self._ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._rollback = {}       # service -> rollback-pre tag
        self._baseline_img = {}   # service -> baseline (pre-deploy) image id, for baseline-correct rollback verify

    def now(self):
        return datetime.now(timezone.utc)

    def _cps(self, service):
        return _run(_compose(self.compose_dir, "ps", "-q", service), capture=True).stdout.strip()

    def preflight(self):
        if not self.frontend_api_base or not str(self.frontend_api_base).strip():
            raise PreflightError("frontend_api_base_required")
        # Deployable source: fetch, checkout the EXACT sha, assert HEAD==sha, assert a clean (trusted) tree.
        try:
            _run(["git", "-C", self.app_dir, "fetch", "origin", "--quiet"])
            _run(["git", "-C", self.app_dir, "checkout", "--quiet", self.sha])
            head = _run(["git", "-C", self.app_dir, "rev-parse", "HEAD"], capture=True).stdout.strip()
        except Exception as exc:  # noqa: BLE001
            raise PreflightError("target_sha_unavailable_or_ambiguous:%s" % type(exc).__name__)
        if head != self.sha:
            raise PreflightError("checkout_mismatch")
        try:
            dirty = _run(["git", "-C", self.app_dir, "status", "--porcelain"], capture=True).stdout.strip()
        except Exception as exc:  # noqa: BLE001 — a failed status probe is a preflight failure, not a mid-deploy error
            raise PreflightError("git_status_probe_failed:%s" % type(exc).__name__)
        if dirty:
            raise PreflightError("dirty_or_untrusted_source_tree")
        # Compose file + both target services present; both baseline images (running containers) present. Every probe
        # here must fail-closed as a PreflightError (a clean, lease-releasing refuse) — never escape as a generic error.
        try:
            services = _run(_compose(self.compose_dir, "config", "--services"), capture=True).stdout.split()
        except Exception as exc:  # noqa: BLE001
            raise PreflightError("missing_compose_file:%s" % type(exc).__name__)
        for svc in (BACKEND_SERVICE, FRONTEND_SERVICE):
            if svc not in services:
                raise PreflightError("missing_service:%s" % svc)
            try:
                running = self._cps(svc)
            except Exception as exc:  # noqa: BLE001
                raise PreflightError("service_probe_failed:%s:%s" % (svc, type(exc).__name__))
            if not running:
                raise PreflightError("missing_running_baseline_container:%s" % svc)

    def snapshot_inventory(self):
        # Snapshot the MANAGED compose project's running containers (the set the deploy's own compose commands act on).
        # We deliberately do NOT snapshot the global `docker ps`: unrelated transient `docker run --rm` cron/maintenance
        # jobs come and go during the multi-minute build window and would false-positive the unchanged-inventory guard,
        # reverting a good deploy. Standalone / other-stack containers (Traefik, Guacamole, guvfx-wayond-listener) live
        # in different compose projects and are structurally untouched by this deploy (--no-deps, never --remove-orphans),
        # so they need not be in this guard.
        out = _run(_compose(self.compose_dir, "ps", "--format", "{{.Name}}\t{{.State}}"), capture=True).stdout
        inv = {}
        for line in out.splitlines():
            if "\t" in line:
                name, state = line.split("\t", 1)
                inv[name.strip()] = state.strip()
        return inv

    def _tag_rollback(self, service, image_ref):
        cid = self._cps(service)
        if not cid:
            raise RuntimeError("no_running_container_for_%s" % service)
        img = _run(["docker", "inspect", cid, "--format", "{{.Image}}"], capture=True).stdout.strip()
        tag = "%s:rollback-pre-%s-%s-%s" % (image_ref.split(":")[0], self.release_prefix, service, self._ts)
        _run(["docker", "tag", img, tag])
        self._rollback[service] = tag
        self._baseline_img[service] = img   # the PRE-DEPLOY image id — the identity a verified rollback must restore to
        print("ROLLBACK_TAG[%s]=%s (%s)" % (service, tag, img))

    def tag_rollback_backend(self):
        self._tag_rollback(BACKEND_SERVICE, BACKEND_IMAGE)

    def tag_rollback_frontend(self):
        self._tag_rollback(FRONTEND_SERVICE, FRONTEND_IMAGE)

    def build_backend(self):
        _run(backend_build_cmd(self.app_dir, self.sha, self._ts, self.release_prefix))

    def build_frontend(self):
        _run(frontend_build_cmd(self.app_dir, self.sha, self._ts, self.frontend_api_base))

    def recreate_backend(self):
        _run(recreate_cmd(self.compose_dir, BACKEND_SERVICE))

    def recreate_frontend(self):
        _run(recreate_cmd(self.compose_dir, FRONTEND_SERVICE))

    def _http_code(self, url):
        return _run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8", url],
                    capture=True).stdout.strip()

    def _poll_200(self, url):
        import time
        for _ in range(self.health_retries):
            try:
                if self._http_code(url) == "200":
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(self.health_sleep_s)
        return False

    def verify_backend(self):
        try:
            cid = self._cps(BACKEND_SERVICE)
            git = _run(["docker", "exec", cid, "printenv", "GUVFX_GIT_COMMIT"], capture=True).stdout.strip()
            label = _run(["docker", "inspect", cid, "--format",
                          "{{index .Config.Labels \"org.opencontainers.image.revision\"}}"],
                         capture=True).stdout.strip()
        except Exception:  # noqa: BLE001
            return False
        if git != self.sha or label != self.sha:
            return False
        return self._poll_200(self.health_url)

    def verify_frontend(self):
        try:
            cid = self._cps(FRONTEND_SERVICE)
            label = _run(["docker", "inspect", cid, "--format",
                          "{{index .Config.Labels \"org.guvfx.git_commit\"}}"], capture=True).stdout.strip()
        except Exception:  # noqa: BLE001
            return False
        if label != self.sha:
            return False
        # Poll readiness (200) BEFORE reading the served identity: `up -d --force-recreate` returns at container start,
        # not at port-bind, so a one-shot build-info fetch would false-fail a still-starting-but-healthy frontend and
        # needlessly roll the deploy back. The backend avoids this by polling /api/health/ with retries too.
        if not self._poll_200(self.frontend_url):
            return False
        # Served build-info.json must report the exact SHA and DARK capability flags (both false).
        try:
            raw = _run(["curl", "-s", "--max-time", "8", self.frontend_url + "build-info.json"], capture=True).stdout
            info = json.loads(raw)
        except Exception:  # noqa: BLE001
            return False
        if info.get("gitCommit") != self.sha:
            return False
        flags = info.get("flags", {}) or {}
        if flags.get("NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED") or flags.get("NEXT_PUBLIC_OPERATIONS_ENABLED"):
            return False
        return True

    def _verify_restored(self, service, health_url):
        # A verified rollback restores the CAPTURED BASELINE image (the PRIOR commit) — so verify the running container
        # is that exact baseline image id (never self.sha, the target) plus a real health 200.
        base = self._baseline_img.get(service)
        if not base:
            return False
        try:
            cid = self._cps(service)
            img = _run(["docker", "inspect", cid, "--format", "{{.Image}}"], capture=True).stdout.strip()
        except Exception:  # noqa: BLE001
            return False
        if img != base:
            return False
        return self._poll_200(health_url)

    def verify_restored_backend(self):
        return self._verify_restored(BACKEND_SERVICE, self.health_url)

    def verify_restored_frontend(self):
        return self._verify_restored(FRONTEND_SERVICE, self.frontend_url)

    def verify_inventory_unchanged(self, before):
        # Fail closed if the deploy DISRUPTED the running inventory (a prior container vanished or changed state); a new
        # transient container appearing during the multi-minute build window (an unrelated `run --rm` job) is benign.
        return inventory_unchanged(before, self.snapshot_inventory())

    def _restore(self, service, image_ref):
        tag = self._rollback.get(service)
        if not tag:
            return False
        try:
            _run(["docker", "tag", tag, image_ref])
            _run(recreate_cmd(self.compose_dir, service))
            return True
        except Exception:  # noqa: BLE001
            return False

    def restore_backend(self):
        return self._restore(BACKEND_SERVICE, BACKEND_IMAGE)

    def restore_frontend(self):
        return self._restore(FRONTEND_SERVICE, FRONTEND_IMAGE)

    def alert(self, title, detail):
        print("!!! %s :: %s" % (title, json.dumps(detail, sort_keys=True)), file=sys.stderr)


def _plan(args, correlation):
    ts = "<build-time>"
    return {
        "wrapper": "guarded_coordinated_deploy (GFX-167): ONE KIND_DEPLOY lease across backend+frontend",
        "target_sha": args.sha,
        "lease_file": args.lease_file,
        "correlation": correlation,
        "services": {"backend": BACKEND_SERVICE, "frontend": FRONTEND_SERVICE},
        "rollback_first": "tag current guvfx-backend + guvfx-frontend images BEFORE any build/recreate",
        "backend_build": backend_build_cmd(args.app_dir, args.sha, ts, args.release_prefix),
        "frontend_build": frontend_build_cmd(args.app_dir, args.sha, ts, args.frontend_api_base),
        "backend_recreate": recreate_cmd(args.compose_dir, BACKEND_SERVICE),
        "frontend_recreate": recreate_cmd(args.compose_dir, FRONTEND_SERVICE),
        "backend_verify": "GUVFX_GIT_COMMIT==sha AND OCI revision label==sha AND %s 200" % args.health_url,
        "frontend_verify": "served /build-info.json gitCommit==sha AND org.guvfx.git_commit label==sha AND flags DARK "
                           "AND %s 200" % (args.frontend_url.rstrip("/") + "/"),
        "never": ["--remove-orphans", "NEXT_PUBLIC_*_ENABLED", "emergency override", "nested same-kind bypass"],
        "lease_release": "only after clean success OR fully-verified combined rollback; else RETAIN fail-closed + alert",
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="ONE-lease-guarded, rollback-first, coordinated backend+frontend deploy.")
    ap.add_argument("--sha", required=True, help="EXACT target commit SHA to deploy (independent of this wrapper's own revision).")
    ap.add_argument("--compose-dir", required=True, help="Production compose project directory.")
    ap.add_argument("--app-dir", required=True, help="guvfx-app checkout dir (holds .git + backend/ + frontend/).")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--frontend-api-base", default=DEFAULT_FRONTEND_API_BASE,
                    help="NEXT_PUBLIC_API_BASE_URL for the frontend build (REQUIRED, fail-closed if empty).")
    ap.add_argument("--lease-file", default=_default_lease_file(),
                    help="Governed lease state file (contract: /srv/guvfx/mt5_handoff/recovery_operational_lease.json). "
                         "REQUIRED — the deploy is refused fail-closed if empty (never a temp fallback).")
    ap.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    ap.add_argument("--frontend-url", default=DEFAULT_FRONTEND_URL)
    ap.add_argument("--release-prefix", default="coordinated-deploy")
    ap.add_argument("--ttl", type=int, default=RL.TTL_MAX_S,
                    help="Lease TTL seconds (default 2h ceiling — a coordinated two-service deploy is longer than one).")
    ap.add_argument("--dry-run", action="store_true", help="Resolve + print the full plan WITHOUT any mutation.")
    args = ap.parse_args(argv)
    correlation = "coord-deploy-%s-%s" % (args.sha[:12], uuid.uuid4().hex[:8])
    # FAIL CLOSED: no lease file / no frontend api-base ⇒ refuse before anything.
    if not args.lease_file:
        print(json.dumps({"outcome": OUTCOME_REFUSED, "reason": "no_lease_file_configured_fail_closed"}))
        return 3
    if not args.frontend_api_base or not str(args.frontend_api_base).strip():
        print(json.dumps({"outcome": OUTCOME_REFUSED, "reason": "no_frontend_api_base_fail_closed"}))
        return 3
    if args.dry_run:
        print(json.dumps(_plan(args, correlation), sort_keys=True, indent=2))
        return 0
    ops = _RealCoordinatedDeployOps(sha=args.sha, compose_dir=args.compose_dir, app_dir=args.app_dir,
                                    frontend_api_base=args.frontend_api_base, release_prefix=args.release_prefix,
                                    health_url=args.health_url, frontend_url=args.frontend_url)
    res = run_guarded_coordinated_deploy(ops, correlation_id=correlation, owner=args.owner, ttl_s=args.ttl,
                                         lease_path=args.lease_file)
    print(json.dumps({"outcome": res.outcome, "lease_held": res.lease_held, "lease_released": res.lease_released,
                      "rolled_back": res.rolled_back, "reason": res.reason, "detail": res.detail}, sort_keys=True))
    return 0 if res.ok() else 3


if __name__ == "__main__":
    sys.exit(main())
