# Customer Telegram product policy

Status: implementation candidate, not deployed
Branch: `feat/customer-telegram-policy-preferences`
Baseline: `5d8c53429fbf873e17aa591cb22572c76750a303`

## Product boundary

Customer Telegram is an outcome and account-status service. It is never a live
signal-distribution service. `TRADE_OPENED`, `SIGNAL_RECEIVED`, `RAW_SIGNAL`,
`TRADE_ENTRY`, and every unknown future event fail closed at both enqueue and
delivery. Prohibited attempts may leave a sanitised `SUPPRESSED` audit row, but
retain no live payload and produce no provider call.

Historical pilot rows and legacy schema fields remain immutable. The old
`Trade` creation observer and trade-open reconciliation source are removed.
WIMs/stakeholder presentation and transport are not imported or changed.

## Eligible events and defaults

| Scope | Event | Default | Additional gate |
|---|---|---:|---|
| Strategy | complete winning result | ON | connected + assignment preference ON + durable final progress |
| Strategy | complete loss | OFF | customer must opt in |
| Strategy | breakeven | OFF | classified as non-winning for beta |
| Strategy | safe TP progress | ON | durable partial leg evidence and `TPn` label only |
| Account/system | optional safe status | ON | account preference ON |
| Essential | connection confirmation | mandatory | internal authority only |
| Onboarding | workspace ready | explicit one-shot | saved intent + canonical readiness + safe GuvFX URL |

The four customer-visible account preferences are `winning_trades`,
`losing_trades`, `tp_progress`, and `system_messages`. There is no live-entry
preference in the API or UI. Legacy `telegram_enabled=false` remains a hidden
fail-closed master gate for compatibility.

## Strategy and language authority

Strategy intent is stored as customer × `StrategyAssignment` with independent
`enabled` and `pending_enable` fields. It never writes strategy activation,
execution mode, jobs, routing, MT5, bridges, or authorization. When Telegram is
not connected, enabling preserves pending intent; a successful secure binding
activates that intent.

The GuvFX app language (`en` or `ja`) is authoritative. Every outbox row
snapshots it at creation. Later language changes affect future rows only.
Proper names and account identifiers remain canonical.

## Safe payload contract

The server uses an event-specific payload allow-list. Final and progress events
may contain only strategy, symbol, realised result/currency, derived outcome,
durable progress, optional completed execution size, safe account display, and
event time. Direction, entry, SL, future TP prices, raw signal identifiers,
worker/node/bridge data, arbitrary payload fields, and diagnostics are removed.

TP progress sends only when durable evidence proves a partial result:
`0 < progress_closed < progress_total`, a `TPn` label exists, and the realised
result is numeric. Final results require `progress_closed == progress_total`.
No durable evidence means suppression.

## Onboarding one-shot

`WorkspaceReadinessNotificationIntent` durably records one request per
workspace/milestone. The notification is eligible only when the owner account
is confirmed, the workspace/account projection matches, and canonical state is
`CONNECTED`, `EXECUTION_READY`, or `EXECUTING`. The only accepted link is the
configured GuvFX origin plus `/onboarding/hosted`.

Pending intent survives page refresh, logout/login, readiness-before-connect,
disconnect, and reconnect. Pending/processing/delivered rows dedupe repeated
observation. A provider-acknowledged row marks the intent fulfilled.

## Presentation

Completed results use a dedicated customer result-card renderer and the
dedicated customer bot. The renderer consumes only the sanitised customer
outbox row. EN/JA WIN and optional LOSS treatments are localized. Text fallback
remains available if local image rendering fails before send.

Visual evidence: [`docs/evidence/customer-telegram-product-policy/`](../evidence/customer-telegram-product-policy/README.md).

## Verification contract

The focused policy suite contains the packet's 26-case matrix covering defaults,
prohibited and unknown events, malformed payloads, strategy/customer/language
isolation, reconnect semantics, onboarding races/dedupe, WIMs separation, card
safety, and no execution mutation. Local full backend/frontend, lint, production
build, customer-app migration drift, governance/parity, secret scan, `make check`,
and adversarial review are green. Exact-head GitHub CI remains required.

## Production activation

This change must not deploy automatically. Production currently runs the
certified pilot transport with the pre-policy revision and therefore remains
product-policy noncompliant until this PR is merged and separately deployed.
Do not broaden beta notification use before the new revision is installed and
verified. No existing narrow event switch can suppress only trade-open delivery.
