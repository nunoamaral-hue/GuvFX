"""CVM-Inc-3 B2/B3P-1 — agent configuration + network bind-guard.

The agent binds ONLY to the configured Tailscale/private-management address (requirement 1/5). Startup
fails hard if configured as a wildcard / public / non-private address. Secrets (the signing keyring) are
loaded from the environment / an approved Windows secret mechanism — NEVER from Git.

B3P-1 hardening (verification B-9): the LIVE service additionally pins the bind host to the EXACT expected
management address (not merely "some private address") and refuses a bind port that collides with Nuno's
services. The broad ``assert_private_bind`` predicate is retained ONLY for offline validation.
"""
import hashlib
import ipaddress
import json
import os

from lib.mgmt_agent_core import EXECUTION_MODEL_SLOT_POOL, EXECUTION_MODEL_UUID_DIR
from win_primitives import BETA_SLOTS_ROOT

#: Kept here rather than imported from pool_op_impls so config has no dependency on the lifecycle layer.
#: Asserted equal to the implementation's values by ``tests_pool_ops``.
LAUNCH_SETTLE_ATTEMPTS, STOP_SETTLE_ATTEMPTS, SETTLE_POLL_SECONDS = 20, 30, 1.0

# The single management interface the live agent is expected to bind (verification B-9). ``load_config``
# pins the live bind to this exact address; ``BETA_AGENT_EXPECTED_BIND_HOST`` overrides it for a different box.
DEFAULT_EXPECTED_BIND_HOST = "100.79.101.19"

# Ports that belong to Nuno's estate / RDP — the agent must never be pointed at one, even by fat-finger.
FORBIDDEN_BIND_PORTS = frozenset({8787, 8788, 3389})


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
    """Refuse to start unless the bind host is a private/Tailscale management address. Broad predicate —
    used by offline validation. The LIVE service uses the stricter ``assert_exact_bind`` (B-9)."""
    if not _is_private_mgmt_address(host):
        raise ConfigError(f"refusing to bind to a non-private/management address: {host!r}")


def assert_exact_bind(host: str, expected: str) -> None:
    """LIVE bind pin (verification B-9): the running service must bind the ONE expected management address,
    not merely some private one — a loopback / alternate-NIC bind would side-step the interface-scoped
    firewall rule. Still requires the address to be private (defense-in-depth)."""
    assert_private_bind(host)
    if host != expected:
        raise ConfigError(
            f"refusing to bind {host!r}: live agent must bind exactly {expected!r} "
            f"(set BETA_AGENT_EXPECTED_BIND_HOST to change the expected interface)")


#: The seven fields a task approval must pin. Same set as ``occupancy.TASK_IDENTITY_FIELDS``; asserted
#: equal by the tests so the two can never drift apart.
APPROVED_TASK_FIELDS = ("task_name", "run_as_identity", "executable", "working_directory", "arguments",
                        "logon_type", "run_level", "enabled")


def _load_approved_tasks(path: str):
    """Load the operator's approved task definitions, and refuse anything incomplete.

    Fails closed in three distinct ways because each means something different: the file is missing
    (deployment fault), the file is malformed (tampering or a bad edit), or a definition omits a pinned
    field (an approval that does not actually approve anything). Returned alongside a digest of the file so
    a change to the approvals themselves is visible in the evidence.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ConfigError(f"approved task definitions unreadable: {path!r}") from exc
    try:
        # utf-8-sig, not utf-8: a BOM is a benign encoding default on Windows, and rejecting it here would
        # report a routine editor save as "tampering". The installer writes BOM-free; this is the safety net.
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConfigError("approved task definitions are not valid JSON") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ConfigError("approved task definitions are empty")
    for name, definition in parsed.items():
        if not isinstance(definition, dict):
            raise ConfigError(f"approved task definition for {name!r} is not an object")
        missing = [f for f in APPROVED_TASK_FIELDS if f not in definition]
        if missing:
            raise ConfigError(f"approved task definition for {name!r} omits {','.join(missing)}")
    return parsed, hashlib.sha256(raw).hexdigest()[:16]


def load_config(env: dict | None = None) -> dict:
    """Load agent config from the environment. Required: BETA_AGENT_BIND_HOST, BETA_AGENT_BIND_PORT,
    BETA_AGENT_KEYRING (JSON), BETA_AGENT_KEY_ID. Optional base/tombstone/state/manifest paths.

    The bind is pinned to the EXACT expected management address (B-9) and the port is refused if it collides
    with Nuno's estate / RDP (B3P-1)."""
    env = env if env is not None else os.environ
    host = env.get("BETA_AGENT_BIND_HOST", "")
    expected = env.get("BETA_AGENT_EXPECTED_BIND_HOST", DEFAULT_EXPECTED_BIND_HOST)
    assert_exact_bind(host, expected)
    port = int(env.get("BETA_AGENT_BIND_PORT", "8791"))
    if port in FORBIDDEN_BIND_PORTS:
        raise ConfigError(f"refusing bind port {port}: reserved for Nuno's estate/RDP ({sorted(FORBIDDEN_BIND_PORTS)})")
    keyring_raw = env.get("BETA_AGENT_KEYRING", "")
    keyring = json.loads(keyring_raw) if keyring_raw else {}
    if not keyring or not env.get("BETA_AGENT_KEY_ID"):
        raise ConfigError("missing signing keyring / key id (provision via the Windows secret store)")
    # State + logs live UNDER a dedicated state dir, SEPARATE from the code dir, so an update/rollback copy
    # over the code dir can never clobber the durable nonce/idempotency store or logs (verification, §8/§11).
    state_dir = env.get("BETA_AGENT_STATE_DIR", r"C:\GuvFX\beta\agent-state")
    model = env.get("BETA_AGENT_EXECUTION_MODEL", EXECUTION_MODEL_UUID_DIR)
    if model not in (EXECUTION_MODEL_UUID_DIR, EXECUTION_MODEL_SLOT_POOL):
        raise ConfigError(f"unknown execution model {model!r}")
    pool_size = int(env.get("BETA_AGENT_SLOT_POOL_SIZE", "0"))
    golden_digest = env.get("BETA_AGENT_GOLDEN_DIGEST", "")
    approved_tasks_path = env.get("BETA_AGENT_APPROVED_TASKS", "")
    approved_tasks, approved_tasks_digest = {}, ""
    if model == EXECUTION_MODEL_SLOT_POOL:
        # The pool cannot be inferred. A pool of zero would silently accept every runtime and then exhaust;
        # an unset golden digest would make the stage-copy integrity check compare against "".
        if pool_size < 1:
            raise ConfigError("slot_pool execution model requires BETA_AGENT_SLOT_POOL_SIZE >= 1")
        if not golden_digest:
            raise ConfigError("slot_pool execution model requires BETA_AGENT_GOLDEN_DIGEST")
        if not env.get("BETA_AGENT_GOLDEN_MANIFEST_VERSION"):
            # Left empty, the stage-copy pre-check would compare "" == "" and pass on an unversioned image.
            raise ConfigError("slot_pool execution model requires BETA_AGENT_GOLDEN_MANIFEST_VERSION")
        if not approved_tasks_path:
            raise ConfigError("slot_pool execution model requires BETA_AGENT_APPROVED_TASKS")
        slots_root = env.get("BETA_AGENT_SLOTS_ROOT", BETA_SLOTS_ROOT)
        if slots_root != BETA_SLOTS_ROOT:
            # The knob is honoured for the containment base but the primitives derive every slot path from
            # the module constant, so any other value makes every operation fail path_escape at RUNTIME.
            # Refuse at STARTUP instead of shipping a config that cannot work.
            raise ConfigError(
                f"BETA_AGENT_SLOTS_ROOT must equal {BETA_SLOTS_ROOT!r}: slot paths are derived from the "
                f"fixed namespace, not from configuration")
        settle = max(LAUNCH_SETTLE_ATTEMPTS, STOP_SETTLE_ATTEMPTS) * SETTLE_POLL_SECONDS
        if float(env.get("BETA_AGENT_DRAIN_TIMEOUT_S", "20")) <= settle:
            # A settle window longer than the drain budget guarantees that a service stop during a mutation
            # force-kills it mid-stage — the exact outcome the drain exists to prevent.
            raise ConfigError(
                f"BETA_AGENT_DRAIN_TIMEOUT_S must exceed the settle window ({settle:.0f}s)")
        approved_tasks, approved_tasks_digest = _load_approved_tasks(approved_tasks_path)
    return {
        "bind_host": host,
        "expected_bind_host": expected,
        "bind_port": port,
        "keyring": keyring,
        "key_id": env["BETA_AGENT_KEY_ID"],
        "beta_root": env.get("BETA_AGENT_ROOT", r"C:\GuvFX\beta\accounts"),
        "tombstone_base": env.get("BETA_AGENT_TOMBSTONE", r"C:\GuvFX\beta\tombstones"),
        "state_db": env.get("BETA_AGENT_STATE_DB", state_dir + r"\state.sqlite"),
        "log_dir": env.get("BETA_AGENT_LOG_DIR", state_dir + r"\logs"),
        "manifest_path": env.get("BETA_AGENT_MANIFEST", ""),
        "max_body_bytes": int(env.get("BETA_AGENT_MAX_BODY_BYTES", "16384")),
        "max_connections": int(env.get("BETA_AGENT_MAX_CONNECTIONS", "16")),
        "request_timeout_s": float(env.get("BETA_AGENT_REQUEST_TIMEOUT_S", "10")),
        "drain_timeout_s": float(env.get("BETA_AGENT_DRAIN_TIMEOUT_S", "20")),
        # ── B3P-2 execution model ──
        # EXPLICIT, with no silent fallback: selecting the slot pool without the settings it needs is a
        # startup failure, not a quiet reversion to the B2 uuid-directory layout (security RULE 3's
        # reasoning applied to configuration).
        "execution_model": model,
        "slot_pool_size": pool_size,
        "slots_root": env.get("BETA_AGENT_SLOTS_ROOT", r"C:\GuvFX\beta\slots"),
        "golden_dir": env.get("BETA_AGENT_GOLDEN_DIR", r"C:\GuvFX\beta\golden"),
        "golden_digest": golden_digest,
        "golden_manifest_version": env.get("BETA_AGENT_GOLDEN_MANIFEST_VERSION", ""),
        "slot_db": env.get("BETA_AGENT_SLOT_DB", state_dir + r"\slots.sqlite"),
        "approved_tasks_path": approved_tasks_path,
        "approved_tasks": approved_tasks,
        "approved_tasks_digest": approved_tasks_digest,
    }
