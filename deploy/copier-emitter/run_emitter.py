"""GuvFX Copy Trading host emitter launcher (ADR-0077; standing key loader added GFX-...-148, ADR-0114) - DARK.

Thin entrypoint invoked by the (DISABLED) scheduled task or by the operator attestation script. It adds the
vendored emitter library subset to sys.path and delegates to trade_copier.emitter.app.main. No Django, no DB,
no broker; it only ever runs the gated emitter modes (attest / observe-once / run / status), all default-OFF.

Standing ingest signing key (GFX-...-148): the emitter signs with the key named by the config's ``ingest_key_env``
(default COPIER_INGEST_HMAC_KEY). A scheduled task does not inherit a shell env, so this launcher, if that env var
is NOT already set, loads it ONCE at startup from a protected host-local file next to the --config file
(``<config dir>/ingest.key``) or from an explicit ``--key-file PATH``. The value is injected PROCESS-LOCAL only and
is NEVER written to argv, logs, the event log, or any result. A missing/short key leaves the env unset and the
emitter fails closed (``resolve_secret`` returns '' -> OFF). The protected file is placed/verified/removed only by
the reviewed human-operated provisioner (operator/Provision-GuvfxCopierEmitterKey.ps1); this launcher never
writes it.

Packaging (see docs/COPIER_HOST_EMITTER_INSTALL_AND_ACCEPTANCE.md): at bundle-build time the ``lib/`` directory
is populated with the pinned source subset listed in MANIFEST.json. This file carries no secret.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(_HERE, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

_MIN_SECRET_LEN = 32


def _arg_value(argv, name):
    """Return the value following ``name`` in argv, or None. Pure/secret-free."""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _strip_arg(argv, name):
    """Return argv with ``name VALUE`` removed (so it is not forwarded to the emitter argparse)."""
    if name not in argv:
        return list(argv)
    i = argv.index(name)
    return [a for j, a in enumerate(argv) if j not in (i, i + 1)]


def _load_standing_key(argv):
    """If the configured ingest key env var is unset, load it ONCE from the protected standing key file (explicit
    --key-file, else <config dir>/ingest.key). PROCESS-LOCAL injection only; never logs or persists the value.
    Returns argv with any --key-file removed. Fail-silent: on any error the env is left unset (emitter fails
    closed via resolve_secret)."""
    key_env = "COPIER_INGEST_HMAC_KEY"
    config_path = _arg_value(argv, "--config")
    # Resolve the real env-var NAME from the config (never a secret), best-effort.
    if config_path:
        try:
            from trade_copier.emitter.config import load_config_file
            cfg = load_config_file(config_path)
            key_env = (getattr(cfg, "ingest_key_env", "") or key_env)
        except Exception:  # noqa: BLE001 - config errors are handled by the emitter itself downstream
            pass
    out_argv = _strip_arg(argv, "--key-file")
    # Already provided in the environment (e.g. a one-shot wrapper set it) -> never override.
    if (os.environ.get(key_env) or "").strip():
        return out_argv
    key_file = _arg_value(argv, "--key-file")
    if not key_file and config_path:
        key_file = os.path.join(os.path.dirname(os.path.abspath(config_path)), "ingest.key")
    if not key_file or not os.path.isfile(key_file):
        return out_argv
    try:
        with open(key_file, "r", encoding="utf-8") as fh:
            val = fh.read().strip()
        if val and len(val) >= _MIN_SECRET_LEN:
            os.environ[key_env] = val   # process-local; never logged or written back
    except OSError:
        pass
    return out_argv


from trade_copier.emitter.app import main  # noqa: E402

if __name__ == "__main__":
    _argv = _load_standing_key(sys.argv[1:])
    raise SystemExit(main(_argv))
