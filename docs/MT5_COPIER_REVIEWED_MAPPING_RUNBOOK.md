# Runbook — Copy Trading reviewed instrument-mapping persistence (SAFE-EXACT only)

Scope: the governed `--safe-exact-only` review mode of `refresh_instrument_mappings` (ADR-0062). DARK;
no production persistence until a separate Sponsor GO. Execution stays OFF. Never touches `CopierSymbolMap`.

## What it does

Persists **only** reviewer-approved SAFE-EXACT discovery candidates into `InstrumentMapping`
(`review_status=APPROVED`, with `reviewed_by` + `review_evidence_ref`). It is an off-execution-path audit
layer: approval grants **no** execution authority (see ADR-0062). The copier execution path never reads
`InstrumentMapping`.

## Preconditions

- `BrokerInstrument` catalogues synced for the accounts (Phase A).
- Migration `trade_copier/0007` applied in the target environment.
- Master and follower are the **same owner**, **different accounts**, and **non-Pepperstone** for this stage.

## Commands

Dry-run (inert — writes nothing; shows how many SAFE-EXACT would persist and how many are skipped):

```bash
python manage.py refresh_instrument_mappings --master 46 --follower 48 --safe-exact-only
```

Governed commit (requires a non-empty reviewer AND evidence; fails closed otherwise):

```bash
python manage.py refresh_instrument_mappings --master 46 --follower 48 \
  --safe-exact-only --commit \
  --reviewer "nuno-operator" \
  --evidence "notion:<programme-ledger-page-id>" \
  --review-note "Phase B non-Pepperstone, direction 46->48"
```

Output reports `created / updated / skipped(non-safe-exact)` counts. Re-running is idempotent (upsert on the
native directional pair).

## Guardrails (all fail-closed)

- No `--reviewer` or no `--evidence` on commit → `CommandError`, nothing written.
- Self-pair (`--master == --follower`) → refused.
- Cross-tenant (different owners) → refused.
- AMBIGUOUS / INELIGIBLE / ABSENT / STALE / hard-dimension / soft material-warning candidates → skipped.
- VERIFIED candidates (reflected from `CopierSymbolMap`) are not persisted by this mode.
- `review_status=APPROVED` never changes `classification` to `VERIFIED`, never sets
  `verified_via_symbol_map`, never writes `CopierSymbolMap`, never enables a flag, never creates an
  ExecutionJob or order.

## Verify after a commit

```bash
# approved rows for the direction (classification must remain UNVERIFIED)
python manage.py shell -c "from trade_copier.models import InstrumentMapping as I; \
  print(list(I.objects.filter(master_account_id=46, follower_account_id=48, review_status='APPROVED')\
  .values_list('master_symbol','follower_symbol','classification','reviewed_by')))"
```

Confirm: `CopierSymbolMap` count unchanged; all copier/execution flags OFF; ExecutionJob count unchanged.

## Rollback (table-scoped)

Approved discovery rows are additive audit data. To revert a direction:
`InstrumentMapping.objects.filter(master_account_id=<m>, follower_account_id=<f>, review_status='APPROVED').delete()`.
To revert the schema: `migrate trade_copier 0006` (drops the five review columns; reverses cleanly on PG15).
