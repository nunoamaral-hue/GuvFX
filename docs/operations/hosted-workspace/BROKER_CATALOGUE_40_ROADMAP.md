# Wayond 40-broker catalogue intake roadmap (2026-09-28)

Target coverage universe for the hardened broker catalogue. **Classification rule:** a broker becomes an ACTIVE
catalogue entry ONLY with an authoritative exact MT5 server id (from `account_info().server` on a real connection,
or a pristine broker-shipped `Config\servers.dat`). **No server ids are invented here.** The list establishes target
BROKERS, not server identifiers.

Legend: **VERIFIED** = authoritative exact MT5 server id + evidence in hand · **NEEDS_CAPTURE** = MT5 broker, server
id not yet captured · **NOT_MT5 / VERIFY_PLATFORM** = name indicates a non-MetaTrader platform; confirm before any
capture (never assume an MT5 server).

## VERIFIED (authoritative evidence)
| Broker | Exact server id(s) | Evidence |
|---|---|---|
| Taurex (#5) | `Taurex-Demo` | Live acct 36 `account_info().server`; credential-free `servers.dat` captured (sha `23fd33b8…`), staged; DEMO scope |
| Pepperstone (#27) | `PepperstoneUK-Demo` (live), `PepperstoneUK-Live` (in broker file) | Live acct 35 (Demo); catalogue v1 artefact `afd6d65b…` |
| IS6 Technologies (not in list) | `IS6Technologies-Demo`, `IS6Technologies-Live` | Live acct 25 (Demo) + IS6-Live tenants; catalogue v1 artefact `a05ddd55…` |

## VERIFY_PLATFORM (likely non-MT5 — confirm before capture, do NOT assume a server id)
| # | Broker | Note |
|---|---|---|
| 22 | Match-Trader | "Match-Trader" is a distinct trading platform (Match-Trade Technologies), not MetaTrader 5. Confirm whether the broker also offers MT5 before any capture; if MT5-only-absent → NOT_APPLICABLE. |
| 14, 28, 31 | Funded Firm, Prop Firms, Shark Funded | Prop-firm brands — platform varies (MT4/MT5/cTrader/Match-Trader/DXtrade). Confirm the MT5 offering + exact demo server before capture. |

## NEEDS_CAPTURE (MT5 assumed by convention; server id NOT yet authoritative)
All remaining targets — capture required before any catalogue entry:
1 Vantage Markets · 2 Dominion Markets · 3 Multibank Groups · 4 ATFX · 6 ADS Markets · 7 Aegean Labs · 8 Alpari ·
9 CXM · 10 ABH Forex · 11 ACCM · 12 Capital · 13 Fortress FX · 15 FXTM · 16 Ingot · 17 Inzo · 18 Just Markets ·
19 Lirunex · 20 M4 Markets · 21 Mahi Markets · 23 Mazi Finance · 24 Mena Capital · 25 Moneta · 26 NIC ·
29 PU Prime · 30 SGFX · 32 Star Prime · 33 TMGM · 34 Traders Hub · 35 UEXO · 36 Ultima · 37 Verixa · 38 VPFX ·
39 WinProFX · 40 Zita Plus.
(#14, #28, #31 above also NEEDS_CAPTURE once their MT5 offering is confirmed.)

## How NEEDS_CAPTURE evidence is obtained (per broker)
1. Stand up a **disposable, supervised** MT5 runtime (never a customer runtime) and connect once to the broker's
   **demo** access server; read the authoritative id from `account_info().server`.
2. Copy **only** `config\servers.dat` (never `accounts.dat`/`bases`/`logs`); compute SHA256 + size.
3. Run `sanitise_broker_artefact --file <candidate> --identity <login> --identity <email> …` → require machine PASS.
4. `register_artefact_approval --kind broker_servers_dat --ref <broker>/<label> --sha256 <hex> --metadata '{… "servers_intended":["<Server-Demo>"], "size_bytes":<n>, "sanitiser":<verdict> …}'` → `decide_artefact_approval --approve` (staff).
5. Stage bytes at `versions/<label>/<broker>/servers.dat`; `build_catalogue_version` → `activate_catalogue_version --attest-host --require-certified`.

## Recommended capture sequence
1. **Taurex** (evidence already in hand) — first entry through the hardened pipeline (see `TAUREX_CATALOGUE_PRESEED_2026-09-28.md`).
2. Brokers with **imminent Wayond customer demand** next (Sponsor to prioritise from live sign-ups).
3. Batch the remainder in waves; because a version is immutable + activation is version-wide, **capture a wave of
   brokers, then roll a single new catalogue version** carrying the full approved set (existing + new) rather than
   one version per broker. Each wave: capture → sanitise → approve → build → attest-host → activate → rollback anchor.
4. Resolve **VERIFY_PLATFORM** brokers' platform before scheduling their capture.

Governance: existing tenants are never migrated; a missing/unapproved broker always falls back to native MT5
discovery (no onboarding breakage). MAGIC READ/ENFORCE OFF; live estate untouched.
