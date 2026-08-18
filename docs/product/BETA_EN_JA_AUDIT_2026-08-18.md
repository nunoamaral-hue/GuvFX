# English/Japanese beta translation audit — 2026-08-18

## P0 implementation result

The closed-beta activation journey now has practical English/Japanese parity. The implementation localizes
core plan selection and onboarding framing, Hosted onboarding (including waiting, confirmation, unavailable,
and retry states), Hosted MT5 launch states, Hosted Workspace account status, Wayond Configure and its managed
settings, activation consent, My Strategies lifecycle states, and customer-safe errors on those paths.

The contract now checks non-empty EN/JA pairs, referenced-key resolution, interpolation parity, locale helpers,
and meaningful Japanese rendering for critical components. Locale-sensitive dates in My Strategies use the
selected language rather than the browser default. This is a P0 journey certification, not a claim that every
customer route in the product is translated.

## Visual acceptance evidence

All evidence was captured at 1440×1000 from a localhost preview using deterministic fixtures and intercepted
local-preview API responses. No production customer account or production data was used.

| Customer state | English | Japanese |
|---|---|---|
| Hosted onboarding | `evidence/en-ja/01-hosted-onboarding-en.png` | `evidence/en-ja/01-hosted-onboarding-ja.png` |
| Terminal / account ready | `evidence/en-ja/02-terminal-account-ready-en.png` | `evidence/en-ja/02-terminal-account-ready-ja.png` |
| Marketplace | `evidence/en-ja/03-marketplace-en.png` | `evidence/en-ja/03-marketplace-ja.png` |
| Wayond Configure | `evidence/en-ja/04-wayond-configure-en.png` | `evidence/en-ja/04-wayond-configure-ja.png` |
| Enable Strategy confirmation | `evidence/en-ja/05-enable-confirmation-en.png` | `evidence/en-ja/05-enable-confirmation-ja.png` |
| My Strategies | `evidence/en-ja/06-my-strategies-en.png` | `evidence/en-ja/06-my-strategies-ja.png` |
| Needs attention | `evidence/en-ja/07-needs-attention-en.png` | `evidence/en-ja/07-needs-attention-ja.png` |

## Remaining backlog after P0

- **P1:** billing/subscription, invoices/usage, profile/settings, analytics/charts, and remaining dashboard,
  account-detail, strategy-detail/create, and Terminal Access management fragments.
- **P1:** a native Japanese reviewer should approve the glossary and consent/risk wording before a wider launch.
- **P1:** add mobile-width EN/JA screenshots for the broader customer route matrix.
- **P2:** introduce a reviewed per-surface hard-coded-copy rule and consider splitting the dictionary by domain.

## Scope and collision gate

- Baseline: `origin/main` at `4224486e8e1433327dd4065e86820efecbe8ebbe`.
- Repository/authentication: `nunoamaral-hue/GuvFX`, authenticated as `nunoamaral-hue`.
- Open PRs at the gate: #343 and #304, both hosted-workspace documentation only. Neither touches frontend i18n.
- Recent merged work was reviewed through #366. The latest customer-journey change (#358) touched onboarding,
  but is already merged into the audited baseline. Current execution/node work is outside this increment.
- Collision result: clear for `frontend/src/lib/i18n.ts`, its contract tests, and this audit. No protected execution,
  hosted provisioning, production data, flags, or infrastructure is touched.

## Architecture and supported locales

The frontend uses a single TypeScript dictionary in `frontend/src/lib/i18n.ts`. `Lang` is the closed union
`"en" | "ja"`; preference resolution is cookie, local storage, browser language, then English. `AppShell`
provides the selected language through `useLang()`. Public pages manage the same preference directly. The
translation function falls back to English, then the key. The backend is configured `en-us` and does not
provide a customer-facing gettext catalogue; customer-safe error codes are expected to be translated by the
frontend, but several screens still display caught exception text directly.

At this baseline the dictionary has 717 entries. Every entry has both `en` and `ja`, and all 433 unique
statically referenced keys resolve. That is dictionary parity, not customer-surface parity.

## Customer routes audited

Routes already wired to the dictionary (some still contain hard-coded fragments): landing, login, register,
how-it-works, pricing, dashboard, accounts list, marketplace, strategy create/detail/edit, backtests list/detail,
live trading, and trade history.

Customer routes with no page-level i18n wiring are billing, hosting, invoices, usage, account detail,
strategy lab, strategy metrics, charts, onboarding, hosted onboarding, profile, Configure, My Strategies,
Terminal Access, and the broker-account route pair. Admin and operations routes were classified as internal and
excluded from beta parity.

## Findings

### P0 — closed-beta journey gaps

1. Hosted onboarding is English-only. `HostedWorkspaceJourney`, `OnboardingShell`, and onboarding step
   components hard-code loading, success, preparation, failure, retry, and CTA text. This includes the main
   Hosted MT5 journey and its error states.
2. Configure and My Strategies are English-only at page level, including enable/disable states, setup routing,
   loading/failure messages, and strategy lifecycle copy. These are core Wayond beta surfaces.
3. Terminal Access and account-detail components are English-only, including viewer states, credential states,
   retry/replace actions, and Hosted Workspace status.
4. Backend/network exception messages are sometimes assigned directly to customer alerts. This can expose
   English or implementation detail and has no stable code-to-copy translation boundary on those screens.
5. There was no automated contract preventing a referenced key from being removed, an EN/JA value from becoming
   blank, or interpolation variables from diverging. The first increment adds that gate.

### P1 — broad customer parity and terminology

- Billing, invoices, usage, profile, analytics, charts, and broker-account components are English-only.
- Hard-coded English remains inside otherwise translated routes, especially the dashboard, strategy detail,
  strategy creation option labels, and trade-history summary labels.
- Terminology is inconsistent: Marketplace is both `マーケット` and product copy that implies a marketplace;
  Trading Intelligence is rendered as `トレーディングAI`; Billing & Plans mixes English punctuation in
  `請求 & プラン`; Account alternates between `アカウント` and the more domain-natural `口座` without a documented
  distinction. A reviewed glossary should reserve `口座` for broker/trading accounts and `アカウント` for the
  GuvFX user account.
- A Japanese-native editorial pass is still required. Examples such as `戦略を探索` and terse noun chains are
  understandable but can read like literal translations without their full UI context.

### P1 — locale-sensitive values

- Billing, invoices, and Terminal Access explicitly format dates as `en-US`.
- Other customer screens use the browser default rather than the selected GuvFX language, so an English UI can
  show Japanese formatting (or the reverse).
- Currency output is frequently assembled from symbols/strings instead of `Intl.NumberFormat`; JPY precision,
  symbol placement, and grouping can therefore be wrong.

### P1 — layout/accessibility risks

- No EN/JA visual regression evidence exists for the principal beta journeys or their mobile breakpoints.
- Inline-styled CTA groups and fixed/minimum widths in cards may wrap unpredictably. Japanese usually contracts
  some labels but can expand explanatory copy and has different line-break opportunities.
- Several icon/tool-tip labels and modal/error controls remain hard-coded, so screen-reader output does not
  follow the selected language.

### P2 — test and maintenance gaps

- No route inventory asserts that every beta-customer route opts into i18n.
- No hard-coded customer-copy lint exists. A naive global ban would create noise from proper names, symbols,
  test fixtures, and internal/admin screens; introduce it per migrated surface with a reviewed allow-list.
- No screenshot matrix covers English/Japanese at desktop and mobile widths.
- The dictionary is a single 2,600-line file; keep it for beta stability, but consider domain modules only in a
  separately approved post-beta refactor.

## Ranked implementation increments

1. **P0: parity contract and locale helpers (this PR).** Test non-empty EN/JA values, referenced-key existence,
   and interpolation parity; centralize explicit `en-GB`/`ja-JP` formatting. This prevents silent regression
   while customer surfaces are migrated.
2. **P0: core activation journey.** Translate Configure, My Strategies, Enable modal, Hosted onboarding,
   onboarding steps, Hosted Workspace status, and customer-safe error mapping. Add journey tests in both locales
   and desktop/mobile screenshots.
3. **P0: account and Hosted MT5 management.** Translate account detail, broker components, Terminal Access,
   viewer/session states, and credential modals. Use the locale helpers for all dates/numbers.
4. **P1: remaining customer routes.** Billing/invoices/usage/profile/dashboard residuals, analytics/charts,
   strategy detail/create residuals, backtests, and trade history. Add a customer-route i18n inventory gate.
5. **P1: Japanese editorial and visual certification.** Approve a terminology glossary with a native reviewer,
   then capture EN/JA desktop/mobile evidence for the closed-beta route matrix.
6. **P2: modularization and stricter hard-coded-copy lint.** Only after parity is achieved and the beta branch is
   stable.

## Certification boundary

This audit does **not** certify full EN/JA customer parity. It establishes the exact baseline and a regression
gate for the staged completion work. Production was not accessed or mutated, and no deployment was performed.
