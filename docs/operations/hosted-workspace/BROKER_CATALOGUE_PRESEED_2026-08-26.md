# Broker-Neutral Golden + Broker Catalogue — Research & Preseed Proof (2026-08-26)

Status: **BROKER_CATALOGUE_PRESEED_PROVEN** (engineering proof). No production code or host
mutation was deployed. Existing customers (CZ acct1, support@ acct25, Brian acct30, Patrick
acct31) were not touched (read-only inspection only). Disposable fixtures fully torn down.

## Problem

Onboarding UX defect: a customer waits several minutes for MT5 to discover a common broker
(e.g. IS6) via MT5's global online broker directory.

## Root cause (confirmed from code + host)

`backend/terminal_provisioning/windows/Populate-GuvfxViewerRuntime.ps1` builds the golden
template and **deliberately creates an empty `config`** (line ~46: `New-Item ... "config"`),
then robocopies the golden to each tenant. Verified on host: the active materialise source
`C:\GuvFX\golden\mt5\5.0.0.5833` has **no `servers.dat`**. So every fresh tenant starts with an
empty server list and must perform full online broker discovery → the multi-minute stall.

## The metadata artefact (Phase 1)

- The one artefact that makes a broker available is **`config\servers.dat`** — an **opaque
  binary** file. Server names are **not** stored as plaintext (a string search for "IS6" returns
  false even inside IS6's own file). Presence can therefore only be proven **behaviourally**.
- Non-secret source used: the broker-shipped **`C:\Program Files\IS6 Technologies MT5 Terminal`**
  (build 5833). Verified **clean**: `accounts.dat` count = 0, `bases\` empty, no history — i.e.
  no D/E/F class data. Its `Config\` holds `servers.dat` (3788 B) and `terminal.lic` (7289 B).

### A–F classification

| Artefact | Class | Disposition |
|---|---|---|
| `config\servers.dat` | **A** (public broker/server metadata) | **catalogue candidate** |
| `terminal.lic` | **C** (generic/white-label config) | not required for connectivity — excluded from minimum preseed |
| `accounts.dat` | D/E (identity/credentials) | ABSENT in source; never copy |
| `bases\*\history` | F (customer/history state) | ABSENT in source; never copy |
| `*.srv`, `dnsperf.dat`, `terminal.ini` | none present as broker-metadata carriers | n/a |

Minimum IS6 preseed (Phase 2) = **`servers.dat` alone (3788 B)**.

## Positive / negative control (Phase 3, RULE 11)

Method: clean runtime cloned from the **generic** golden `mt5\5833`; a credential-free startup
`/config` with `Server=…`, `Login=999999`, `Password=notarealpassword` (account 999999 does not
exist — authorization fails harmlessly, but **server resolution + connection happen first and are
logged**). MT5 launched in a real interactive RDP session (xfreerdp+Xvfb); session-0 headless MT5
does **not** initialise its network engine and cannot be used for this.

| Runtime | `servers.dat` | Result | Latency |
|---|---|---|---|
| **R_B preseed** | IS6 3788 B injected | `Network '999999': authorization on IS6Technologies-Demo failed (Invalid account)` — server RESOLVED + REACHED | **~3 s** from terminal start |
| **R_A control** | none | **No IS6 resolution at all** after 75 s+; only generic "MQL5 Cloud Server" entries; `servers.dat` never created | IS6 unreachable |

"Invalid account" (not "server not found") proves the IS6 server was reached; only the fake
account was rejected. The control never reaches IS6 → it is the multi-minute online-search path.
**Discriminates decisively.** Target ≤3 s met; ceiling ≤10 s met.

## Portability (Phase 4)

Same `servers.dat`, **different path (990013) and different Windows SID**, `Server=IS6Technologies-Live`:
`authorization on IS6Technologies-Live failed (Invalid account)` in ~2 s. → metadata is not bound
to path / SID / machine / account. Both IS6 servers (Demo + Live, the ones customers use) resolve
from one file.

## Broker-lock finding (Phase 5)

Same runtime + IS6 `servers.dat`, `Server=MetaQuotes-Demo`: **no resolution** in 55 s (vs 2–3 s
for IS6). **Broker-branded `servers.dat` files are broker-LOCKED** (contain only that broker's
servers). Therefore the catalogue must be **per-broker** — the customer selects a broker and
GuvFX preseeds that broker's file — not a single dropped file expected to serve all brokers.
Multi-broker coexistence in one runtime is **not required** for the one-broker-per-customer model
(and would require a merged `servers.dat`, obtainable only by binary merge or capture-via-discovery).
A genuinely-independent second broker's non-secret metadata is **not available on the current host**,
so empirical two-real-broker coexistence is deferred to the catalogue bootstrap (one-time
supervised discovery per broker). IS6 is proven as broker #1.

## First-run performance (Phase 9)

The active golden `mt5\5833` ships MQL5 samples **already precompiled** (Experts 39 `.mq5`/39
`.ex5`, Indicators 71/71) → MT5 logs "0 file(s) compiled" → **first launch ≈5 s**. The Sponsor's
~96 s was a **raw** MetaQuotes install recompiling missing `.ex5`, not this golden. Action belongs
to Phase 11 (precompile/prune before freezing any new golden), not to current tenants.

## Recommended architecture

- **Golden stays broker-neutral** (empty config). Catalogue supplies broker metadata separately.
- **Catalogue representation:** per-broker `servers.dat` files in a versioned host store
  (`C:\GuvFX\catalogue\vN\<broker_id>\servers.dat`) + a manifest (broker_id → sha256, certified
  build). **Reuse the existing `trading.BrokerServer`** DB rows (`server_name`, `environment`,
  `aliases`) for selection metadata — do **not** add a parallel schema.
- **Versioning:** independent `Golden vX` + `Catalogue vY`; record both (+ chosen broker) on the
  workspace provenance. A catalogue update = drop `vN+1` + flip a pointer; **no MT5
  rebuild/reinstall/reprovision**. Existing-tenant catalogue mutation is a separate gated packet.
- **Selection UX (Phase 10):** GuvFX onboarding → pick broker → pick Demo/Live → GuvFX preseeds
  that broker's `servers.dat` → open MT5 → customer types their **own** credentials in MT5.
  Server is pre-positioned **without** GuvFX storing any password. No auto-login.
- **Unknown broker (Phase 8):** MT5 online discovery stays available ("Can't find your broker?
  Search for another MT5 broker."). Customer-discovered metadata is **never auto-trusted** —
  discover → collect → validate → certify → future catalogue version.

## Reproduce

Host `guvfx-windows-mt5` (100.79.101.19); interactive session via xfreerdp+Xvfb from the VPS.
Clone `C:\GuvFX\golden\mt5\5.0.0.5833` to a disposable path; drop
`C:\Program Files\IS6 Technologies MT5 Terminal\Config\servers.dat` (sha256 `16600F67E3C4…`) into
`config\`; launch `terminal64.exe /portable /config:<abs>\startup.ini` with the credential-free
`Server=` config; read `logs\<date>.log` for the `Network … authorization on <server>` line.
