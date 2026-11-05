"""trade_copier.emitter.config — account-scoped, secret-free emitter configuration + host-side gates (ADR-0077).

Django-free (host-runnable). The config holds the emitter's IDENTITY and the NAMES of the environment variables
that carry secrets — never a secret value. Every gate defaults OFF and a missing/malformed value evaluates OFF
(fail closed). The service cannot start without a complete identity (``require_complete_identity``), so a
half-configured emitter never runs.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

_TRUTHY = ("1", "true", "yes", "on")


class ConfigError(Exception):
    """A malformed/incomplete emitter configuration. Raised so the service refuses to start."""


def _gate(value) -> bool:
    """Parse a gate value. Anything not explicitly truthy — including None/malformed — is OFF (fail closed)."""
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class EmitterConfig:
    """One emitter instance belongs to exactly one immutable GuvFX account. Account/host/server/terminal identity
    is fixed here and is NEVER taken from an incoming event. Secrets are referenced by env-var NAME only."""

    # ─── immutable identity (fixed at install; never overridable by an incoming payload) ───
    account_id: int
    broker_login: str          # the EXPECTED broker login (== account_number); not a secret
    broker_server: str
    host_id: str
    bridge_base_url: str       # the LOCAL (loopback) read-only bridge, e.g. http://127.0.0.1:8806
    ingest_url: str            # backend signed ingestion endpoint

    key_id: str = "copier-ingest"
    build_sha: str = ""
    terminal_fingerprint: str = ""
    bridge_id: str = ""

    # ─── secret ENV-VAR NAMES (never the secret values) ───
    ingest_key_env: str = "COPIER_INGEST_HMAC_KEY"       # HMAC key for ingestion + attestation signing
    agent_token_env: str = "GUVFX_AGENT_TOKEN"           # the local bridge's read-only agent token

    # ─── durable spool ───
    spool_path: str = ""                                 # empty → in-memory (tests); a path → durable FileSpool
    max_pending: int = 1000
    max_attempts: int = 5

    # ─── cadence (capacity-safe defaults; fast loop DISABLED until explicitly configured > 0) ───
    fast_interval_seconds: float = 0.0
    min_interval_seconds: float = 1.0
    reconcile_interval_seconds: float = 60.0
    stale_expiry_seconds: float = 300.0
    failure_backoff_seconds: float = 5.0
    max_backoff_seconds: float = 60.0
    jitter_seconds: float = 0.5

    # ─── gates (ALL default OFF; malformed → OFF; kept separate from projection/dispatch/alert/execution) ───
    attestation_allowed: bool = False
    observation_allowed: bool = False
    delivery_allowed: bool = False

    def require_complete_identity(self) -> None:
        """Fail closed unless the full immutable identity is present + valid (Phase-5: no start without it)."""
        if not isinstance(self.account_id, int) or self.account_id <= 1:
            raise ConfigError("account_id must be an integer > 1 (reserved/Customer-Zero ids refused)")
        for name in ("broker_login", "broker_server", "host_id", "bridge_base_url", "ingest_url"):
            if not str(getattr(self, name) or "").strip():
                raise ConfigError(f"missing required identity field: {name}")
        if not str(self.bridge_base_url).startswith(("http://127.0.0.1", "http://localhost", "https://127.0.0.1",
                                                     "https://localhost")):
            raise ConfigError("bridge_base_url must be a loopback address (the emitter reads the LOCAL bridge)")

    def effective_fast_interval(self) -> float:
        """The fast loop is OFF (0) unless configured, and is floored at ``min_interval_seconds`` when on."""
        fi = float(self.fast_interval_seconds or 0.0)
        if fi <= 0:
            return 0.0
        return max(fi, float(self.min_interval_seconds or 1.0))

    def config_digest(self) -> str:
        """Stable fingerprint of the whole (secret-free) config — bound into the attestation envelope."""
        return hashlib.sha256(
            json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def identity_fingerprints(self) -> dict:
        """Secret-safe identity fingerprints for diagnostics/attestation (no raw login, no secret)."""
        return {
            "account_id": int(self.account_id),
            "broker_login_fp": hashlib.sha256(str(self.broker_login).strip().encode()).hexdigest()[:32],
            "broker_server": str(self.broker_server),
            "host_id": str(self.host_id),
            "terminal_fp": str(self.terminal_fingerprint),
            "bridge_id": str(self.bridge_id),
            "build_sha": str(self.build_sha),
            "config_digest": self.config_digest(),
        }


def load_config(data: dict) -> EmitterConfig:
    """Build an ``EmitterConfig`` from a plain dict (parsed from the account-scoped config file or a test).

    Identity/scalars are read strictly; gates use the fail-closed ``_gate`` parser (missing/malformed → OFF).
    Unknown keys are ignored. Raises ``ConfigError`` on a type fault so the service refuses to start."""
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")

    def _s(key, default=""):
        v = data.get(key, default)
        return default if v is None else str(v)

    def _f(key, default):
        v = data.get(key, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be numeric")

    def _i(key, default):
        v = data.get(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be an integer")

    try:
        account_id = int(data["account_id"])
    except (KeyError, TypeError, ValueError):
        raise ConfigError("account_id is required and must be an integer")

    cfg = EmitterConfig(
        account_id=account_id,
        broker_login=_s("broker_login"),
        broker_server=_s("broker_server"),
        host_id=_s("host_id"),
        bridge_base_url=_s("bridge_base_url"),
        ingest_url=_s("ingest_url"),
        key_id=_s("key_id", "copier-ingest") or "copier-ingest",
        build_sha=_s("build_sha"),
        terminal_fingerprint=_s("terminal_fingerprint"),
        bridge_id=_s("bridge_id"),
        ingest_key_env=_s("ingest_key_env", "COPIER_INGEST_HMAC_KEY") or "COPIER_INGEST_HMAC_KEY",
        agent_token_env=_s("agent_token_env", "GUVFX_AGENT_TOKEN") or "GUVFX_AGENT_TOKEN",
        spool_path=_s("spool_path"),
        max_pending=_i("max_pending", 1000),
        max_attempts=_i("max_attempts", 5),
        fast_interval_seconds=_f("fast_interval_seconds", 0.0),
        min_interval_seconds=_f("min_interval_seconds", 1.0),
        reconcile_interval_seconds=_f("reconcile_interval_seconds", 60.0),
        stale_expiry_seconds=_f("stale_expiry_seconds", 300.0),
        failure_backoff_seconds=_f("failure_backoff_seconds", 5.0),
        max_backoff_seconds=_f("max_backoff_seconds", 60.0),
        jitter_seconds=_f("jitter_seconds", 0.5),
        attestation_allowed=_gate(data.get("attestation_allowed")),
        observation_allowed=_gate(data.get("observation_allowed")),
        delivery_allowed=_gate(data.get("delivery_allowed")),
    )
    return cfg


def load_config_file(path: str) -> EmitterConfig:
    """Load + parse the account-scoped JSON config file. A read/parse fault fails closed (ConfigError)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        raise ConfigError(f"config file unreadable/invalid: {type(e).__name__}")
    return load_config(data)
