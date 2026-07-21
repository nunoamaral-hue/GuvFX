"""CVM-Inc-3 B2 — offline validation: prove the bind-guard refuses public binds and the manifest matches
the on-disk implementation. Safe to run anywhere; performs NO Windows side-effects, starts NO server."""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "lib"))
import config, manifest

def main():
    ok = True
    for bad in ("0.0.0.0", "::", "8.8.8.8", ""):
        try:
            config.assert_private_bind(bad); print(f"FAIL bind-guard accepted {bad!r}"); ok = False
        except config.ConfigError:
            print(f"ok   bind-guard refused {bad!r}")
    for good in ("127.0.0.1", "100.79.101.19", "10.0.0.5"):
        try:
            config.assert_private_bind(good); print(f"ok   bind-guard allowed {good!r}")
        except config.ConfigError:
            print(f"FAIL bind-guard refused private {good!r}"); ok = False
    approved = manifest.load_manifest(os.path.join(_HERE, "manifest.json")).get("checksums", {})
    actual = manifest.compute_checksums(_HERE)
    if manifest.integrity_ok(approved, actual):
        print("ok   manifest matches on-disk implementation")
    else:
        print("FAIL manifest/implementation checksum mismatch"); ok = False
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
