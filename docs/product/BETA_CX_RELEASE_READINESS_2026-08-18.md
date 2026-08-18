# GuvFX closed-beta customer-experience release readiness

Date: 2026-08-18  
Scope: customer-facing closed-beta journey only  
Verdict: **READY for Programme Director review with 0 open P0 customer-experience blockers**  
Explicit exclusions: production, Customer Zero, support acceptance, signal ingestion/promotion, execution jobs, routing/order send, nodes, workers, bridges, MT5 runtime, execution authorization and assignment semantics.

## 1. Provenance and collision gate

- `CURRENT_MAIN_SHA`: `4224486e8e1433327dd4065e86820efecbe8ebbe`
- Base was fetched before work and again before this report; it did not move.
- Open PRs at the second gate:
  - #367 — `feat/beta-en-ja-parity` into `main`; EN/JA beta activation journey; CI green at audit time.
  - #343 — hosted-workspace host certification status/evidence documentation.
  - #304 — hosted-workspace ADRs, roadmap, `docs/NEXT.md`, and `docs/STATUS.md`.
- #367 protected every file in its PR, notably the i18n dictionary/tests, AppShell, hosted journey/status/terminal, onboarding shell/progress/plan selection, Configure, Enable modal, and My Strategies.
- #343 and #304 documentation paths were also protected.
- Sacred execution paths were protected regardless of PR ownership: signal intake/promotion, execution, worker/node identity, bridges, MT5/terminal runtime, hosted provisioning control plane, authorization/assignment semantics, production configuration, and Customer Zero/support acceptance state.
- This stream has **zero path overlap with #367, #343, or #304**.
- Safe working surface used: public login/register/support, verification/risk/2FA presentation, safe error mapping, public-route parity inventory, tests, this report, and screenshot evidence.

## 2. Journey transition forensic

Loading and error descriptions below are customer-visible behavior; backend/operator detail remains outside the UI.

| ID | Source | CTA/event | Destination | Required state | Loading state | Error and retry | Abandon/return behavior |
|---|---|---|---|---|---|---|---|
| A | Landing | Create account / Log in | `/register` / `/login` | None | Normal navigation | Browser-level retry | Public routes remain directly addressable |
| B | Register | Continue | `/onboarding` | Valid identity fields; successful registration/login | Button disables and shows creating state | Safe duplicate or generic message; correct fields and retry | Authenticated revisit resumes onboarding; anonymous revisit retains no false progress |
| C | Email verification | Send / verify code | Next onboarding step | Authenticated, unverified user | Send/verify controls disable during request | Expired, used, invalid, send failure mapped safely; resend/re-enter | Durable server verification state survives refresh/login |
| D | Login | Continue | Explicit safe `returnTo`, otherwise server `setup-status.next_route` | Valid credentials | Button disables and shows sign-in progress | Generic credential failure; retry in place | Resumes hosted/onboarding/marketplace/strategies/dashboard as appropriate |
| E | Plan selection | Standard / Hosted | Standard account flow or `/onboarding/hosted` | Earlier required onboarding steps complete | Selected action is busy/guarded | Existing inline failure/retry | Server state, not browser-only state, determines the resumed step |
| F | Hosted request | Set up hosted workspace | Hosted journey on same route | Hosted plan and no workspace | Request-in-progress state; duplicate action guarded | Customer-safe retry/support | Setup-status routes a returning user back to hosted journey |
| G | Hosted provisioning | Automatic poll | Same route, advancing progress | Requested/preparing workspace | Progress panel; 5-second polling | Temporary read failure offers Try again; unavailable offers support | Reload/refocus/login re-reads durable backend journey |
| H | Awaiting login | Open MetaTrader / continue | Embedded terminal, then journey advances | Workspace awaiting broker login | Terminal connection feedback plus continued polling | Retry/reopen/support path | Backend journey preserves waiting state |
| I | Account confirmation | Confirm account | Hosted ready state | Detected account/login requiring confirmation | Confirmation action guarded | Failure remains actionable; retry | Confirmation is server-backed and survives reload |
| J | Hosted ready | Choose strategy | `/strategies/marketplace` | `WORKSPACE_READY`, confirmed and strategy-eligible | Normal navigation | Marketplace has its own load/retry state | Setup-status returns ready/no-strategy users to Marketplace |
| K | Marketplace | Get Strategy | Configure with marketplace/account query | Eligible workspace and account | Catalogue/account loading feedback | Safe retry or support guidance | Browser back returns to Marketplace without losing backend state |
| L | Configure | Enable strategy | Confirmation modal | Owned/eligible strategy and selected account | Configuration/account readiness feedback | Safe retry/support; new `/support` route removes prior 404 | Query state is recoverable; back link returns to Marketplace |
| M | Enable confirmation | Enable Strategy | `/strategies?enabled=1` | Explicit customer confirmation | Primary action disables while submitting | Modal displays safe failure and Try again | Double-click guarded; server assignment determines return state |
| N | My Strategies | Manage / disable where allowed | Strategy management | Owned strategy | Card/status loading | Safe attention/support states | Owned-disabled returns here; enabled returns here or dashboard via setup router |
| O | Hosted terminal | Open/reopen terminal | Embedded terminal panel | Hosted terminal delivery ready | Connection/detection feedback | Retry and support paths | Reopen does not reset onboarding or strategy state |
| P | Logout/login | Log out, then log in | `/login`, then durable setup route | Existing account | Session/login feedback | Safe login retry | No treasure hunt: setup-status selects hosted, marketplace, strategies, or dashboard |

### Defect classification

| Finding | Class | Result |
|---|---|---|
| Configure linked to a nonexistent `/support` page | `DEAD_END` / broken navigation | Fixed with a localized public support route and route-inventory test |
| Login defaulted every ordinary return to `/dashboard` | `STATE_DESYNC` / hidden next action | Fixed by using durable setup status unless a validated explicit return target exists |
| Register and login used desktop-fixed surfaces at 390px | hidden/inaccessible primary action risk | Fixed responsive stacking, wrapping, and width constraints |
| Auth/onboarding steps exposed arbitrary API exception text | `TECHNICAL_COPY` | Fixed with reviewed allow-list mappings and generic safe fallbacks |
| Japanese registration retained English field/setup labels | misleading/incomplete localization | Fixed without touching #367 files; regression tested |

No reciprocal navigation loop, broken back-navigation P0, or progress-loss P0 remains in the audited path.

## 3. Asynchronous hosted-workspace UX

| Required state | Customer representation | Automatic progress | Customer action | Failure/recovery |
|---|---|---|---|---|
| `REQUESTED` | Request received / workspace starting | Journey is polled | None | Retry the read; support if persistently unavailable |
| `PROVISIONING` | Workspace preparing with progress | 5-second polling | Wait; page can remain open | Temporary API failure is retryable |
| `WAITING_FOR_LOGIN` | MetaTrader login is required | Polling continues | Open terminal and log in | Reopen terminal, retry, or contact support |
| `CONNECTED` | Broker connection/account confirmation progress | Polling continues | Confirm the detected account when asked | Retry confirmation; state remains server-backed |
| `WORKSPACE_READY` | Ready panel and Choose Strategy | Terminal/status may refresh independently | Choose a strategy | Marketplace retry/support behavior applies |
| `EXECUTION_READY` | Represented only as customer eligibility within the ready projection; internal execution terms are not exposed | Backend read model supplies current readiness | No operator knowledge required | Fail-closed customer attention/support state |
| `FAILED/UNAVAILABLE` | Customer-safe unavailable panel | No false progress | Try again for a read failure; contact support for terminal failure | Direct support route/mail action; no dead end |

Proof points:

- Polling is keyed to autonomous phases and stops for ready/confirmation/support terminal states.
- Refresh, browser reload, and logout/login re-read the journey from the server; progress is not held only in React/local storage.
- Duplicate request/confirmation actions are disabled while in flight.
- Setup-status tests pin pre-ready hosted users to `/onboarding/hosted`, ready/no-strategy users to Marketplace, owned-disabled users to Strategies, and completed users to Dashboard.
- A preview using real components and safe mocked API state exercised preparing, ready, failure/retry, and the major activation surfaces without production authentication.

## 4. Customer-visible error audit

Added `customerSafeError`, which returns only an explicitly reviewed mapping or a caller-supplied generic message. It never displays an arbitrary backend detail. Applied to:

- login;
- registration;
- email verification;
- risk acceptance;
- two-factor setup.

The original exception remains available to application logging/call-site diagnostics where already logged; it is not interpolated into customer copy. Configure/enable/hosted error files owned by #367 were inspected but not edited.

## 5. Email and notification readiness

No production email was sent.

| Communication | Classification | Customer-visible | Beta blocking | Disposition |
|---|---|---:|---:|---|
| Registration/welcome email | `NOT_IMPLEMENTED` | No | No | P2; verification is the necessary first communication |
| Email verification | `IMPLEMENTED` | Yes | Yes, where verification applies | Genuine multipart email, configured sender/reply-to, 24-hour token, tested send failure |
| Password reset | `NOT_IMPLEMENTED` | No | No for supported closed beta | P1; authenticated password change exists, recovery is support-mediated during beta |
| Hosted workspace requested | `NOT_IMPLEMENTED` | No | No | P2; in-page durable status is sufficient |
| Workspace ready | `NOT_IMPLEMENTED` | No | No | P2; polling and return router are sufficient |
| Workspace failure | `NOT_IMPLEMENTED` | No | No | P2; in-page support route is sufficient |
| Strategy enabled | `NOT_IMPLEMENTED` | No | No | P2; immediate My Strategies confirmation exists |
| Strategy disabled | `NOT_IMPLEMENTED` | No | No | P2; immediate in-product state exists |
| Important account failure | Customer email `NOT_IMPLEMENTED`; operator alerting exists | Operator only | No | P2; product attention/support states remain the beta channel |

## 6. Responsive and visual acceptance

The combined prospective release (`#367` plus this branch) was rendered locally with real components and deterministic, non-production API mocks. The temporary preview harness was removed and is not committed.

- Desktop: 1440 × 1000.
- Tablet-ish: 900 × 1000 on Marketplace and Configure.
- Mobile: 390 × 844 on Japanese Register, Login, Configure, and My Strategies.
- Automated acceptance checked `documentElement` and body width for every capture: no horizontal overflow.
- Primary buttons, account/configuration detail, modal action, support/retry action, and long Japanese copy remained visible and usable.

Evidence directory: `docs/product/evidence/beta-cx-release-readiness/`

1. Register
2. Plan selection
3. Hosted provisioning
4. Hosted ready
5. Marketplace
6. Configure
7. Enable confirmation
8. My Strategies enabled
9. Provisioning failure/retry
10. Tablet Marketplace
11. Tablet Configure
12. Mobile Japanese Register
13. Mobile Japanese Login
14. Mobile Japanese Configure
15. Mobile Japanese My Strategies

## 7. Adversarial review

| Perspective | Attack | Outcome |
|---|---|---|
| First-time non-technical | Follow only visible primary CTAs | Complete path; no internal knowledge required |
| Impatient customer | Repeat request/confirm/enable actions | In-flight guards prevent ordinary duplicate action |
| Slow provisioning | Remain on page | Automatic polling and plain-language progress |
| Failed provisioning | Force journey API unavailable | Safe error, Try again, and support; no raw 503 detail |
| Returning customer | Close/reopen at each durable phase | Setup-status selects the correct next surface |
| Mobile customer | Use 390px viewport | No overflow or hidden primary CTA in sampled P0 surfaces |
| Japanese customer | Use long Japanese copy | Critical sampled path renders; registration hardcoded labels fixed |
| Double click | Repeat guarded primaries | Busy state prevents ordinary duplicate request |
| Refresh mid-action | Reload preparing/ready surfaces | Server state restores current phase |
| Temporary API failure | 503 journey read | Customer-safe retry without architecture terminology |

Adversarial result for this stream: **0 HIGH, 0 MEDIUM**. Remaining items below are LOW/P1 or post-beta enhancements.

## 8. Beta blocker matrix

| Area | Status | Severity | Beta blocker | Fixed | Deferred | Owner |
|---|---|---|---:|---:|---:|---|
| Support navigation | Ready | P0 / High before fix | Yes before fix | Yes | No | CX stream |
| Returning login routing | Ready | P0 / High before fix | Yes before fix | Yes | No | CX stream |
| Auth mobile responsiveness | Ready | P0 / Medium before fix | Yes before fix | Yes | No | CX stream |
| Auth/onboarding raw errors | Ready | P0 / Medium before fix | Yes before fix | Yes | No | CX stream |
| Japanese registration labels | Ready | P0 / Medium before fix | Yes before fix | Yes | No | CX stream |
| Password recovery | Support-mediated | P1 / Low | No | No | Yes | Product/Auth |
| Hosted copy says the customer will be notified when no ready notification exists | Functional path unaffected | P1 / Low | No | No; #367-owned file | Yes | #367 / Programme |
| Japanese hosted failure support CTA may use an English action label | Functional CTA remains visible | P1 / Low | No | No; #367-owned file | Yes | #367 / Programme |
| Lifecycle emails beyond verification | In-product feedback available | P2 / Low | No | No | Yes | Product |
| Production/live execution certification | Out of scope and untouched | Separate sacred stream | Not assessed here | No | Separate gate | Execution stream |

## 9. Release decision and boundaries

### Local quality gates

- `make check`: PASS.
- Governance: secret scan PASS; 18 no-secret tests PASS; 7 data-root tests PASS; 31 evidence manifests valid; 6 evidence-manifest tests PASS.
- Backend: 4,216 tests PASS, 1 skipped, against an isolated local test database. Test transports/mocks only; no external execution or production connection.
- Targeted onboarding/email backend suite: 32 tests PASS.
- Frontend: 41 files / 243 tests PASS.
- Frontend lint: PASS with 0 errors and 19 pre-existing warnings outside this stream.
- Frontend parity: PASS (46 routes, 54 components).
- Frontend production build: PASS, including the new `/support` route.
- Visual acceptance: 15 captures, all width assertions PASS.

- `P0_FIXED`: five independent customer blockers — support dead end, login return routing, auth mobile layout, raw errors, and Japanese registration gaps.
- `P0_REMAINING`: **0** within the customer-facing closed-beta scope.
- `P1_DEFERRED`: password reset; two low-risk #367 copy/localization residuals.
- `P2_DEFERRED`: optional lifecycle emails and broader post-beta notification UX.
- No execution-plane or production file changed. No trade was placed, manufactured, replayed, approved, claimed, or routed. No deployment, merge, production authentication, or real customer email occurred.
