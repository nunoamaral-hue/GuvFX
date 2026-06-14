# intelligence — GuvFX Signal Intelligence Producer (Phase 7A)

GuvFX-side intelligence **producer** (ADR-009: *GuvFX creates intelligence,
WIMS consumes it*). This app packages an existing **Wayond signal** into an
immutable **Signal Intelligence Envelope** and delivers it into the existing
WIMS consumption pipeline. Wayond is the only source for this phase.

This phase is **packaging + delivery, not signal generation** — the producer
never invents a signal, it wraps one it is given.

## Flow

```
Wayond Signal (external)
  → Signal Intelligence Envelope (immutable, version=1.0)
  → WIMS Consumption Contract (source_type=WAYOND)
  → existing WIMS pipeline (context → content → review → publish)
```

## Components (no persistence models)

- `envelope.py` — `SignalIntelligenceEnvelope` + `SignalPayload`, **frozen
  dataclasses** (immutable after creation). Header: `intelligence_id`,
  `intelligence_type=SIGNAL`, `version="1.0"`, `source`, `timestamp`,
  `confidence`, `summary`, `structured_payload`. No registry.
- `producer.py` — `SignalIntelligenceProducer.produce(signal)` → envelope (pure;
  no I/O/persistence/audit).
- `delivery.py` — `ingest_wayond_signal(signal, actor)` runs the full audited
  path and returns `(envelope, contract)`. `deliver(envelope, actor)` is the
  GuvFX→WIMS boundary crossing; it consumes the envelope via the **unchanged**
  `wims.services.create_contract`.

The envelope is **transient** — it is never persisted as its own object. The
first persisted artefact is the WIMS `ConsumptionContract`.

## Audit (reuses existing WIMS capability)

No new audit framework, no `IntelligenceAuditRecord`. Four lifecycle events are
recorded via the existing `wims` audit (`AuditEvent` + `record_audit_ref`),
correlated by `intelligence_id`:

```
SIGNAL_RECEIVED → ENVELOPE_CREATED → ENVELOPE_DELIVERED → ENVELOPE_CONSUMED
```

(plus WIMS' own `CONTRACT_CREATED` at consumption).

## Dependency direction (ADR-009)

`intelligence` (GuvFX) → `wims` (consumer). WIMS never imports `intelligence`.
`intelligence` persists **no** models; WIMS persists no Trade/Position/Deal/
Execution/MT5/Broker object. Both are asserted by the demo and tests.

## Demonstrate / test

Runs on the isolated SQLite settings shim (no Postgres role; production DB
untouched):

```bash
cd backend
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py migrate
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py produce_wayond_signal
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py test wims intelligence
```

`produce_wayond_signal` reads a Wayond signal (default fixture
`intelligence/fixtures/wayond_signal_sample.json`, or `--fixture` / `--signal`),
produces + delivers the envelope, runs the existing WIMS pipeline, prints the
five evidence points + audit trail, and ends in `PASS`. `--no-pipeline` stops
after consumption.

In deployment, run against Postgres unchanged (ORM-only): `python manage.py
migrate wims && python manage.py produce_wayond_signal`.
