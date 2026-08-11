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

Self-contained (Django-free): loaded from the process environment via an approved Windows secret mechanism at
deploy time, mirroring the proven ``deploy/beta-agent/config.py`` construction.
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


def _load_keyring(env: dict, *, name: str, key_id_name: str) -> tuple[dict, str]:
    """Load a JSON ``{key_id: secret}`` map + its active key id, fail-closed (RULE 3). Missing/malformed/
    placeholder -> ConfigError. Never returns a partial or substituted credential."""
    raw = (env.get(name) or "").strip()
    if not raw:
        raise ConfigError(f"{name} is not configured (provision via the Windows secret store); "
                          f"this service will NOT fall back to another service's credential")
    try:
        keyring = json.loads(raw)
    except (ValueError, TypeError):
        raise ConfigError(f"{name} is not valid JSON")
    if not isinstance(keyring, dict) or not keyring:
        raise ConfigError(f"{name} must be a non-empty JSON object")
    if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in keyring.items()):
        raise ConfigError(f"{name} must map string key ids to non-empty string secrets")
    for k, v in keyring.items():
        if _looks_placeholder(v):
            raise ConfigError(f"{name} entry {k!r} looks like placeholder text, not a real secret")
    key_id = (env.get(key_id_name) or "").strip()
    if not key_id:
        raise ConfigError(f"{key_id_name} is not configured")
    if key_id not in keyring:
        raise ConfigError(f"{key_id_name} {key_id!r} is not present in {name}")
    return keyring, key_id


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

    keyring, key_id = _load_keyring(env, name="HOSTED_EXECUTOR_KEYRING", key_id_name="HOSTED_EXECUTOR_KEY_ID")

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
