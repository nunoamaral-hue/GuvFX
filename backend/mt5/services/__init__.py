"""
Packet A — Terminal Interaction Service Layer.

Seven services that centralize domain logic for terminal binding
lifecycle, session management, authorization, and cleanup.

Minimal state strings used by the service layer:

    InteractionSession.state:
        "requested"  — session requested, not yet authorized
        "authorized" — authorization validated, pending launch
        "active"     — session running (started_at set)
        "ended"      — session terminated (ended_at set)

    MT5Session.state:
        "launching"  — launch issued, awaiting connection
        "connected"  — adapter connected (connected_at set)
        "suspended"  — temporarily suspended (suspended_at set)
        "ended"      — session ended (ended_at set)
        "failed"     — launch or connection failed

These are the smallest set necessary for service logic and are
documented here as the single source of truth.  They are NOT
enforced via Django TextChoices (per stabilization patch decision).
"""
