# Rule — Data

Scope: read when handling market data, account/trade records, derived datasets, or any
ingestion/cleaning/storage work.

- **Raw evidence is immutable.** Captured raw data (prices, ticks, broker records) is
  written once and never edited in place. Corrections are new records, not overwrites.
- **Preserve source and time semantics.** Every record keeps its source and the time it
  refers to versus the time it was observed/ingested. Do not collapse these.
- **Point-in-time correctness.** Research and any backtest must see only data that was
  knowable at the modelled moment. No look-ahead, no survivorship leakage.
- **Derivatives are rebuildable.** Any derived/aggregated dataset must be reproducible
  from raw inputs plus recorded config. Treat derived data as a cache, not a source.
- **Quarantine, don't destroy.** Suspect or malformed data is moved to a quarantine area
  with a reason — never silently deleted or "cleaned" in a way that loses the original.
- **Configurable paths.** Data locations are configuration, not hard-coded constants or
  personal/home paths.
- **No large or raw data in Git.** Bulk market data and binary artefacts live outside the
  repository; Git holds code, small fixtures, and concise evidence only.
