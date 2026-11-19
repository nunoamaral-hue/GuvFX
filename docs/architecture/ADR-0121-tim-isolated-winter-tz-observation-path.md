# ADR-0121 — Isolated TradersWay-Demo winter-timezone observation path

- **Status:** Accepted (repository-only; additive; migration-free; default-off bridge options preserve byte-identical
  production behaviour; no production deploy, no host staging, no terminal/bridge launch, no token provisioning, no
  probe execution, no broker contact by this packet).
- **Packet:** GFX-TIM-ISOLATED-WINTER-TZ-PATH-IMPLEMENTATION-013 (TIM — winter broker-timezone verification).
- **Date:** 2026-11-19.
- **Relates to:** ADR-0070 / ADR-0076 (TIM producer boundary), GFX-TIM-WINTER-TIMEZONE-HOST-PROBE-009 (the probe),
  GFX-TIM-WINTER-TZ-OPAQUE-LAUNCHER-011 (the opaque launcher), and the pkt-012 design.

## Context

The winter broker-timezone observation requires a TradersWay-Demo (`trade_mode==0`) account. Two on-host runs of the
merged opaque launcher returned `not_demo_or_ambiguous`. A strictly read-only binding diagnosis proved the default
probe endpoint `127.0.0.1:8788` is bound (via `MT5_TERMINAL_PATH` in the host autostart) to the IS6 Program-Files MT5
terminal, which the owner's authenticated view shows is a **live** `Taurex-LIVE` / `trade_mode=2` session with an
active guarded attachment. The approved `TradersWay-Demo` producer exists only in the **dormant**
`C:\GuvFX\terminals\account_001\instance` slot. The launcher must therefore read the dormant demo slot **without ever
touching the live `:8788` bridge/terminal or any live tenant** — which the stock bridge cannot do, because its HTTP
server hard-coded `("0.0.0.0", HTTP_SERVER_PORT)` (no loopback option) and always runs the job poller.

## Decision

Add a repository-only, isolated, ephemeral read path. Nothing is deployed, staged, or run by this packet.

1. **Explicit, fail-closed bind host** (`scripts/mt5_signal_bridge.py`): `resolve_bind_host()` reads `BRIDGE_BIND_HOST`,
   default **exactly `0.0.0.0`** (production byte-identical), accepts only the allow-list `{0.0.0.0, 127.0.0.1, ::1}`,
   and fails **closed** (refuses to start) on any other value (empty, `*`, malformed, hostname). Both HTTP-server
   construction sites now bind `(resolve_bind_host(), HTTP_SERVER_PORT)`.
2. **HTTP-only diagnostic mode** (same file): `http_only_enabled()` reads `BRIDGE_HTTP_ONLY`, default **off**. When on,
   `main()` serves the read-only snapshot endpoints only — it never enters the job-polling `main_loop()` — and `do_POST`
   refuses every mutating route (`http_only_mode_no_mutation`, 403), so the bridge is structurally incapable of placing
   or changing a trade. Inbound auth (`GUVFX_AGENT_TOKEN`) stays mandatory; the backend poller config is not required.
3. **Isolated lifecycle controller** (`scripts/tim_isolated_bridge_ctl.py`, stdlib-only): `start`/`stop` the isolated
   observation. It treats `:8788` as a **protected control** (verified listening and unchanged, never touched); uses
   only loopback `127.0.0.1:8977` (a fixed port proven free of the 8800–8899 allocator and all repository ports),
   proving the bind is loopback-only; starts the dormant `account_001` terminal (or attaches to an owner-started one
   without owning it) and an isolated `BRIDGE_HTTP_ONLY` / `MT5_GUARDED_ATTACH` / no-`MT5_ALLOW_LIVE` bridge; records a
   dedicated state file; and on teardown terminates **only** the PIDs it created, each after a live-identity re-check
   (PID reuse is refused), never by process name. Partial starts roll back deterministically.
4. **Three fixed, no-argument opaque launchers** (`deploy/tim-winter-tz/{start_tim_isolated_bridge,
   run_winter_tz_probe_isolated, stop_tim_isolated_bridge}.bat`): thin wrappers that load the token opaquely via the
   bridge's own governed secrets loader (never read/echoed/persisted) and invoke the controller / the probe (base URL
   hard-coded to `http://127.0.0.1:8977`, server hard-coded to `TradersWay-Demo`).

## Consequences

- The token stays opaque: it flows from the secrets `.bat` through the process environment into the temp bridge; the
  controller passes the environment through and never reads, logs, or stores a token.
- DEMO classification and server confirmation remain the probe's fail-closed responsibility; the isolated bridge only
  serves loopback reads of the dormant demo slot.
- Default-off everywhere: with `BRIDGE_BIND_HOST` and `BRIDGE_HTTP_ONLY` unset, the production bridge is byte-identical.
- Execution (host staging, terminal/bridge launch, one probe run, teardown) remains Sponsor-gated under a separate
  authorization; this packet is repository-only, migration-free, auto-merge OFF, and does not touch production or any
  host.

## Alternatives considered

- **Relog / repoint the `:8788` terminal:** rejected — it carries a live `Taurex-LIVE` guarded monitoring session.
- **Reuse an existing per-account demo bridge (e.g. account_025 IS6-Demo):** rejected — wrong broker/server and a live
  tenant; must not be touched.
- **0.0.0.0 bind + firewall rule:** rejected in favour of a true loopback bind — fewer host-state changes, no reliance
  on a firewall rule for isolation.
- **Pure `.bat` lifecycle:** rejected — PID-identity teardown and `:8788` protection are safety-critical and must be
  unit-tested; the logic lives in a stdlib controller with thin `.bat` wrappers.
