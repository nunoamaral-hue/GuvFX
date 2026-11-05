"""trade_copier.emitter — the host-side Copy Trading emitter application (ADR-0076, repository-only / DARK).

Django-free, host-runnable modules that wrap the ADR-0074 signing library (``trade_copier.bridge_emitter``) into
an installable Windows emitter: a narrow READ-ONLY MT5 adapter, a one-shot identity-attestation mode, and a
gated observe/deliver runner. Nothing here places, modifies, recovers or closes a broker order; all gates default
OFF; it is NOT installed or enabled by this packet.
"""
