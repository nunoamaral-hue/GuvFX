#!/usr/bin/env python3
"""guarded_deploy — the ONE shared, lease-guarded backend production-deploy wrapper (packet-128).

Packet-126 showed an ad-hoc backend deploy (build + ``docker compose up --force-recreate``) can race an in-flight
established-recovery/acceptance window and reset ``beta.env`` mid-test. This wrapper is the canonical backend-deploy
entry point that closes that race: it **acquires a governed DEPLOY lease before any mutation** (which the lease
module refuses while a RECOVERY lease is live or MALFORMED — the gate), runs the existing **rollback-first,
backend-only, never-``--remove-orphans``** build+recreate, and **releases the lease through guaranteed cleanup**.
While the deploy lease is held a recovery attempt fails closed (``recovery_lease`` mutual exclusion), and vice-versa.

Guarantees (unit-tested with an injected ``DeployOps`` fake — no production command runs in the tests):
  * NO mutation begins before the deploy lease is acquired (or an explicit, audited emergency override is supplied);
  * a live/malformed recovery lease REFUSES the deploy before any mutation;
  * a clean success OR a successful rollback releases the lease; a deploy failure whose ROLLBACK also fails RETAINS
    the lease and ALERTS (never falsely reports cleanup) — the lease then self-expires via its TTL;
  * the emergency override requires a NON-EMPTY reason, is audited, and the reason is redacted (no secret / no env);
  * it is usable when the backend/Django is unavailable — it uses only the stdlib ``recovery_lease`` file primitives.

This wrapper never enables recovery flags, never touches Copy-Trading / broker / MT5 / credentials / ``is_active``,
and never passes ``--remove-orphans``. The real host ``DeployOps`` shells to git + docker; operators route every
backend deploy through this wrapper (see docs/operations/hosted-workspace/RECOVERY_OPERATING_MODEL.md)."""
from __future__ import annotations

import abc
import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Import the single-source lease primitives. In the Django test suite ``hosted_workspace`` is already importable;
# on the production host (guvfx-app checkout) add ``<repo>/backend`` to the path. recovery_lease is stdlib-only at
# import, so this works even when the backend CONTAINER is down (the deploy host always has the code checked out).
try:
    from hosted_workspace import recovery_lease as RL
except Exception:  # noqa: BLE001
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
    from hosted_workspace import recovery_lease as RL

OUTCOME_DEPLOYED = "DEPLOYED"
OUTCOME_VERIFY_FAILED = "VERIFY_FAILED"
OUTCOME_ERROR = "ERROR"
OUTCOME_REFUSED = "REFUSED"


def _default_lease_file():
    """Resolve the lease file from the environment ONLY — never a temp fallback (that would bypass the gate on a
    backend-down host). Returns None when unset, so the CLI fails closed."""
    f = os.getenv("GUVFX_RECOVERY_LEASE_FILE")
    if f:
        return f
    d = os.getenv("GUVFX_RECOVERY_LEASE_DIR")
    if d:
        return os.path.join(d, RL._LEASE_FILENAME)
    return None


class DeployOps(abc.ABC):
    """Injected production I/O. The real implementation shells to git + docker; the test fake records calls."""

    @abc.abstractmethod
    def now(self) -> datetime: ...

    @abc.abstractmethod
    def tag_rollback(self) -> None:
        """Rollback-FIRST: preserve the currently-running backend image under an explicit rollback tag."""

    @abc.abstractmethod
    def build(self) -> None:
        """Build the stamped backend image from the target SHA."""

    @abc.abstractmethod
    def recreate(self) -> None:
        """Recreate ONLY guvfx-backend (``--no-deps --force-recreate``; NEVER ``--remove-orphans``)."""

    @abc.abstractmethod
    def verify(self) -> bool:
        """True iff the recreated backend is healthy at the target revision."""

    @abc.abstractmethod
    def rollback(self) -> bool:
        """Restore the preserved rollback image + recreate. True iff production is back to a known-good state."""

    @abc.abstractmethod
    def alert(self, title: str, detail: dict) -> None: ...


@dataclass
class DeployResult:
    outcome: str
    lease_held: bool
    lease_released: bool
    rolled_back: object            # bool | None
    reason: str
    detail: dict = field(default_factory=dict)

    def ok(self) -> bool:
        return self.outcome == OUTCOME_DEPLOYED and (self.lease_released or not self.lease_held)


def run_guarded_deploy(ops: DeployOps, *, correlation_id: str, owner: str, ttl_s: int, lease_path: str | None = None,
                       override_reason: str | None = None, purpose: str = "backend_production_deploy") -> DeployResult:
    """Acquire the deploy lease (gate), run the rollback-first build+recreate, and release through guaranteed
    cleanup. No mutation runs before the lease is acquired (or an explicit audited override is supplied)."""
    now = ops.now()
    blocked, block_reason = RL.blocks(RL.KIND_DEPLOY, now, lease_path)   # gate: recovery/malformed lease ⇒ blocked
    ok_acq, lease_or_reason = RL.acquire(RL.KIND_DEPLOY, purpose, owner, correlation_id, ttl_s, now, lease_path)
    lease_held = bool(ok_acq)

    if not ok_acq:
        reason = block_reason if blocked else str(lease_or_reason)
        ovr = (override_reason or "").strip()
        # A MALFORMED (corrupt) lease is NEVER overridable — it is a fail-closed integrity condition the operator must
        # clear with `recovery_ops lease-reclaim` first. The emergency override bypasses only a live (ACTIVE) lease.
        if "malformed" in reason.lower():
            return DeployResult(outcome=OUTCOME_REFUSED, lease_held=False, lease_released=False, rolled_back=None,
                                reason=f"deploy_refused:{reason}")
        if not ovr:
            # No mutation. The deploy is refused before build/recreate — this is the whole point of the gate.
            return DeployResult(outcome=OUTCOME_REFUSED, lease_held=False, lease_released=False, rolled_back=None,
                                reason=f"deploy_refused:{reason}")
        # EXPLICIT, AUDITED emergency override: proceed WITHOUT a deploy lease (the recovery lease still holds it).
        safe_reason = ovr if RL._secret_free(ovr) else "<redacted>"
        RL._audit("deploy_emergency_override", now, lease_path, kind=RL.KIND_DEPLOY, correlation_id=correlation_id,
                  reason="override:" + safe_reason[:160])
        lease_held = False

    outcome = OUTCOME_ERROR
    rolled_back: object = None
    detail: dict = {}
    try:
        ops.tag_rollback()                 # rollback-first (before :latest moves)
        ops.build()
        ops.recreate()                     # backend-only; never --remove-orphans (enforced by the ops impl)
        outcome = OUTCOME_DEPLOYED if ops.verify() else OUTCOME_VERIFY_FAILED
    except Exception as exc:  # noqa: BLE001
        outcome = OUTCOME_ERROR
        detail["exception"] = type(exc).__name__
    finally:
        lease_released = False
        try:
            if outcome != OUTCOME_DEPLOYED:
                # Deploy did not cleanly verify → attempt rollback-first restoration.
                try:
                    rolled_back = bool(ops.rollback())
                except Exception as exc:  # noqa: BLE001
                    rolled_back = False
                    detail["rollback_exception"] = type(exc).__name__
            if outcome == OUTCOME_DEPLOYED or rolled_back:
                # Clean success OR production restored → safe to release the deploy lease.
                if lease_held:
                    rel, rel_reason = RL.release(correlation_id, ops.now(), lease_path)
                    lease_released = bool(rel)
                    if not rel:
                        ops.alert("guarded_deploy lease release failed",
                                  {"reason": rel_reason, "correlation_id": correlation_id})
            else:
                # Deploy FAILED and rollback FAILED (or was impossible) → production is indeterminate. If we hold a
                # deploy lease we RETAIN it (keep blocking further deploys AND recovery) and ALERT — never falsely
                # report cleanup; it self-expires via TTL. On the override path no deploy lease was held (the recovery
                # lease still blocks), so the alert says so rather than claiming a retained deploy lease.
                held_note = ("deploy lease RETAINED (self-expires via TTL)" if lease_held
                             else "no deploy lease held (override path; a recovery lease still blocks)")
                ops.alert("guarded_deploy FAILED and rollback FAILED — production indeterminate; " + held_note,
                          {"outcome": outcome, "rolled_back": rolled_back, "correlation_id": correlation_id,
                           "lease_held": lease_held, "note": "investigate prod immediately"})
        except Exception as exc:  # noqa: BLE001
            ops.alert("guarded_deploy cleanup error", {"error": type(exc).__name__, "correlation_id": correlation_id})
    return DeployResult(outcome=outcome, lease_held=lease_held, lease_released=lease_released, rolled_back=rolled_back,
                        reason=("overridden" if (not ok_acq and (override_reason or '').strip()) else "acquired"),
                        detail=detail)


# ── Real host DeployOps + CLI (operator-run; not exercised in CI) ────────────────────────────────────────────────
import subprocess  # noqa: E402


def _run(cmd, capture=False):
    return subprocess.run(cmd, check=True, text=True,
                          stdout=(subprocess.PIPE if capture else None),
                          stderr=(subprocess.PIPE if capture else None))


class _RealDeployOps(DeployOps):
    def __init__(self, *, sha, compose_dir, app_dir, release_prefix, health_url,
                 health_retries=12, health_sleep_s=5):
        self.sha = sha
        self.compose_dir = compose_dir
        self.app_dir = app_dir
        self.release_prefix = release_prefix
        self.health_url = health_url
        self.health_retries = health_retries
        self.health_sleep_s = health_sleep_s
        self._ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._rollback_tag = None

    def now(self):
        return datetime.now(timezone.utc)

    def _compose(self, *args):
        return ["docker", "compose", "--project-directory", self.compose_dir, *args]

    def tag_rollback(self):
        be = _run(self._compose("ps", "-q", "guvfx-backend"), capture=True).stdout.strip()
        img = _run(["docker", "inspect", be, "--format", "{{.Image}}"], capture=True).stdout.strip()
        self._rollback_tag = "guvfx-prod-guvfx-backend:rollback-pre-%s-%s" % (self.release_prefix, self._ts)
        _run(["docker", "tag", img, self._rollback_tag])
        print("ROLLBACK_TAG=%s (%s)" % (self._rollback_tag, img))

    def build(self):
        _run(["git", "-C", self.app_dir, "fetch", "origin", "--quiet"])
        _run(["git", "-C", self.app_dir, "checkout", "--quiet", self.sha])
        head = _run(["git", "-C", self.app_dir, "rev-parse", "HEAD"], capture=True).stdout.strip()
        if head != self.sha:
            raise RuntimeError("checkout mismatch: %s != %s" % (head, self.sha))
        _run(["docker", "build", "-t", "guvfx-prod-guvfx-backend:latest",
              "--build-arg", "GIT_COMMIT=%s" % self.sha, "--build-arg", "BUILD_TIMESTAMP=%s" % self._ts,
              "--build-arg", "RELEASE_ID=%s-%s-%s" % (self.release_prefix, self.sha[:12], self._ts),
              os.path.join(self.app_dir, "backend")])

    def recreate(self):
        # backend ONLY; NEVER --remove-orphans.
        _run(self._compose("up", "-d", "--no-deps", "--force-recreate", "guvfx-backend"))

    def verify(self):
        # 1) Revision: read ONLY the one non-secret var (never materialise the whole container env, which carries
        #    DJANGO_SECRET_KEY / DB_PASSWORD / tokens).
        try:
            be = _run(self._compose("ps", "-q", "guvfx-backend"), capture=True).stdout.strip()
            git = _run(["docker", "exec", be, "printenv", "GUVFX_GIT_COMMIT"], capture=True).stdout.strip()
        except Exception:  # noqa: BLE001 — container missing/exited ⇒ not healthy
            return False
        if git != self.sha:
            return False
        # 2) Readiness: recreate() returns when the container is CREATED, not when gunicorn is serving — poll the
        #    health endpoint for a REAL 200 (a DB-touching readiness signal, not an auth-layer 401) within a bounded
        #    window. A migration-broken / 500-ing revision never reaches 200 here ⇒ verify fails ⇒ rollback.
        import time
        for _ in range(self.health_retries):
            try:
                code = _run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "8",
                             self.health_url], capture=True).stdout.strip()
                if code == "200":
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(self.health_sleep_s)
        return False

    def rollback(self):
        if not self._rollback_tag:
            return False
        try:
            _run(["docker", "tag", self._rollback_tag, "guvfx-prod-guvfx-backend:latest"])
            _run(self._compose("up", "-d", "--no-deps", "--force-recreate", "guvfx-backend"))
            return True
        except Exception:  # noqa: BLE001
            return False

    def alert(self, title, detail):
        print("!!! %s :: %s" % (title, json.dumps(detail, sort_keys=True)), file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Lease-guarded, rollback-first, backend-only production deploy.")
    ap.add_argument("--sha", required=True)
    ap.add_argument("--compose-dir", required=True)
    ap.add_argument("--app-dir", required=True, help="guvfx-app checkout dir (holds .git + backend/).")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--lease-file", default=_default_lease_file(),
                    help="The governed lease state file (or set GUVFX_RECOVERY_LEASE_FILE / GUVFX_RECOVERY_LEASE_DIR). "
                         "REQUIRED — the deploy is refused fail-closed if it is not configured (never a temp fallback).")
    ap.add_argument("--health-url", default="https://api.guvfx.com/api/health/",
                    help="Readiness URL that must return a real 200 for the recreated stack (derive from the target).")
    ap.add_argument("--release-prefix", default="deploy")
    ap.add_argument("--ttl", type=int, default=RL.TTL_DEFAULT_S)
    ap.add_argument("--emergency-override", dest="override", default=None,
                    help="Explicit, audited, non-empty reason to deploy despite a LIVE recovery lease (never a "
                         "malformed one).")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    # FAIL CLOSED: a deploy with no lease file configured must NOT silently fall back to a temp dir and bypass the
    # gate — refuse, exactly like scripts/deploy_recovery_gate.py.
    if not args.lease_file:
        print(json.dumps({"outcome": OUTCOME_REFUSED, "reason": "no_lease_file_configured_fail_closed"}))
        return 3
    correlation = "deploy-%s-%s" % (args.sha[:12], uuid.uuid4().hex[:8])
    if args.dry_run:
        print(json.dumps({"plan": "acquire-deploy-lease → rollback-tag → build → recreate(backend-only) → verify → "
                                  "release(or retain+alert on rollback failure)", "sha": args.sha,
                          "lease_file": args.lease_file, "correlation": correlation}))
        return 0
    ops = _RealDeployOps(sha=args.sha, compose_dir=args.compose_dir, app_dir=args.app_dir,
                         release_prefix=args.release_prefix, health_url=args.health_url)
    res = run_guarded_deploy(ops, correlation_id=correlation, owner=args.owner, ttl_s=args.ttl,
                             lease_path=args.lease_file, override_reason=args.override)
    print(json.dumps({"outcome": res.outcome, "lease_held": res.lease_held, "lease_released": res.lease_released,
                      "rolled_back": res.rolled_back, "reason": res.reason, "detail": res.detail}, sort_keys=True))
    return 0 if res.ok() else 3


if __name__ == "__main__":
    sys.exit(main())
