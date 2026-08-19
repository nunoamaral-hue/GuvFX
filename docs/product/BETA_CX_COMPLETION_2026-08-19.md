# Beta customer experience completion — 2026-08-19

## Scope and boundaries

This increment is presentation-only: customer-facing frontend copy, EN/JA catalogue wiring, and the embedded MT5 RemoteApp viewport. It does not change hosted transport, tenant provisioning, Guacamole authentication, execution authorization, order routing/claiming, workers, bridges, MT5 order handling, or lot-sizing semantics.

## Collision gate

Baseline: `origin/main` at `c21bfb3f1d9f3df67155fa12a5d692b58aed702f`.

- PR #371 (draft Telegram notifications) overlaps only in the shared i18n catalogue and shared operational documentation. This increment does not touch Telegram code, migrations, worker activation, webhook activation, or notification delivery. A small catalogue/doc rebase may be needed if #371 lands first.
- PR #343 is hosted-certificate documentation only and has no changed-path collision with this increment.
- PR #304 overlaps shared programme status documents. To avoid colliding with that stream, this increment records its evidence in this dedicated product note and does not edit `STATUS.md` or `NEXT.md`.
- No protected execution-plane files are changed.

## Defects fixed

- Hosted journey state models returned render-ready English, causing the Japanese `/onboarding/hosted` shell to surround an English central panel. They now return catalogue keys and parameters only.
- Backend-provided English lifecycle labels/details leaked into Japanese onboarding and broker-connection status. Customer surfaces now map structured phase/step codes to EN/JA catalogue copy.
- Broker, readiness, strategy-assignment, registration, account-status loading/error, session-verification, and primary Terminal Access states contained hard-coded or locally branched English.
- Marketplace summaries, styles, execution labels, category pills, tags, and Free pricing were rendered from English seed data. Visible content now resolves through EN/JA catalogue keys.
- Generic Configure copy now uses the catalogue. Stale “Managed by GuvFX” sizing copy was aligned with the already-existing customer-owned per-position control; sizing calculations, limits, persistence, and execution behaviour are unchanged.

## Regression contract

`frontend/src/lib/beta-customer-copy.test.ts` parses the TypeScript/TSX syntax tree for required closed-beta surfaces and rejects:

- customer-visible English JSX text;
- literal English `title`, `aria-label`, or `placeholder` attributes;
- literal customer error/info/notice state;
- render-ready `title`, `body`, `label`, `description`, or `message` model fields.

It also asserts that hosted journey models expose `titleKey`, `descriptionKey`, and `labelKey`, not prose. The guard is intentionally bounded to required beta surfaces to avoid repository-wide false positives.

## Full-screen MT5 design

The existing RemoteApp card gains a presentation-only full-screen state:

- `Full Screen` / `Exit Full Screen` in EN and `全画面表示` / `全画面表示を終了` in JA;
- browser Fullscreen API as progressive enhancement;
- fixed, viewport-maximized in-app fallback when browser permission is denied or unsupported;
- minimal header with connection state and exit control;
- the same iframe node, key, source, account, and delivery descriptor across expand/collapse;
- no delivery-connect call and no iframe remount on the normal → full-screen → normal transition;
- 390px mode hides the long focus hint but retains connection and exit controls.

Mobile is certified for control access and containment, not for desktop-equivalent MT5 chart ergonomics. A desktop/laptop/tablet viewport remains the recommended terminal experience.

## Visual evidence

The screenshots use deterministic owner-scoped API fixtures and a non-secret mock terminal canvas. The capture checks `documentElement.scrollWidth <= innerWidth` before writing each image.

### Hosted workspace preparing

- [EN desktop](../evidence/beta-cx-2026-08-19/hosted-preparing-desktop-en.png)
- [JA desktop](../evidence/beta-cx-2026-08-19/hosted-preparing-desktop-ja.png)
- [EN 390px](../evidence/beta-cx-2026-08-19/hosted-preparing-390-en.png)
- [JA 390px](../evidence/beta-cx-2026-08-19/hosted-preparing-390-ja.png)

### Terminal full-screen

- [EN desktop](../evidence/beta-cx-2026-08-19/terminal-fullscreen-desktop-en.png)
- [JA desktop](../evidence/beta-cx-2026-08-19/terminal-fullscreen-desktop-ja.png)
- [EN 390px](../evidence/beta-cx-2026-08-19/terminal-fullscreen-390-en.png)
- [JA 390px](../evidence/beta-cx-2026-08-19/terminal-fullscreen-390-ja.png)

### Required journey sweep

| Surface | EN desktop | JA desktop | EN 390px | JA 390px |
|---|---|---|---|---|
| Login | [view](../evidence/beta-cx-2026-08-19/login-desktop-en.png) | [view](../evidence/beta-cx-2026-08-19/login-desktop-ja.png) | [view](../evidence/beta-cx-2026-08-19/login-390-en.png) | [view](../evidence/beta-cx-2026-08-19/login-390-ja.png) |
| Registration | [view](../evidence/beta-cx-2026-08-19/registration-desktop-en.png) | [view](../evidence/beta-cx-2026-08-19/registration-desktop-ja.png) | [view](../evidence/beta-cx-2026-08-19/registration-390-en.png) | [view](../evidence/beta-cx-2026-08-19/registration-390-ja.png) |
| Accounts | [view](../evidence/beta-cx-2026-08-19/accounts-desktop-en.png) | [view](../evidence/beta-cx-2026-08-19/accounts-desktop-ja.png) | [view](../evidence/beta-cx-2026-08-19/accounts-390-en.png) | [view](../evidence/beta-cx-2026-08-19/accounts-390-ja.png) |
| Marketplace | [view](../evidence/beta-cx-2026-08-19/marketplace-desktop-en.png) | [view](../evidence/beta-cx-2026-08-19/marketplace-desktop-ja.png) | [view](../evidence/beta-cx-2026-08-19/marketplace-390-en.png) | [view](../evidence/beta-cx-2026-08-19/marketplace-390-ja.png) |
| Configure Wayond | [view](../evidence/beta-cx-2026-08-19/configure-wayond-desktop-en.png) | [view](../evidence/beta-cx-2026-08-19/configure-wayond-desktop-ja.png) | [view](../evidence/beta-cx-2026-08-19/configure-wayond-390-en.png) | [view](../evidence/beta-cx-2026-08-19/configure-wayond-390-ja.png) |
| My Strategies | [view](../evidence/beta-cx-2026-08-19/my-strategies-desktop-en.png) | [view](../evidence/beta-cx-2026-08-19/my-strategies-desktop-ja.png) | [view](../evidence/beta-cx-2026-08-19/my-strategies-390-en.png) | [view](../evidence/beta-cx-2026-08-19/my-strategies-390-ja.png) |

The 390px sweep found and fixed an account-card action overlap in Japanese. Re-capture confirmed the status and MT5 connection controls now stack without clipping.

## CX classification

- P0 remaining: none found in the active closed-beta customer path.
- P1 before wider beta: native/Sponsor review of newly catalogued Japanese marketplace trading terminology; finish EN/JA conversion of the feature-dark broker-connectivity replacement before enabling that flag for customers.
- P2 deferred: translate the legacy non-hosted full-desktop terminal recovery panel; preserve English product/codename strings where they are proper names; improve phone-sized MT5 chart ergonomics if mobile terminal use becomes a product requirement.

## Telegram PR #371

Assessment only: this increment neither duplicates nor activates Telegram. If this increment lands first, #371 should rebase and retain both sets of additive i18n keys. Bot, webhook, worker, migration, and customer-notification activation remain outside this packet.
