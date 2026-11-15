#!/usr/bin/env python3
"""recovery_acceptance_runner — host wrapper for a bounded single-account recovery acceptance (packet-127).

Runs ON the production host (where ``docker compose`` and ``beta.env`` are local). It applies the SAME guaranteed
``finally`` teardown contract that is unit-tested in ``hosted_workspace.recovery_acceptance.run_bounded_acceptance``:
whatever happens, it restores ``beta.env`` byte-exact, recreates ONLY the backend, confirms both recovery flags read
OFF (via ``recovery_ops config``), and releases the governed lease. A teardown failure is surfaced loudly and the
lease is left to self-expire (TTL) — recovery flags are never left silently enabled.

This is an OPERATOR tool (it shells to ``docker compose`` and the compose exec'd ``recovery_ops`` CLI, which is the
single source of truth for the lease). It is Django-free. It arms only the conservative allow-list-of-one /
breaker-1-1 configuration; the human still performs the single unavoidable terminal stop out of band.

NOTE: the ``finally`` block always runs; a KeyboardInterrupt/timeout therefore still tears down. ``--no-remove-orphans``
is enforced implicitly (we never pass it).
"""
import argparse
import json
import subprocess
import sys
import uuid


def _run(cmd, check=True, capture=False):
    return subprocess.run(cmd, check=check, text=True,
                          stdout=(subprocess.PIPE if capture else None),
                          stderr=(subprocess.PIPE if capture else None))


def _compose(compose_dir, *args):
    return ["docker", "compose", "--project-directory", compose_dir, *args]


def _recovery_ops(compose_dir, *args):
    return _compose(compose_dir, "exec", "-T", "guvfx-backend", "python", "manage.py", "recovery_ops", *args)


ARM_BLOCK = (
    "\n# --- GUVFX bounded recovery acceptance (packet-127 runner; auto-removed on teardown) ---\n"
    "HOSTED_ESTABLISHED_RECOVERY_ENABLED=1\n"
    "HOSTED_LIVE_RECOVERY_ENABLED=1\n"
    "HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS={account}\n"
    "HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN=1\n"
    "HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES=1\n"
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bounded single-account recovery acceptance with guaranteed teardown.")
    ap.add_argument("--account", type=int, required=True)
    ap.add_argument("--compose-dir", required=True, help="Directory containing the prod docker-compose + beta.env.")
    ap.add_argument("--env-file", required=True, help="Path to beta.env on the host.")
    ap.add_argument("--owner", required=True)
    ap.add_argument("--ttl", type=int, default=1800)
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and exit without mutating anything.")
    args = ap.parse_args(argv)

    correlation = "acc-%d-%s" % (args.account, uuid.uuid4().hex[:8])
    purpose = "bounded_recovery_acceptance_account_%d" % args.account

    if args.dry_run:
        print(json.dumps({"plan": "acquire→backup→arm→recreate→(operator stops terminal + watches)→FINALLY teardown",
                          "account": args.account, "correlation": correlation, "ttl": args.ttl}))
        return 0

    # 1) acquire the recovery lease (fail-closed: refuses if a deploy lease is live).
    acq = _recovery_ops(args.compose_dir, "lease-acquire", "--kind", "recovery_acceptance",
                        "--purpose", purpose, "--owner", args.owner, "--correlation", correlation,
                        "--ttl", str(args.ttl))
    r = _run(acq, check=False, capture=True)
    if r.returncode != 0:
        print("LEASE ACQUIRE REFUSED:", (r.stdout or "") + (r.stderr or ""))
        return 1
    print("LEASE ACQUIRED:", r.stdout.strip())

    with open(args.env_file, "r", encoding="utf-8") as fh:
        baseline = fh.read()
    armed = False
    try:
        with open(args.env_file, "w", encoding="utf-8") as fh:
            fh.write(baseline + ARM_BLOCK.format(account=args.account))
        armed = True   # beta.env is now mutated — the finally MUST restore it even if the recreate below fails.
        _run(_compose(args.compose_dir, "up", "-d", "--no-deps", "--force-recreate", "guvfx-backend"))
        print("ARMED account %d; recreate done. Operator: stop ONLY #%d terminal and watch; Ctrl-C to teardown."
              % (args.account, args.account))
        # Operator watches out of band. Block until interrupted / TTL guidance printed.
        input("Press Enter when the acceptance window is complete (PASS/FAIL) to tear down... ")
        outcome = "OPERATOR_COMPLETE"
    except KeyboardInterrupt:
        outcome = "INTERRUPTED"
    except Exception as exc:  # noqa: BLE001
        outcome = "ERROR:%s" % type(exc).__name__
    finally:
        # GUARANTEED teardown: restore byte-exact, recreate, verify flags OFF, release lease.
        teardown_ok = True
        try:
            if armed:
                with open(args.env_file, "w", encoding="utf-8") as fh:
                    fh.write(baseline)
                _run(_compose(args.compose_dir, "up", "-d", "--no-deps", "--force-recreate", "guvfx-backend"))
            cfg = _run(_recovery_ops(args.compose_dir, "config"), check=False, capture=True)
            meta = {}
            try:
                meta = json.loads((cfg.stdout or "{}").strip().splitlines()[-1])
            except Exception:  # noqa: BLE001
                teardown_ok = False
            if meta.get("established_recovery_enabled") or meta.get("live_recovery_enabled"):
                teardown_ok = False
            rel = _run(_recovery_ops(args.compose_dir, "lease-release", "--correlation", correlation),
                       check=False, capture=True)
            if rel.returncode != 0:
                teardown_ok = False
            print(json.dumps({"outcome": outcome, "teardown_ok": teardown_ok,
                              "flags": {"established": meta.get("established_recovery_enabled"),
                                        "live": meta.get("live_recovery_enabled")}}))
            if not teardown_ok:
                print("!!! TEARDOWN INCOMPLETE — recovery flags may still be set; lease will self-expire. "
                      "INVESTIGATE NOW and run: recovery_ops config / lease-reclaim.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print("!!! TEARDOWN FAILED (%s) — restore beta.env + recreate backend manually; lease self-expires."
                  % type(exc).__name__, file=sys.stderr)
            return 3
    return 0 if teardown_ok else 3


if __name__ == "__main__":
    sys.exit(main())
