# Broker-Server Timezone Evidence v2 — pre-transition control

**Packet:** GFX-PKT-006D-TZ-PROBE-V2  
**Probe date:** 2026-10-24  
**Authority:** Sponsor-approved, strictly read-only broker-timezone probe  
**Verdict:** **PARTIAL / TIME-GATED — winter offset not established**

## What this probe establishes

- The Tailnet route to the Windows MT5 host `100.79.101.19` is reachable.
- Port `8788` is owned by `python.exe` running
  `C:\GuvFX\mt5_signal_bridge.py`.
- The unauthenticated rates request returned HTTP `401` (negative control).
- The existing production backend credential path returned three EURUSD M1 bars
  with timestamps `1792799820`, `1792799880`, `1792799940`; cadence was exactly
  60 seconds (positive data-path control). No token value was printed or copied.
- The Windows clock was NTP-synchronised to `time.windows.com`; the last
  successful sync was `2026-10-24 13:22:35` local display time. The captured
  UTC time was `2026-10-24T13:30:46.7349617Z`.
- The bridge health endpoint returned `ok=true`, `status=healthy`.

## Why this does not establish the winter offset

The probe ran on Saturday while the FX market was closed, and before the
2026-10-25 European DST transition. The newest M1 bar was therefore stale from
Friday's session. Comparing that timestamp with Saturday UTC would measure
market-closure age, not broker-server offset. The freshness condition used by
the valid summer probe cannot pass.

Current source identity is also not independently exposed by the bridge health
or positions payload. The repository's v1 evidence identifies `:8788` as
TradersWay-Demo account 1121106, but this v2 probe did not re-establish that
binding from broker truth. The production database currently contains no
`TradersWay-Demo` account row or trade sample. Other demo servers measured by
the read-only database command showed a median `+3` hours, but those observations
are not evidence for TradersWay-Demo and are excluded from the verdict.

## Safety boundary observed

No order, trade, login, credential change, account change, workflow execution,
service restart, deployment, normalisation, backfill, or production-data write
was performed. No secret value was requested, displayed, or stored.

## Required completion probe

Run the same read-only three-bar M1 comparison after the market is open following
the DST transition. It is valid only when all of the following pass:

1. broker-truth identity confirms the intended TradersWay-Demo login/server;
2. Windows UTC clock is synchronised;
3. three consecutive M1 bars have 60-second cadence;
4. the latest bar is fresh within the current broker minute;
5. the rounded whole-hour offset is within `[-12, +14]` and residual is within
   180 seconds.

Until then, the v1 summer observation (`UTC+3`) remains valid only for its
observed season. **Do not create a winter mapping, normalise across the DST
boundary, or run a broad backfill.**
