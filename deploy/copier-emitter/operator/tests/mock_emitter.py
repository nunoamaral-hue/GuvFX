"""SYNTHETIC mock of `run_emitter.py attest` for the operator-wrapper Pester test (ADR-0077;
GFX-COPIER-ATTESTATION-OPERATOR-WRAPPER-021). Test tooling ONLY - never installed, never run in production.

It accepts the same CLI shape (`<mode> --config <path>`) and prints exactly ONE well-formed, SYNTHETIC attest
result on stdout. It uses NO real secret, contacts NO MT5/bridge/backend, and reads only the config's account_id.
The signature and fingerprints are obvious synthetic placeholders. A ``MOCK_EMITTER_MODE`` env var lets the test
drive failure paths (ok=false / malformed / multi-envelope) to prove the wrapper never POSTs on bad output.
"""
import argparse
import json
import os
import sys


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["attest", "observe-once", "run", "status"])
    p.add_argument("--config", required=True)
    p.add_argument("--max-cycles", type=int, default=None)
    args = p.parse_args(argv)
    if args.mode != "attest":
        print(json.dumps({"ok": False, "mode": args.mode, "reason": "mock_only_supports_attest"}))
        return 1
    try:
        with open(args.config, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        account_id = int(cfg.get("account_id", 0))
    except Exception:
        print(json.dumps({"ok": False, "mode": "attest", "reason": "config_error"}))
        return 2

    behaviour = os.environ.get("MOCK_EMITTER_MODE", "ok")
    if behaviour == "fail":
        print(json.dumps({"ok": False, "mode": "attest", "reason": "attestation_key_unavailable"}))
        return 1
    if behaviour == "malformed":
        print("this is not json")
        return 0
    if behaviour == "multi":
        print(json.dumps({"ok": True, "mode": "attest", "envelope": {"signed": {}, "signature": "x"}}))
        print(json.dumps({"ok": True, "mode": "attest", "envelope": {"signed": {}, "signature": "y"}}))
        return 0

    envelope = {
        "signed": {
            "protocol_version": "1", "operation": "copier-attest", "key_id": "copier-ingest",
            "build_sha": "SYNTHETIC_SHA", "host_id": "SYNTHETIC-HOST", "account_id": account_id,
            "broker_login_fp": "synthetic_login_fp", "broker_server": str(cfg.get("broker_server", "")),
            "terminal_fp": "synthetic_terminal_fp", "bridge_id": "synthetic_bridge",
            "config_digest": "synthetic_digest", "observed_login_fp": "", "nonce": "synthetic_nonce_0001",
            "timestamp": 1.0, "expiry": 2.0,
            "synthetic_big_int": 9007199254740993,
        },
        "signature": "SYNTHETIC_SIGNATURE_NOT_A_REAL_HMAC",
    }
    print(json.dumps({"ok": True, "mode": "attest", "envelope": envelope, "key_id": "copier-ingest",
                      "observed_identity_probed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
