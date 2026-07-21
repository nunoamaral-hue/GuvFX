"""CVM-Inc-3 B2/B3P-1 — offline install-verify: prove the bind-guard refuses public binds, the LIVE bind is
pinned to the exact management address, and the FULL bundle matches the approved manifest. Safe to run
anywhere; performs NO Windows side-effects, starts NO server, binds NO socket.

Authenticity (verification B-7): pass ``--expect-manifest-sha256 <hex>`` (the reviewed commit's manifest.json
hash) so that editing an implementation file AND its manifest entry to match is still caught — the operator
pins the manifest.json's OWN hash to the value recorded in the merged commit.

Usage:  python -B validate.py [--expect-bind 100.79.101.19] [--expect-manifest-sha256 <hex>]
"""
import argparse
import hashlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import config          # noqa: E402
import manifest        # noqa: E402

EXPECTED_BIND_DEFAULT = config.DEFAULT_EXPECTED_BIND_HOST


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--expect-bind", default=EXPECTED_BIND_DEFAULT)
    ap.add_argument("--expect-manifest-sha256", default="")
    args = ap.parse_args(argv)
    ok = True

    # 1. bind-guard refuses wildcard/public/empty
    for bad in ("0.0.0.0", "::", "8.8.8.8", ""):
        try:
            config.assert_private_bind(bad); print(f"FAIL bind-guard accepted {bad!r}"); ok = False
        except config.ConfigError:
            print(f"ok   bind-guard refused {bad!r}")

    # 2. broad private predicate (offline) allows private addresses
    for good in ("127.0.0.1", "100.79.101.19", "10.0.0.5"):
        try:
            config.assert_private_bind(good); print(f"ok   bind-guard allowed private {good!r}")
        except config.ConfigError:
            print(f"FAIL bind-guard refused private {good!r}"); ok = False

    # 3. LIVE exact-bind pin: only the expected management address is accepted (B-9)
    try:
        config.assert_exact_bind(args.expect_bind, args.expect_bind); print(f"ok   exact-bind pin accepts {args.expect_bind!r}")
    except config.ConfigError:
        print(f"FAIL exact-bind pin rejected the expected host {args.expect_bind!r}"); ok = False
    for other in ("127.0.0.1", "10.0.0.5"):
        if other == args.expect_bind:
            continue
        try:
            config.assert_exact_bind(other, args.expect_bind); print(f"FAIL exact-bind pin accepted non-expected {other!r}"); ok = False
        except config.ConfigError:
            print(f"ok   exact-bind pin refused non-expected {other!r}")

    # 4. FULL-bundle manifest matches on-disk implementation (every executable module, B-7)
    manifest_path = os.path.join(_HERE, "manifest.json")
    approved = manifest.load_manifest(manifest_path).get("checksums", {})
    actual = manifest.compute_checksums(_HERE)
    missing = [m for m in manifest.IMPL_MODULES if m not in approved]
    if missing:
        print(f"FAIL manifest is missing checksums for {missing}"); ok = False
    if manifest.integrity_ok(approved, actual):
        print(f"ok   manifest matches on-disk implementation ({len(manifest.IMPL_MODULES)} modules)")
    else:
        drift = [m for m in manifest.IMPL_MODULES if approved.get(m) != actual.get(m)]
        print(f"FAIL manifest/implementation checksum mismatch: {drift}"); ok = False

    # 5. authenticity: manifest.json's own hash equals the reviewed-commit value (defeats matched tampering)
    if args.expect_manifest_sha256:
        got = _sha256(manifest_path)
        if got == args.expect_manifest_sha256.lower():
            print("ok   manifest.json authenticity hash matches the reviewed commit")
        else:
            print(f"FAIL manifest.json authenticity mismatch: on-disk {got}"); ok = False
    else:
        print("warn no --expect-manifest-sha256 given; authenticity vs the merged commit NOT verified")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
