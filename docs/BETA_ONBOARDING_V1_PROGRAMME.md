# GFX Beta Onboarding V1 — Programme Plan & Target Architecture (PROPOSAL)

> **Status: PROPOSAL — awaiting Nuno's architecture approval. Customer onboarding stays CLOSED.**
> Source of truth for the findings: read-only investigation `wf_e3b038d9-1e7` (8 parallel agents) +
> production census, 2026-07-20. Nothing in production was changed. This document is a proposal; the
> lifecycle/decision status is PM-owned and NOT advanced here.

## 0. Verdict

**GuvFX cannot onboard external beta users today, and must not, until per-user isolation exists.**
21 Critical + 14 High blockers. The platform has never run multi-tenant (prod: 4 users — 2 staff, 2
inactive/test; 3 broker accounts; 1 real MT5 terminal). It is **safe right now only because onboarding
is hard-blocked** (email verification is required but no code is ever sent). **The first rule of this
programme: do not unblock onboarding until MT5 runtime + terminal + routing + sizing isolation is built
and verified.** Unblocking onboarding early would arm live paths that endanger Nuno's production account.

**What is already solid** (do not rebuild): application-DATA isolation — every user-owned DRF resource
(TradingAccount, Trade, StrategyAssignment, Strategy, MT5 sessions, hosting, analytics, jobs) scopes its
queryset to `request.user` with an intentional staff bypass; no DB-level IDOR found; MT5 broker
credentials are **Fernet-encrypted at rest** (`GUVFX_FERNET_KEY`, 0 plaintext rows); the worker
broker-credentials endpoint is worker-token-only. The VPS is **not** the constraint (≈20 GB RAM free, 8
cores, 91 GB disk, near-idle). The gap is entirely in the **MT5-terminal / execution / routing / sizing**
layer, which is single-tenant by construction.

---

## Part 1 — The 21 Critical blockers, grouped and ranked

Legend — **Effort:** S (<1 day) · M (2–4 days) · L (1–2 weeks) · XL (multi-week / infra).
**Resolve:** *Independent* (ship now, additive, no architecture dep) · *Arch-gated* (needs the hosting
decision below) · *Depends: Cn*.

### Group 1 — SECURITY (cross-tenant leakage / credential exposure / account hijack)

| # | Blocker | Evidence | Effort | Resolve |
|---|---------|----------|--------|---------|
| **C15** | Reliability **alerts & recommendations endpoints unscoped** — any user sees the operator's alerts (1 open CRITICAL + 42 recs w/ internal refs) | `AlertListView`/`RecommendationListView` return `.objects.all()`, gated only by `IsAuthenticated`; FKs `AlertEvent.trading_account`, `Recommendation.terminal_node` exist | **S** | **Independent** — filter by `trading_account__user` for non-staff |
| **C14** | Dashboard **"Able to trade" pill = GLOBAL operator health** shown to every user | prod dashboard `:442-458` + repo Trading-Health card read `/api/reliability/trading-health/` with no `account_id` → defaults `scope=GLOBAL` (currently HEALTHY) | **S** | **Independent** — pass `?account_id=<user's>`; per-account snapshots already exist (55 786 rows); else "unavailable" |
| **C2** | **Account-create hijacks Nuno's terminal** — a new user's broker validation logs the *shared* production terminal into *their* broker account | `add-with-mt5-login` → `_get_user_mt5_instance()` (`trading/views.py:67-93`) priority-2 returns any Windows instance = `guvfx-windows-mt5` (leased to Nuno) → agent `/mt5/login-and-validate` on Nuno's box | **S** guardrail / **XL** real | **Independent guardrail** (fail-close fallback) + **Arch-gated** real fix |
| **C17** | New external accounts **auto-bound to Nuno's leased instance** — set-active/sync/launch all target Nuno's box | `_get_user_mt5_instance` fallback returns `guvfx-windows-mt5` (id 1, leased to user 2); `perform_create` (`:217`) binds every non-staff account to it | **S** guardrail / **XL** real | **Independent guardrail** (fail-close) + **Arch-gated** |
| **C16** | **Decrypted broker password written to a shared handoff dir** — two users on the box write `launch_account.json` (plaintext pw) into the same hostname-keyed dir | `mt5/views.py` launch handoff; single shared box, no per-user runtime; TX-1 identity not applied to the live instance | **XL** | **Arch-gated** — interim: keep MT5 desktop launch disabled for non-staff |
| **C7** | **Shared VNC desktop, write-enabled** — every session VNCs to Nuno's `Administrator` MT5 console with `read-only:false` → a viewer can trade on Nuno's live MT5 | all `SessionAssignment.enabled=False` → LEGACY adapter → `build_mt5_desktop_payload` → `100.79.101.19:5900`, shared `GUAC_MT5_PASS`, `read-only:false` | **XL** | **Arch-gated** — interim: desktop launch disabled for non-staff |

### Group 2 — ARCHITECTURE (multi-tenant MT5 runtime / terminal / routing — the gating decisions)

| # | Blocker | Evidence | Effort | Resolve |
|---|---------|----------|--------|---------|
| **C3** | **No automatic per-user isolated runtime** — all users land on one shared `Administrator` box | `_get_user_mt5_instance` fallback → single `guvfx-windows-mt5`, `windows_username=Administrator`; no per-user Windows identity / portable dir in the live path | **XL** | **Arch-gated** (the core decision) |
| **C4** | **One active account per (user, instance)** serializes users — a new validation deactivates all others on the shared box | `views_account_add.py:97` + `uniq_active_account_per_instance` (`trading/models.py:125-128`) | **M** | **Arch-gated** — resolved by a distinct runtime per user |
| **C6** | **Real provisioning is manual** — admin CLI + hand-run `Provision-GuvfxAccount.ps1` + manual bridge; the isolation path is DORMANT (`SessionAssignment.enabled=False`) | `manage.py provision_terminal_account` writes DB only; materialization is manual PowerShell on the box; `/provision-user` only exists in DEPRECATED `demo_endpoints.py` | **L–XL** | **Arch-gated** — automate via the per-user agent |
| **C9** | **Cannot deliver 5 simultaneous isolated MT5 sessions** — single `TerminalNode`; every binding resolves to the same shared `:5900` desktop; standard Windows 2-session RDP ceiling (RDS forbidden) | occupancy per-binding → 409 on 2nd occupant; all bindings → one framebuffer | **XL** | **Arch-gated** (capacity) |
| **C18** | **No isolated per-user terminals exist** — pool rows `mt5free-1..4` are inert `platform=LINUX` placeholders (no host/desktop/user) | `build_mt5_desktop_payload` targets one fixed host + one `conn_id 'mt5-terminal'` | **XL** | **Arch-gated** |
| **C19** | Every external account **binds to the shared box or None** (dead-ends `is_active=False`) | `trading/views.py:67` fallback; `perform_create` binds it | **S** guardrail / **XL** | **Independent guardrail** + **Arch-gated** |
| **C10** | **Auto-router is single-tenant per source** — a 2nd user arming a source → `len(active)==2` → `None` → fail-closed → **stops auto-copy for everyone incl. Nuno** | `execution/auto_router.py:123-127` requires exactly one AUTO_DEMO assignment per source | **L** | **Depends: per-account identity**; mostly code (fan-out), arch-adjacent |

### Group 3 — MISSING IMPLEMENTATION (features to build; mostly app-layer / additive)

| # | Blocker | Evidence | Effort | Resolve |
|---|---------|----------|--------|---------|
| **C1** | **Onboarding hard-blocked** — email verification required but no code ever sent (token discarded, no MTA) | `email_verified` in `REQUIRED_STEPS` (`onboarding/services.py:66-72`); `EmailSendVerificationView` (`views.py:62-84`) drops the token (`_ = plaintext`, `:79`), never `send_mail`; prod `EMAIL_HOST=localhost` | **S** | **Independent** — beta-flag bypass (auto-verify) OR real transactional email; **must stay behind a flag with onboarding CLOSED** |
| **C5** | Onboarding **"auto-provision" is a DB-row stub inside `try/except: pass`** — no runtime, no creds, no launch; errors swallowed | `mark_account_connected` → `provision_terminal_for_account` (`onboarding/services.py:356-360`) only writes `TerminalBinding`+auth if instance+node already exist | **M** skeleton / **XL** real | **Arch-gated** for the real pipeline; surface-errors part is independent |
| **C8** | **No self-service terminal binding/authorization** — bindings are manual DB rows (prod: 1 auth, both bindings = operator) | no API/UI/command creates `TerminalBinding`/`UserToTerminalAuthorization` | **M** | **Depends: C3** (needs a per-user runtime to bind to) |
| **C11** | **No per-user/per-account lot-size config** — sizing is one global row per source | `SignalSourceConfig.source` unique; planning reads by source only (`signal_planning.py:209,273-277`); no per-account field/api/ui | **M** | **Independent** — new per-assignment override model + validation + api + ui |
| **C12** | **Lot edit would mutate Nuno's live global config** (only lever is the shared `SignalSourceConfig`) | ti_signals=0.40 / wayond=0.02 rows drive Nuno's acct#1 | **—** | **Depends: C11** — never expose `SignalSourceConfig`; gate behind the override model |
| **C13** | **Self-service arming doesn't exist** — marketplace rejects signal-copy strategies; toggle 409 `not_armed`; AUTO_DEMO is a staff/DB step | `views.py:804-808, 895-904, 993-1001` | **M** | **Depends: C10** (fan-out) — gated, demo-only, entitlement-scoped |
| **C21** | **Activation requires a manual human MT5 login** on the box | `set-active` (`trading/views.py:288-324`) 409s unless the EA reports MT5 already logged into that account | **L** | **Arch-gated** — automate agent-driven broker login into the per-user terminal |

### Group 4 — BUGS (defects in existing code)

| # | Blocker | Evidence | Effort | Resolve |
|---|---------|----------|--------|---------|
| **C20** | **Terminal launch 409-broken for every non-Nuno user** — binding source ≠ lease source | `perform_create` binds `guvfx-windows-mt5`; `Mt5DesktopLinkView` leases from `mt5free-1..4` pool → HARD GATE 2 (`mt5/views.py:194`) 409 "bound to a different MT5 instance" | **S** | **Independent** — unify binding & lease source; add e2e test |

**Cross-cutting High blockers that become Critical at beta:** no automatic beta entitlement (users land
in viewer mode, 0 accounts); free uncontrolled grant of the paid tier; marketplace not entitlement-scoped
(hardcoded 10 templates); pool caps at 4 (5th user → 503); provisioning failure silently swallowed;
credential validation can hang PENDING forever (no timeout); no WS-J Account Status panel.

### What can ship independently NOW (additive, onboarding stays closed)

These need **no** architecture decision and reduce real risk / build reusable pieces:
`C15`, `C14` (cross-tenant leaks) · `C2`/`C17`/`C19` **fail-close guardrails** (a new user can never bind
to Nuno's box) · `C20` (launch path bug) · `C11`+`C12` (per-account lot override model) · `C1` (beta
entitlement + email bypass **behind a flag, onboarding stays closed**) · a **WS-J Account Status panel**
(backend signals mostly exist). Everything else waits on the architecture.

---

## Part 2 — Target architecture for safe multi-tenant operation

### Isolation requirements (must all hold per user)

| Dimension | Requirement |
|---|---|
| **MT5 runtime** | Each user's terminal runs as a distinct OS identity + private data dir; no shared desktop, no shared handoff dir |
| **Broker credentials** | Fernet-encrypted at rest (✓ already); decrypted only inside that user's isolated runtime; never written to a shared path |
| **Guacamole session** | Per-user connection to only that user's terminal; scoped/short-lived token; no cross-user connection id |
| **Strategy execution** | Signals fan out to N accounts; each user's orders placed only on their terminal; one user's failure/arming never affects another (fixes C10) |
| **Sizing** | Per-account lot override; never touches the shared `SignalSourceConfig` (fixes C11/C12) |
| **Alerts / dashboard** | Per-account scoping; operator/global data staff-only (fixes C14/C15) |

The **execution model is already enqueue-only** (`ExecutionJob` → worker → bridge), so multi-tenancy is
achieved by giving each user their own **runtime endpoint** the bridge/worker targets — not by rewriting
execution. That makes the terminal-hosting choice the pivotal decision.

### The three options

**Option A — Multiple Windows VPSs (≈1 per 2 users).**
Each user (or pair) gets a Windows VPS with their MT5 terminal + a per-user bridge; Guacamole/RDP to their
own host. *Scalability:* linear, ~3 VPSs for 5 users (2-session ceiling). *Isolation:* strongest
(separate OS + host). *Ops:* high — N Windows hosts to patch/monitor, a bridge per host, provisioning
automation per host. *Security:* strong. *Cost:* **high** — Windows VPS + licensing × ~3, grows linearly.
*Fit:* matches today's bridge-per-host model; least new tech risk (native MT5).

**Option B — Windows-per-user isolation on fewer hosts (kiosk sessions / Windows containers).**
Per-user `guvfx_u_<id>` kiosk RDP (the built-but-dormant TX-1 path) or Windows Docker containers running
MT5. *Scalability:* capped — standard Windows allows 2 concurrent RDP sessions (RDS is
governance-forbidden), and Windows containers can't easily present the MT5 GUI to Guacamole. *Isolation:*
strong (per-user identity + runtime dir already built). *Ops:* **very high** (Windows containers are
heavy; kiosk-session automation is fragile — this is the path that's already dormant *because* of the
capacity cap). *Security:* strong. *Cost:* medium. *Fit:* reuses TX-1 but hits the ceiling that stalled it.

**Option C — Linux + Wine per-user containers (RECOMMENDED).**
One lightweight Linux container per user running **MT5 under Wine**, each with its own VNC endpoint +
per-user bridge, orchestrated by Docker on the existing (or one additional) Ubuntu VPS; Guacamole connects
each user to their container's VNC. *Scalability:* **excellent** — no 2-session ceiling, no Windows
licensing; the current VPS (20 GB free / 8 cores) fits 5+ comfortably; scale by adding containers/hosts.
*Isolation:* strong (container + separate framebuffer + per-container Fernet-injected creds). *Ops:*
**moderate** — the whole stack is already Docker/Linux; one Wine+MT5 image + a compose template per user.
*Security:* strong. *Cost:* **low**. *Fit:* strongest — Guacamole already supports "MT5 (Wine on Linux)"
(per CLAUDE.md), the pool rows are already `platform=LINUX`, and execution is enqueue-only.

### Comparison

| Criterion | A · Multi-Windows-VPS | B · Windows-per-user | C · Linux+Wine (rec.) |
|---|---|---|---|
| Scalability to 5 (and beyond) | Linear, ~3 hosts | Capped (2-session/RDS) | Excellent (containers) |
| Isolation strength | Strongest | Strong | Strong |
| Operational complexity | High | Very high | Moderate |
| Cost | High (lic*hosts) | Medium | **Low** |
| Fit with current stack | Good (bridge/host) | Reuses dormant TX-1 | **Best (all-Docker/Linux)** |
| Key risk | Cost/ops at scale | Hits the ceiling that already stalled it | Wine↔broker (WIMS/IS6) compatibility |

### Recommendation

**Adopt Option C (Linux + Wine per-user containers)**, gated by a **1–2 day compatibility spike**:
stand up one Wine+MT5 container, log into the **target broker (WIMS-Demo / IS6)**, and confirm reliable
`order_send` + terminal GUI over Guacamole. If the spike passes → Option C. If Wine proves unreliable for
this broker → fall back to **Option A** (multi-Windows-VPS) with per-user provisioning automation.
**Nuno's existing single Windows box stays as his isolated production runtime, untouched, in every option.**

---

## Phased plan (once architecture is approved)

- **Phase 0 — Safe now (no arch dep):** C15, C14 (leaks) · C2/C17/C19 fail-close guardrails · C20 · WS-J
  Account Status panel · C11/C12 per-account lot override · C1 beta entitlement+email bypass **behind a
  flag, onboarding CLOSED**. Review + deploy; Nuno untouched.
- **Phase 1 — Architecture spike:** Option-C Wine+MT5 compatibility spike (or provision the first extra
  Windows host for Option A). Decision gate.
- **Phase 2 — Per-user runtime + provisioning automation:** C3, C4, C6, C18, C9, C16, C7, C8, C21 —
  automatic isolated runtime, credential injection, launch+login verification, self-service binding,
  per-user Guacamole.
- **Phase 3 — Multi-tenant execution:** C10 fan-out + C13 gated self-service arming (demo-only,
  entitlement-scoped) + per-account routing/sizing end-to-end.
- **Phase 4 — Beta hardening:** failure messaging/timeouts, 5-user load validation, isolation red-team,
  full test suite, adversarial re-review, controlled deploy, Notion evidence.

## Non-negotiables preserved throughout

Nuno's account (1302561), TI Signals, Wayond, lot sizes (ti 0.40 / wayond 0.02), AUTO_DEMO, provider
engine (disabled), and his single Windows runtime remain **isolated and untouched**. Onboarding remains
**CLOSED** until Phase 2/3 isolation is built and verified. Everything additive; no forced trades; no replay.
