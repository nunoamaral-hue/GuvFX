"""GuvFX Copy Trading host emitter launcher (ADR-0076) - repository artifact, DARK.

Thin entrypoint invoked by the (DISABLED) scheduled task or by the operator attestation script. It adds the
vendored emitter library subset to sys.path and delegates to trade_copier.emitter.app.main. No Django, no DB,
no broker; it only ever runs the gated emitter modes (attest / observe-once / run / status), all default-OFF.

Packaging (see docs/COPIER_HOST_EMITTER_INSTALL_AND_ACCEPTANCE.md): at bundle-build time the ``lib/`` directory
is populated with the pinned source subset listed in MANIFEST.json (trade_copier/emitter/*, bridge_emitter.py,
event_ingestion.py, diff.py, revisions.py and their __init__ shims). This file carries no secret.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from trade_copier.emitter.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
