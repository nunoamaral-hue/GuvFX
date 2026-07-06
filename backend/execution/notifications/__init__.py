"""TELEGRAM-TRANSPORT-FOUNDATION — the notification transport layer.

Consumes ``NotificationCandidate`` rows and renders them through a pluggable, transport-agnostic
interface. The only adapter is a DRY-RUN Telegram adapter: it RENDERS the message but NEVER
transmits it (no network, no HTTP, no Telegram API, no credentials). Future transports
(Discord/Slack/Email) implement the same ``NotificationTransport`` interface.
"""
