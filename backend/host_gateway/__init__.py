"""host_gateway — per-host authenticated MONITORING gateway reference component (ADR-0075, DARK).

GFX-MT5-BRIDGE-SCALE-001, Phase 4 (gateway half). A dependency-free (stdlib-only), NON-Django package: it
is NOT an installed app, is never imported by the Django runtime, and MUST NOT be installed or run on a
Windows host by this packet. It is the repository artifact that defines — and lets CI exercise in-process,
without sockets or a host — the gateway's authentication boundary, its allow-listed read-only operation
set, its route-by-registered-identity rule (never an arbitrary host:port), its rejection of unknown /
stale / mismatched / tombstoned registrations, its replay protection, and its identity attestation.

The gateway exists so a whole physical host is reachable on ONE backend-facing port: adding a monitored
account registers a local bridge behind the gateway and needs no new Tailscale grant. The gateway never
places an order, never logs in, and exposes read-only monitoring methods only.
"""
from host_gateway.gateway_core import (  # noqa: F401
    ALLOWED_OPS,
    GatewayError,
    MonitoringGateway,
    PROTOCOL_VERSION,
)
from host_gateway.registry import BridgeRegistry, RegisteredBridge  # noqa: F401
