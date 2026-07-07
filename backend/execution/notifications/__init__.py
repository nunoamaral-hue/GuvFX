"""TELEGRAM-TRANSPORT-FOUNDATION — the notification transport layer.

Consumes ``NotificationCandidate`` rows and renders them through a pluggable, transport-agnostic
interface. Two adapters implement ``NotificationTransport``: ``TelegramDryRunTransport`` (renders
but NEVER transmits — the DEFAULT) and ``RealTelegramTransport`` (the single network-capable
surface, in ``real_transport.py``, disabled by default — used only when dispatch is enabled AND
that transport is explicitly selected). Every OTHER file in this package stays network-free; the
boundary test enforces that split. Future transports (Discord/Slack/Email) implement the same
interface.
"""
