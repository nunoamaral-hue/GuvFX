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

## Beta-critical EN ↔ JA glossary

| English product term | Japanese beta term | Usage note |
|---|---|---|
| Hosted Workspace | ホステッドワークスペース | The managed workspace product; do not alternate with generic hosting terminology. |
| Hosted MT5 | ホステッドMT5 | The managed MT5 customer experience. Use `MetaTrader` when referring to the application itself. |
| Marketplace | マーケットプレイス | Strategy discovery surface. |
| Get Strategy | 戦略を追加 | Adds a strategy to the selected account; it does not enable automated trading. |
| Configure | 設定 | Reviews the strategy contract and available customer actions. |
| My Strategies | 利用中の戦略 | Customer-owned and enabled/disabled strategy list. |
| Enable Strategy | 戦略を有効にする | Explicitly consents to automated trading for the named strategy and demo account. |
| Disable Strategy | 戦略を停止する | Stops automated trading for the named strategy. |
| Automated trading | 自動売買 | Use consistently for the customer-authorised trading state. |
| Demo account | デモ口座 | State plainly that demo trading does not use real funds; avoid outcome guarantees. |
| Needs attention | 確認が必要です | Actionable state that routes the customer to the next step or support. |
| Trading account | 取引口座 | Broker/MT5 account. Reserve `アカウント` for the GuvFX user account. |

## Visual acceptance evidence

Desktop evidence was captured at 1440×1000. The post-rebase mobile acceptance evidence was captured at
390×844 against the current production-mode frontend bundle using deterministic local API fixtures. No
production customer account, production data, or trading-state mutation was used.

| Customer state | English | Japanese |
|---|---|---|
| Hosted onboarding | `evidence/en-ja/01-hosted-onboarding-en.png` | `evidence/en-ja/01-hosted-onboarding-ja.png` |
| Terminal / account ready | `evidence/en-ja/02-terminal-account-ready-en.png` | `evidence/en-ja/02-terminal-account-ready-ja.png` |
| Marketplace | `evidence/en-ja/03-marketplace-en.png` | `evidence/en-ja/03-marketplace-ja.png` |
| Wayond Configure | `evidence/en-ja/04-wayond-configure-en.png` | `evidence/en-ja/04-wayond-configure-ja.png` |
| Enable Strategy confirmation | `evidence/en-ja/05-enable-confirmation-en.png` | `evidence/en-ja/05-enable-confirmation-ja.png` |
| My Strategies | `evidence/en-ja/06-my-strategies-en.png` | `evidence/en-ja/06-my-strategies-ja.png` |
| Needs attention | `evidence/en-ja/07-needs-attention-en.png` | `evidence/en-ja/07-needs-attention-ja.png` |
| Wayond Configure — 390px | `evidence/en-ja/08-mobile-configure-en.png` | `evidence/en-ja/08-mobile-configure-ja.png` |
| Enable consent — 390px | `evidence/en-ja/09-mobile-consent-en.png` | `evidence/en-ja/09-mobile-consent-ja.png` |
| Risk consent — 390px | `evidence/en-ja/10-mobile-risk-consent-en.png` | `evidence/en-ja/10-mobile-risk-consent-ja.png` |

The three mobile pairs were checked with a 390px viewport and each reported `scrollWidth === innerWidth`.
The Japanese Configure, Enable consent, and risk-consent states contained no customer-facing English leakage;
product names and standard identifiers such as GuvFX, Wayond, MT5, MetaTrader, XAUUSD, and M15 remain unchanged.

## Remaining backlog after P0

- **P1:** billing/subscription, invoices/usage, profile/settings, analytics/charts, and remaining dashboard,
  account-detail, strategy-detail/create, and Terminal Access management fragments.
- **P1:** obtain an independent native-Japanese reviewer sign-off before expanding beyond the closed beta; the
  P0 editorial pass and beta-critical glossary are complete in this PR.
- **P1:** extend mobile-width EN/JA evidence beyond the P0 journey to the broader customer route matrix.
- **P2:** introduce a reviewed per-surface hard-coded-copy rule and consider splitting the dictionary by domain.

## Scope and collision gate

- Reconciled baseline: `origin/main` at `6cba7e6a01aa712e8f384fa571ff971cbaf3e2d6`.
- Repository/authentication: `nunoamaral-hue/GuvFX`, authenticated as `nunoamaral-hue`.
- Open PRs at the fresh gate: #367 (this stream), #343, and #304. The latter two are hosted-workspace
  documentation only and neither touches frontend i18n.
- Main advances since the original #367 base were reviewed through #368 plus `6cba7e6`. PR #368 added current
  CX work; `6cba7e6` changed only authoritative hosted execution username handling and its backend tests.
- Collision result: zero changed-path overlap with current main and the open PRs. The branch was rebased cleanly;
  no protected execution, hosted provisioning, production data, flags, or infrastructure is touched.

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
