# Technical Debt Register — Customer Zero Hosted-Workspace Programme

Read-only research pass, 2026-08-10 (main). No secret **values** recorded — names/locations/categories only.
Each row tagged **VERIFIED** (corroborated in-repo) or **ASSUMED** (mechanism found; specific symptom
inferred). Application code is unusually clean: 1 backend `TODO` (`strategies/execution_guards.py:30`), 0 in
`scripts/`, a few frontend stubs. Material debt lives in **operational/security posture and architecture**
(documented in `docs/`), not in code markers.

| # | Item | Category | Evidence | Why it matters | Action |
|---|------|----------|----------|----------------|--------|
| 1 | **No automated DB backup** — `guvfx-postgres` + `guac-db` unbacked; newest dump ~4.5 mo stale, no off-host copy, restore untested (VERIFIED) | **CRITICAL** | `docs/OPERATIONS_DASHBOARD.md` §6/§7; `OPERATIONS_RUNBOOK.md` §11 (template, never deployed) | Hosted programme now persists execution/workspace + Guac connection data; host/DB loss unrecoverable past Feb 2026. Data-loss SPOF for the exact CZ data. | Deploy §11 cron (`pg_dump` both DBs) + verified off-host copy; test a restore. Nuno. |
| 2 | **Guac/DB secret posture** — secrets inline in compose (2 locations each); documented reuse Guac-admin == `guac-db` password; several EXPOSED/un-rotated (VERIFIED) | **CRITICAL** | `SECRET_INVENTORY.md` Gaps 1,3; `OPERATIONS_DASHBOARD.md` §4/§7 | One leaked/reused credential widens blast radius across the MT5 remote-desktop path CZ depends on. | `600` `env_file:`; split the reuse into two values; rotate the flagged set. Nuno (no LLM rotation). |
| 3 | **RULE-3 encryption-key-from-signing-key** — `crypto.py::_get_fernet` derives MT5-credential key from `DJANGO_SECRET_KEY` (sha256) when `GUVFX_FERNET_KEY` unset (VERIFIED) | **CRITICAL** | `SECRET_INVENTORY.md` Gap 7; `SEC-CRYPTO-001.md`; `POST_INCIDENT_REVIEW_BRIDGE_TOKEN.md` §7a | Rotating `DJANGO_SECRET_KEY` would silently render every stored broker credential undecryptable — directly on the CZ broker-login path. | Execute SEC-CRYPTO-001 as its own packet: distinct `GUVFX_FERNET_KEY`, re-encrypt first. No drive-by. |
| 4 | **RULE-3 cross-credential fallbacks (~12 sites)** — distinct bridge/legacy-agent/worker tokens mixed, masked only because values coincide; incl. :8787/:8788 conflation (VERIFIED) | **HIGH** | `POST_INCIDENT_REVIEW_BRIDGE_TOKEN.md` §7a (names 11 files); `SECRET_INVENTORY.md` Gap 6,8 | `risk_controls.py` fail-closed margin guard: wrong-token 401 → terminal `PROMOTION_REJECTED`, no replay. | Own-packet: each service its own secret + startup self-validation; split conflated inventory rows. |
| 5 | **Single-VPS SPOF (whole estate)** — one Postgres (no replication), one Traefik, one Tailscale tunnel, one Windows box + manually-started bridge (VERIFIED) | **HIGH** | `OPERATIONS_DASHBOARD.md` §7 | Every CZ hosted session traverses these single points; host loss = total outage. RULE-1: manual bridge start is session-bound. | Bridge as supervised service/task; define DR/standby. Sponsor-gated infra. |
| 6 | **Beta validation agent :8791 SPOF** — single WinSW, Manual-start, `recovery=none`, no liveness/alert; dark for hours 2026-08-05 (VERIFIED) | **HIGH** | `OPERATIONS_DASHBOARD.md` §7; `VALIDATION_AGENT_PRODUCTION_HARDENING.md` | Customer broker-validation dependency for onboarding; silent death strands new validations, no auto-restart/alert. | Auto-restart + liveness probe + alert; finish the hardening doc's lifecycle. |
| 7 | **Legacy Administrator shared-runtime executor not retired** — live execution as Administrator console Session 1; isolated per-user/per-slot model is the intended architecture (VERIFIED) | **HIGH** (arch) | `STATUS.md`; memory `reference_terminal_isolation_tx1`; `CLAUDE.md` runtime-identity invariant; see `LEGACY_RETIREMENT_PLAN.md` | Two execution models coexist; legacy path keeps a wide blast radius and blocks true multi-tenant isolation (WP6B). | ADR-gated Administrator→dedicated-slot cutover (no silent switch). Sponsor/host-cert gated. |
| 8 | **Deploy image provenance not enforced** — `GUVFX_GIT_COMMIT`/OCI-revision mechanism exists but prod bakes `:latest`, no tags, VPS has no `.git`; SHA only if operator threads `--build-arg` (else "unknown") (VERIFIED) | **MEDIUM** | `backend/Dockerfile:27-34`; `core/version.py:57`; `OPERATIONS_DASHBOARD.md` §3/§7 | Cannot prove which code serves CZ; shared `:latest` means a backend patch also hits the shadow path. | Make `--build-arg GIT_COMMIT=$SHA` non-optional; adopt immutable `:git-<sha>` tags. |
| 9 | **Beta UX — paste + special-char keyboard mapping** — `server-layout=en-us-qwerty` (server layout, not client Mac/UK); paste browser→MT5 only. Mechanism VERIFIED; "Cmd+V / `#`" symptom ASSUMED | **MEDIUM** | `backend/mt5/guac_json.py:196-211`; `frontend/.../terminal-access/page.tsx:805-810` | UK/Mac customer on US-pinned layout mismaps `#`/`@`/`£`; Cmd+V won't map to RDP Ctrl+V — friction when typing broker passwords. | Confirm with CZ user; consider client-layout detection / in-UI paste helper; document limitation. |
| 10 | **CZ slot-2 orphan occupancy** — agent slot store: slot 2 → CZ uuid gen 4 materialised while backend runtime/job FAILED (data/control-plane divergence) (VERIFIED) | **MEDIUM** | `KNOWN_ISSUES.md`; `POST_INCIDENT_CZ_MATERIALISE_TIMEOUT.md` §4-6 | Contained but consumes a physical slot, violates `(slot,generation)` invariant; reclaim via signed STOP→TOMBSTONE→RELEASE, never manual edits. | Sponsor-authorise reclaim; run ADR-0024 RELEASE driver. |
| 11 | **Optimistic `RUNNING` vs strict `runtime_ready` divergence** — account page shows "ready" on `state==RUNNING`; marketplace/arm-gate use strict `account_runtime_ready` (VERIFIED) | **MEDIUM** | `KNOWN_ISSUES.md`; `docs/product/beta-journey-consolidation.md` §5-7 | RUNNING-but-stale reads "ready" yet blocked at arm — confusing CZ onboarding dead-end. | Converge both surfaces on `account_runtime_ready`. |
| 12 | **NAS/off-host backup — service-account credential design gap** — off-host target blocked on un-provisioned NAS creds (VERIFIED) | **MEDIUM** | `NEXT.md:421`; `KNOWN_ISSUES.md:498`; memory `reference_nas_and_dr_backup` | Even once a backup cron exists, no authenticated off-host target. Compounds #1. | Least-privilege NAS service account + provision creds; wire the push. Nuno. |
| 13 | **`guvfx_u_6` orphan Windows profile** — leftover `C:\Users\guvfx_u_6` after provisioning delete (VERIFIED) | **LOW** | memory `project_trusted_beta_fasttrack`; `BETA_HEADLESS_WSA_FEASIBILITY.md:33` | Residual identity/profile clutter; confuses future identity audits. Contained. | Remove via identity/profile cleanup path. |
| 14 | **Monitoring/alerting gaps** — 9/11 containers lack healthchecks; `/health` trivial (no DB probe); no alert sink confirmed (VERIFIED) | **MEDIUM** | `OPERATIONS_DASHBOARD.md` §5/§7 | A hung (not crashed) container serving CZ traffic is undetected; outages surface via the customer. | Real healthchecks + DB-aware `/health`; wire one alert sink. |
| 15 | **37 git-tracked `.bak` snapshots (24 under `backend/mt5/`)** (VERIFIED) | **LOW** | `git ls-files \| grep .bak` | Repo clutter vs "small diffs" agreement; grep noise on `guac_json.py` central to CZ delivery. | Delete tracked `.bak`; rely on git history. |
| 16 | **Frontend stubs / pre-existing drift** — `accounts` test-connection TODO; `LoginClient.tsx` placeholder; strategies migration index-rename drift; reliability UTC-midnight flake (VERIFIED) | **LOW** | `frontend/.../accounts/page.tsx:547`; `login/LoginClient.tsx:13`; `KNOWN_ISSUES.md` | Small correctness/coverage debts; migration drift + flake will bite the next editor. | Address opportunistically per no-drive-by rule. |
| 17 | **`execution_guards.py` symbol-list TODO** — guard hard-codes FX symbols (VERIFIED) | **LOW** | `backend/strategies/execution_guards.py:30` | Scope limit, not a bug; relevant only when instrument coverage expands. | Extend guard + tests when instrument set grows. |

**Highest-leverage sequence:** (1) DB backup + (12) NAS service-account → close the top data-loss SPOF; then
(2)/(4) secret posture as an owned packet; (7) Administrator→dedicated-slot retirement (ADR-gated) is the
largest architectural item gating true multi-tenant isolation. No secret values were read or printed.

---

## Beta UX Backlog (NOT certification blockers)

Per the Chief Architect's Final Certification packet, the following are explicitly reclassified as **Beta UX
backlog** — they do **not** block Customer Zero certification and are tracked as product polish for the
post-certification Hosted Workspace UX workstream (see `HOSTED_WORKSPACE_UX_ROADMAP.md`). None affects the
security/execution/isolation boundary.

| Item | Category | Current state | Roadmap ref |
|------|----------|---------------|-------------|
| Mac native keyboard shortcuts (Cmd → Ctrl mapping, e.g. Cmd+V) | Beta UX | Right-click paste works; Cmd+V does not map to RDP Ctrl+V | R10 |
| `#` / `@` / `£` and other special-char mapping on non-US client layouts | Beta UX | `server-layout` pinned `en-us-qwerty` (server layout); UK/Mac clients mismap symbols | R11 |
| Clipboard polish (paste helper / clearer affordance; copy-out stays disabled) | Beta UX | browser→MT5 paste enabled; MT5→browser copy intentionally off | R9 |
| Fullscreen MT5 | Beta UX | not implemented | R1 |
| Expand/collapse + hide surrounding navigation | Beta UX | not implemented | R2, R5 |
| Responsive / auto-resize MT5 sizing | Beta UX | `resize-method=display-update` set; resize-event wiring not done | R3, R4 |

These were previously listed as MEDIUM/LOW debt (item #9); they are now formally owned by the UX roadmap and
removed from the certification-blocking set.
