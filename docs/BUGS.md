# Bug Register

Tracked defects with observed evidence. Separate from operational exceptions (`OPERATIONAL_EXCEPTIONS.md`)
and known sharp edges (`KNOWN_ISSUES.md`). PM owns priority/status.

---

## BUG-001 — Accounts page exposes legacy `Mt5Instance` endpoints for beta accounts (not beta-aware)

- **Status:** OPEN — tracked, **scheduled for Phase D** (intentionally separated from Runtime Running).
- **Severity:** medium (UI correctness / customer-facing confusion; **not** the runtime blocker).
- **Observed symptom:** the Broker Accounts page returns
  `{"ok": false, "detail": "Account has no mt5_instance assigned."}` for Customer Zero (beta account #12).
- **Root cause (observed):** the accounts-page connection/test actions are the **legacy `Mt5Instance`** path:
  `backend/trading/views.py` — `test-mt5` (line 232), `set-active` (line 340), `test-connection` (line 415),
  each resolving via `_get_user_mt5_instance()` (line 70), which **fail-closes to `None`** for any account not
  leased a legacy `Mt5Instance`. A beta account uses the `AccountRuntime` model (ADR-0021) and **never** has an
  `mt5_instance`, so these endpoints report "no mt5_instance assigned" **regardless of provisioning progress** —
  and would continue to do so **even after** the runtime reaches RUNNING.
- **Not the runtime failure.** The runtime blocker is provisioner authentication (see
  `CUSTOMER_ZERO_EVIDENCE_MATRIX.md` Blocker B). This bug is a separate UI/endpoint gap and must **not** distract
  from Runtime Running.
- **Required fix (Phase D):** make the accounts-page connection/status for a beta account read the durable
  **`AccountRuntime`** state (via the existing beta-aware `terminal_provisioning/account_status.build_account_status`)
  instead of the legacy `Mt5Instance` actions — so the page accurately represents beta runtime state and never
  shows a false "no mt5_instance" for a beta account. Preserve the legacy behaviour for legacy (Nuno) accounts.
- **Test required:** a beta account with an `AccountRuntime` (any state) renders a truthful runtime status on the
  accounts page and returns **no** "no mt5_instance assigned" error; a legacy account is unchanged.
- **Acceptance criteria:** accounts page reflects `AccountRuntime` for beta accounts; legacy accounts unaffected;
  no regression to Customer Zero's onboarding/provisioning path.
- **Evidence:** `docs/CUSTOMER_ZERO_EVIDENCE_MATRIX.md` (Stage 8); prod observation 2026-07-30.
