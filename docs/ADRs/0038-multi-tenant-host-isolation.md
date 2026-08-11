# ADR-0038 — Multi-tenant host isolation prerequisites (AppLocker merge + per-account RemoteApp alias)

- **Status:** Proposed (Amber — repository + cert-tooling; effective on the host only at the Sponsor-gated cert)
- **Date:** 2026-08-11
- **Programme:** Beta Readiness Stream 6 (Multi-Tenant Host Isolation Prerequisites)
- **Builds on:** ADR-0036 (Host Provisioning Engine), ADR-0037 (Signed Host Executor). Closes the two
  machine-global gaps Stream 5 flagged before a second hosted slot may share the Customer-Zero Windows host.

## Context

Stream 5 shipped the signed host executor but flagged two machine-global hazards that would make provisioning a
**second** hosted tenant on the CZ host unsafe: **M1** — `Set-GuvfxAppLocker.ps1 -Deploy` REPLACES the whole
machine policy (no `-Merge`), so a new tenant would wipe CZ's enforced hardening; **M2** — the RemoteApp alias
`terminal64` is machine-global, so a second slot's publication collides with CZ's.

## Decision

**Canonical rule — one hosted account is exactly:** one Windows user (`guvfx_u_<id>`) + one account root
(`C:\GuvFX\accounts\<id>`) + one RemoteApp alias + one AppLocker tenant contribution + one persistent MT5
workspace. Everything is server-derived from `account_id`; nothing is client-controlled.

### M1 — AppLocker tenant model (`hosted_workspace/applocker_policy.py`, `Set-GuvfxAppLockerTenant.ps1`)

A deterministic **policy compiler**: the certified **base** (shared Administrator recovery + Everyone
`%WINDIR%`/`%PROGRAMFILES%`/MSI + the MetaQuotes publisher Allow) plus, per account, a **tenant fragment** of
SID-scoped shell/escape DENY rules whose rule IDs are deterministically tagged with the account
(`<acct:08x>-0000-4d54-0000-<seq>`). `effective = base + Σ fragments`.

- **Additive:** adding account N merges ONLY N's rules (host `Set-AppLockerPolicy -Merge`); the base and every
  other tenant are untouched — no policy-wide replacement.
- **Reversible:** removing N strips ONLY N's account-tagged rules; **removing Customer Zero (account 1) is
  forbidden**.
- **Idempotent:** re-merging N yields the same rules (same deterministic IDs).
- **Posture preserved:** the compiler refuses to emit any writable-tree executable Allow (`assert_base_invariants`
  is a permanent regression guard), so a renamed shell dropped in `C:\GuvFX\accounts\*` matches no Allow and is
  denied by default; the MetaQuotes publisher Allow and Administrator recovery Allow remain.
- A malformed account/SID, or a Deny scoped to a shared principal (Everyone/Authenticated/Administrators), is
  refused.

### M2 — per-account RemoteApp alias (`host_agent_dispatch.remoteapp_alias`, `delivery.py`, `Set-GuvfxRemoteApp.ps1`)

One **single source of truth**: `remoteapp_alias(account_id)` → `guvfx_mt5_<id>` per account, and `terminal64` for
Customer Zero (compatibility with its already-published alias — CZ is **not** migrated in this packet). Both the
host publisher (`ENSURE_REMOTEAPP` derives it server-side) and the Django delivery descriptor (derives it from
`trading_account.id`) use it, so they can never drift. The descriptor's RemoteApp program is therefore
owner-scoped and server-derived — a browser can never choose or override the program, and account N's descriptor
can name only account N's alias. Publication stays FilterByName (no full desktop) with the exact per-account
`terminal64.exe /portable`.

## Consequences

- The disposable-host certification (ADR-0037 runbook Phases 10–16) is now safe to run on the shared CZ host:
  the tenant AppLocker merge and per-account RemoteApp alias no longer replace CZ's policy or alias.
- No hard-coded user cap; the model scales to any account id with unique identity/root/alias/ACL/fragment.
- **Customer Zero is untouched** in this packet: its live policy and `terminal64` alias remain; migrating CZ to
  the canonical `guvfx_mt5_1` alias is a deliberate, separate later step.
- Repository + cert-tooling only — **no host mutation, no second slot, execution DARK**.

## Residuals (pre-existing, not worsened by Stream 6)

The certified CZ base still Everyone-allows `%WINDIR%` LOLBINs and (Script collection only) the accounts tree;
these are the CZ-H4 residuals tracked in `TECH_DEBT_REGISTER`. Stream 6 does not widen them and adds no new
writable-path executable Allow. Tightening the base's allow-model is a separate CZ-affecting hardening.
