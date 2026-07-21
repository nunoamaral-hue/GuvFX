"""CVM-Inc-3 B2 — agent configuration + network bind-guard.

The agent binds ONLY to the configured Tailscale/private-management address (requirement 1/5). Startup
fails hard if configured as a wildcard / public / non-private address. Secrets (the signing keyring) are
loaded from the environment / an approved Windows secret mechanism — NEVER from Git.
"""
import ipaddress
import json
import os


class ConfigError(Exception):
    pass


def _is_private_mgmt_address(host: str) -> bool:
    """True only for a loopback, RFC-1918 private, or Tailscale CGNAT (100.64.0.0/10) address —
    i.e. an interface that is not reachable from the public internet. Wildcards are explicitly excluded."""
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
    # Tailscale CGNAT range 100.64.0.0/10 is reported as global by ipaddress; allow it explicitly.
    return ip in ipaddress.ip_network("100.64.0.0/10")


def assert_private_bind(host: str) -> None:
    """Refuse to start unless the bind host is a private/Tailscale management address."""
    if not _is_private_mgmt_address(host):
        raise ConfigError(f"refusing to bind to a non-private/management address: {host!r}")


def load_config(env: dict | None = None) -> dict:
    """Load agent config from the environment. Required: BETA_AGENT_BIND_HOST, BETA_AGENT_BIND_PORT,
    BETA_AGENT_KEYRING (JSON), BETA_AGENT_KEY_ID. Optional base/tombstone/state/manifest paths."""
    env = env if env is not None else os.environ
    host = env.get("BETA_AGENT_BIND_HOST", "")
    assert_private_bind(host)
    keyring_raw = env.get("BETA_AGENT_KEYRING", "")
    keyring = json.loads(keyring_raw) if keyring_raw else {}
    if not keyring or not env.get("BETA_AGENT_KEY_ID"):
        raise ConfigError("missing signing keyring / key id (provision via the Windows secret store)")
    return {
        "bind_host": host,
        "bind_port": int(env.get("BETA_AGENT_BIND_PORT", "8791")),
        "keyring": keyring,
        "key_id": env["BETA_AGENT_KEY_ID"],
        "beta_root": env.get("BETA_AGENT_ROOT", r"C:\GuvFX\beta\accounts"),
        "tombstone_base": env.get("BETA_AGENT_TOMBSTONE", r"C:\GuvFX\beta\tombstones"),
        "state_db": env.get("BETA_AGENT_STATE_DB", r"C:\GuvFX\beta\agent\state.sqlite"),
        "manifest_path": env.get("BETA_AGENT_MANIFEST", ""),
    }
