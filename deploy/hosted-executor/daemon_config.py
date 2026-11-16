"""Beta Readiness Stream 7C - hosted signed-executor daemon configuration + network bind-guard.

The hosted executor is the runnable host end of the Stream 5 signed provisioning transport. It runs as a
supported Windows service (RULE 1) on the SAME host that runs Customer Zero, so its configuration is
deliberately strict:

 - It binds ONLY the exact expected private/Tailscale management address (a wildcard / public / loopback /
   alternate-NIC bind is refused at startup - it would side-step the interface-scoped firewall rule).
 - Its HMAC signing keyring is its OWN secret (``HOSTED_EXECUTOR_KEYRING`` / ``HOSTED_EXECUTOR_KEY_ID``),
   distinct from ``BETA_AGENT_KEYRING`` and every other estate secret (security RULE 3/6): a missing or
   placeholder value is a startup FAILURE, never a silent fall-back to another service's credential.
 - The envelope PRIVATE keyring (``HOSTED_EXECUTOR_ENC_PRIVKEYS``) is a further DISTINCT scope, used only to
   open the sealed Windows password for PROVISION_IDENTITY; it is never the HMAC key and never logged.
 - The bind port may never collide with Nuno's estate / RDP / the beta agent / the trade bridge.

Self-contained (Django-free). The HMAC signing keyring is loaded FILE-FIRST from one fixed, ACL-protected host
file (``HOSTED_EXECUTOR_KEYRING_FILE`` / ``DEFAULT_KEYRING_FILE``), with the historical process-environment
loader kept ONLY as a documented backward-compatible fallback during migration
(GFX-HOSTED-EXECUTOR-FILE-BASED-KEYRING-144 / ADR-0112). The file source exists because the Windows Service
Control Manager caches the system environment block at BOOT, so a machine-variable keyring rotation is
invisible to a restarted service until the host reboots; reading the keyring from a file the operator rewrites
atomically removes that reboot coupling. The env construction still mirrors the proven
``deploy/beta-agent/config.py``.
"""
from __future__ import annotations

import ipaddress
import json
import os

# The single management interface the live executor is expected to bind. It is the same box that runs Customer
# Zero + the beta agent; ``HOSTED_EXECUTOR_EXPECTED_BIND_HOST`` overrides it for a disposable/other host.
DEFAULT_EXPECTED_BIND_HOST = "100.79.101.19"
DEFAULT_BIND_PORT = 8790

# Ports that belong to Nuno's estate / RDP / the beta agent (:8791) / the trade bridge (:8788) / the VPS agent
# (:8787). The executor must never be pointed at one, even by fat-finger.
FORBIDDEN_BIND_PORTS = frozenset({3389, 8787, 8788, 8791})

# Substrings that betray an un-provisioned placeholder rather than a real secret (mirrors mt5_validate_worker).
_PLACEHOLDER_MARKERS = ("replace", "changeme", "change-me", "example", "placeholder", "<", "${",
                        "scrubbed", "__set_at_install__", "todo", "dummy")

DEFAULT_STATE_DIR = r"C:\GuvFX\hosted\executor-state"
DEFAULT_SCRIPTS_DIR = r"C:\GuvFX\hosted\scripts"

# GFX-144 / ADR-0112: the ONE fixed, ACL-protected file the daemon reads its HMAC keyring from (primary source,
# immune to the Windows SCM boot-time environment cache). Overridable per-host via HOSTED_EXECUTOR_KEYRING_FILE
# (e.g. a disposable cert host or a test sandbox). The operator writes it with the value-blind provisioner
# ``Provision-GuvfxHostedExecutorKeyringFile.ps1`` (inheritance off; only SYSTEM, Administrators and the exact
# service principal may read it).
DEFAULT_KEYRING_FILE = r"C:\GuvFX\hosted\secrets\hosted_executor_keyring.json"


class ConfigError(Exception):
    """Startup configuration is unsafe/incomplete. Message is operator-facing and never contains a secret."""


def _is_private_mgmt_address(host: str) -> bool:
    """True only for a loopback, RFC-1918 private, or Tailscale CGNAT (100.64.0.0/10) address - an interface
    not reachable from the public internet. Wildcards are explicitly excluded."""
    if not host or host in ("0.0.0.0", "::", "*"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
        return False
    if ip.is_loopback or ip.is_private:
        return True
    return ip in ipaddress.ip_network("100.64.0.0/10")   # Tailscale CGNAT is reported global; allow explicitly


def assert_private_bind(host: str) -> None:
    if not _is_private_mgmt_address(host):
        raise ConfigError(f"refusing to bind to a non-private/management address: {host!r}")


def assert_exact_bind(host: str, expected: str) -> None:
    """The running service must bind the ONE expected management address, not merely some private one - a
    loopback / alternate-NIC bind would side-step the interface-scoped firewall rule. Still requires private."""
    assert_private_bind(host)
    if host != expected:
        raise ConfigError(
            f"refusing to bind {host!r}: hosted executor must bind exactly {expected!r} "
            f"(set HOSTED_EXECUTOR_EXPECTED_BIND_HOST to change the expected interface)")


def _looks_placeholder(value: str) -> bool:
    low = value.lower()
    return any(m in low for m in _PLACEHOLDER_MARKERS)


def _validate_keyring_map(keyring, key_id, *, name: str, key_id_name: str) -> tuple[dict, str]:
    """Shared fail-closed structural validation for an HMAC ``{key_id: secret}`` map + its active id, used by
    BOTH the file loader (primary) and the env loader (fallback) so the two paths accept EXACTLY the same
    shape. Missing/malformed/placeholder/active-absent -> ConfigError. Never returns a partial or substituted
    credential. Error messages carry only the key id + a safe reason, NEVER a secret value."""
    if not isinstance(keyring, dict) or not keyring:
        raise ConfigError(f"{name} must be a non-empty JSON object")
    if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in keyring.items()):
        raise ConfigError(f"{name} must map string key ids to non-empty string secrets")
    for k, v in keyring.items():
        if _looks_placeholder(v):
            raise ConfigError(f"{name} entry {k!r} looks like placeholder text, not a real secret")
    if not key_id:
        raise ConfigError(f"{key_id_name} is not configured")
    if key_id not in keyring:
        raise ConfigError(f"{key_id_name} {key_id!r} is not present in {name}")
    return keyring, key_id


def _load_keyring(env: dict, *, name: str, key_id_name: str) -> tuple[dict, str]:
    """ENV loader -- the backward-compatible FALLBACK during the GFX-144 migration (see ``_load_hmac_keyring``).
    Load a JSON ``{key_id: secret}`` map + its active key id from the process environment, fail-closed (RULE 3).
    Missing/malformed/placeholder -> ConfigError. Never returns a partial or substituted credential.

    DEPRECATED as the PRIMARY source: the process env is captured by the Windows Service Control Manager at
    BOOT, so a machine-variable rotation is invisible to a restarted service until the host reboots
    (GFX-HOSTED-EXECUTOR-FILE-BASED-KEYRING-144). The durable source is the ACL-protected file; this path
    remains only so a host not yet file-provisioned keeps working during migration."""
    raw = (env.get(name) or "").strip()
    if not raw:
        raise ConfigError(f"{name} is not configured (provision via the Windows secret store); "
                          f"this service will NOT fall back to another service's credential")
    try:
        keyring = json.loads(raw)
    except (ValueError, TypeError):
        raise ConfigError(f"{name} is not valid JSON")
    key_id = (env.get(key_id_name) or "").strip()
    return _validate_keyring_map(keyring, key_id, name=name, key_id_name=key_id_name)


def _file_is_over_permissive(st) -> bool:
    """Over-permissive guard for a secret file, taking an ``os.stat`` result. On POSIX it refuses any group/other
    permission bit (it asserts the group/other bits are clear; it does NOT assert ownership -- the production
    path is Windows). On Windows the POSIX mode bits are NOT a real ACL (Python reports NTFS files as mode
    0o666), so enforcement is deferred to the NTFS ACL, which the operator provisioner SETS (inheritance off;
    only SYSTEM, Administrators and the exact service principal may read) and INDEPENDENTLY VERIFIES
    (``-Verify``). The daemon does NOT re-assert the NTFS ACL at read time -- that confidentiality guarantee
    holds from provision time; see ADR-0112."""
    if os.name == "nt":
        return False
    import stat as _stat
    return bool(_stat.S_IMODE(st.st_mode) & 0o077)


def _load_keyring_from_file(path: str, *, name: str, key_id_name: str) -> tuple[dict, str]:
    """PRIMARY loader (GFX-144 / ADR-0112): load the HMAC keyring + active id from ONE fixed, ACL-protected
    JSON file ``{"keyring": {key_id: secret}, "key_id": "<active>"}``. Fail-closed and explicit:
      * truly ABSENT      -> FileNotFoundError (the caller ALONE decides the documented env fallback);
      * symlink / present-but-inaccessible / not a regular file / over-permissive / unreadable / non-UTF-8 /
        non-JSON / structurally invalid / active-absent
                          -> ConfigError (a PRESENT-but-bad or inaccessible secret file is a security condition,
                             NEVER a reason to fall back to the possibly-stale env).
    ``os.stat`` (not ``os.path.exists``) distinguishes true absence from a PermissionError/IO error, so an
    inaccessible-but-present file fails closed instead of silently falling back. Never logs the file content;
    ConfigError messages carry only the path + a safe reason, never a secret or raw file bytes."""
    import stat as _stat
    if os.path.islink(path):
        raise ConfigError(f"{name} file {path!r} is a symlink; refusing (the keyring file must be a real regular file)")
    try:
        st = os.stat(path)                        # NOT os.path.exists -- that swallows PermissionError/IO as "absent"
    except FileNotFoundError:
        raise                                     # genuine absence -> the caller does the documented env fallback
    except OSError:
        raise ConfigError(f"{name} file {path!r} is present but could not be accessed")
    if not _stat.S_ISREG(st.st_mode):
        raise ConfigError(f"{name} file is not a regular file: {path!r}")
    if _file_is_over_permissive(st):
        raise ConfigError(f"{name} file {path!r} is over-permissive (group/other access); refusing to read a "
                          f"secret file readable beyond its owner")
    try:
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
    except OSError:
        raise ConfigError(f"{name} file {path!r} could not be read")
    try:
        raw = raw_bytes.decode("utf-8")           # explicit decode so a non-UTF-8 (e.g. UTF-16 BOM) file fails
    except (UnicodeDecodeError, ValueError):      # closed as a clean, secret-free ConfigError (RULE 9/11 trap)
        raise ConfigError(f"{name} file {path!r} is not valid UTF-8")
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        raise ConfigError(f"{name} file {path!r} is not valid JSON")
    if not isinstance(doc, dict):
        raise ConfigError(f"{name} file {path!r} must be a JSON object with 'keyring' and 'key_id'")
    kid = doc.get("key_id")
    kid = kid.strip() if isinstance(kid, str) else ""
    return _validate_keyring_map(doc.get("keyring"), kid, name=name, key_id_name=key_id_name)


def _load_hmac_keyring(env: dict) -> tuple[dict, str, str]:
    """Load the HMAC signing keyring + active id FILE-FIRST, with the env loader as a documented
    backward-compatible fallback DURING MIGRATION (GFX-144 / ADR-0112).

    Why: the Windows Service Control Manager caches the system environment block at BOOT, so a machine-variable
    keyring rotation is invisible to a restarted service until the host reboots. Reading the keyring from a file
    the operator rewrites atomically removes that reboot coupling.

    Semantics (fail-closed): the fixed file (``HOSTED_EXECUTOR_KEYRING_FILE`` or ``DEFAULT_KEYRING_FILE``) is the
    PRIMARY source. If it is ABSENT the service falls back to the process ENV (a host not yet file-provisioned
    keeps working). If it is PRESENT but malformed / over-permissive / invalid the service FAILS CLOSED and does
    NOT fall back -- a present-but-bad secret file is a security condition, not an absence. Returns
    ``(keyring, key_id, source)`` with ``source`` in {'file','env'} (safe, non-secret metadata)."""
    path = (env.get("HOSTED_EXECUTOR_KEYRING_FILE") or DEFAULT_KEYRING_FILE)
    try:
        keyring, key_id = _load_keyring_from_file(
            path, name="HOSTED_EXECUTOR_KEYRING", key_id_name="HOSTED_EXECUTOR_KEY_ID")
        return keyring, key_id, "file"
    except FileNotFoundError:
        keyring, key_id = _load_keyring(env, name="HOSTED_EXECUTOR_KEYRING", key_id_name="HOSTED_EXECUTOR_KEY_ID")
        return keyring, key_id, "env"


def _int(env: dict, name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except (ValueError, TypeError):
        raise ConfigError(f"{name} must be an integer")


def load_config(env: dict | None = None) -> dict:
    """Load the hosted-executor config from the environment. Required: HOSTED_EXECUTOR_KEYRING (JSON),
    HOSTED_EXECUTOR_KEY_ID, HOSTED_EXECUTOR_ENC_PRIVKEYS (JSON). Optional: bind host/port, scripts/state dirs,
    reserved ids, resource limits. Fail-closed on any missing/placeholder secret (RULE 3)."""
    env = env if env is not None else os.environ

    host = (env.get("HOSTED_EXECUTOR_BIND_HOST") or "").strip()
    expected = (env.get("HOSTED_EXECUTOR_EXPECTED_BIND_HOST") or DEFAULT_EXPECTED_BIND_HOST).strip()
    assert_exact_bind(host, expected)

    port = _int(env, "HOSTED_EXECUTOR_BIND_PORT", DEFAULT_BIND_PORT)
    if port in FORBIDDEN_BIND_PORTS:
        raise ConfigError(f"refusing bind port {port}: reserved for the estate/RDP/beta-agent/bridge "
                          f"({sorted(FORBIDDEN_BIND_PORTS)})")
    if not (1 <= port <= 65535):
        raise ConfigError(f"bind port {port} out of range")

    # GFX-144 / ADR-0112: FILE-FIRST (durable; immune to the Windows SCM boot-time env cache); env fallback only
    # when the file is ABSENT; a present-but-bad file fails closed (never a silent fallback to a stale env).
    keyring, key_id, keyring_source = _load_hmac_keyring(env)

    # Envelope PRIVATE keyring (for opening the sealed Windows password on PROVISION_IDENTITY). A DISTINCT
    # scope from the HMAC keyring; presence is required at startup so a credentialed op never fails mid-flight
    # with a config error the operator could have caught at boot. The individual key is selected per-request by
    # the envelope's own key_id (envelope_open); here we only assert the map parses and is non-empty.
    enc_raw = (env.get("HOSTED_EXECUTOR_ENC_PRIVKEYS") or "").strip()
    if not enc_raw:
        raise ConfigError("HOSTED_EXECUTOR_ENC_PRIVKEYS is not configured (envelope private keyring)")
    try:
        enc_privkeys = json.loads(enc_raw)
    except (ValueError, TypeError):
        raise ConfigError("HOSTED_EXECUTOR_ENC_PRIVKEYS is not valid JSON")
    if not isinstance(enc_privkeys, dict) or not enc_privkeys:
        raise ConfigError("HOSTED_EXECUTOR_ENC_PRIVKEYS must be a non-empty JSON object")
    # Symmetric with the HMAC keyring (RULE 3): type + placeholder gate at BOOT so an un-substituted install
    # placeholder is caught here, not at the first PROVISION_IDENTITY request.
    if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in enc_privkeys.items()):
        raise ConfigError("HOSTED_EXECUTOR_ENC_PRIVKEYS must map string key ids to non-empty string keys")
    for k, v in enc_privkeys.items():
        if _looks_placeholder(v):
            raise ConfigError(f"HOSTED_EXECUTOR_ENC_PRIVKEYS entry {k!r} looks like placeholder text, not a real key")

    state_dir = (env.get("HOSTED_EXECUTOR_STATE_DIR") or DEFAULT_STATE_DIR)
    scripts_dir = (env.get("HOSTED_EXECUTOR_SCRIPTS_DIR") or DEFAULT_SCRIPTS_DIR)

    return {
        "bind_host": host,
        "expected_bind_host": expected,
        "bind_port": port,
        "keyring": keyring,
        "key_id": key_id,
        "keyring_source": keyring_source,     # safe metadata: 'file' (primary) or 'env' (migration fallback)
        "enc_privkeys_raw": enc_raw,          # passed to envelope_open; never logged
        "scripts_dir": scripts_dir,
        "state_db": env.get("HOSTED_EXECUTOR_STATE_DB", state_dir + r"\executor-state.sqlite"),
        "log_dir": env.get("HOSTED_EXECUTOR_LOG_DIR", state_dir + r"\logs"),
        "reserved_account_ids": (env.get("HOSTED_EXECUTOR_RESERVED_ACCOUNT_IDS")
                                 if env.get("HOSTED_EXECUTOR_RESERVED_ACCOUNT_IDS") is not None else None),
        "powershell": env.get("HOSTED_EXECUTOR_POWERSHELL", "powershell"),
        "primitive_timeout_s": float(env.get("HOSTED_EXECUTOR_PRIMITIVE_TIMEOUT_S", "600")),
        "max_output_bytes": _int(env, "HOSTED_EXECUTOR_MAX_OUTPUT_BYTES", 65536),
        "max_body_bytes": _int(env, "HOSTED_EXECUTOR_MAX_BODY_BYTES", 65536),
        "max_connections": _int(env, "HOSTED_EXECUTOR_MAX_CONNECTIONS", 8),
        "request_timeout_s": float(env.get("HOSTED_EXECUTOR_REQUEST_TIMEOUT_S", "10")),
        "drain_timeout_s": float(env.get("HOSTED_EXECUTOR_DRAIN_TIMEOUT_S", "630")),
        "max_skew_seconds": _int(env, "HOSTED_EXECUTOR_MAX_SKEW_SECONDS", 30),
    }
