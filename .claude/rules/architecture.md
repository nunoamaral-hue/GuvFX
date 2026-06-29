# Rule — Architecture

Scope: read when changing structure, adding services, or altering how data, research,
risk, execution or AI responsibilities interact in the GuvFX platform.

- Prefer **small, additive changes**. Extend the existing Django apps and Next.js
  structure rather than replacing them. No whole-subsystem rewrites in a single packet.
- Keep a clear **separation of responsibilities**: data ingestion/storage, research /
  backtesting, risk, execution, and AI assistance stay in distinct, well-named modules.
  Do not let one concern silently leak into another.
- **No silent architecture replacement.** If a change alters an established pattern
  (auth, API surface, execution path, storage layout), it requires an approved decision
  (ADR / Notion record) before merge — not an in-passing edit.
- **No LLM live-trading authority.** Model-generated output may inform research and
  suggestions only. It must never directly place, size, or approve live or paper orders
  without an explicit human-gated control path.
- **No speculative infrastructure.** Do not add Kafka, Kubernetes, agent frameworks,
  feature stores, message buses, or similar heavy machinery without an approved decision
  *and* a measured, documented need. Default to the simplest thing that works.
- Preserve existing behaviour unless the packet explicitly asks to change it.
