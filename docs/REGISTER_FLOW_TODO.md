# Registration Flow Enhancement Plan

> **Status:** BACKLOG — Not yet implemented
> **Priority:** P2 (deferred until core platform stable)
> **Last updated:** 2026-01-26

## Summary

This document outlines a multi-step registration flow for GuvFX designed to:

1. **Build trust** — Guide users through account setup without overwhelming them
2. **Ensure legal compliance** — Capture necessary acknowledgments before feature access
3. **Maintain non-advisory positioning** — All copy reinforces "platform tools only, not financial advice"
4. **Gate features appropriately** — Certain capabilities require completed verification/compliance steps

The flow is designed to be investor-grade: professional, risk-aware, and free of performance promises.

---

## Proposed Registration Steps

| Step | Name | Required? | Description |
|------|------|-----------|-------------|
| 1 | Account Creation | **REQUIRED** | Email, password, optional username (currently implemented) |
| 2 | Email Verification | DEFERRED | Verify email ownership via token link |
| 3 | Hosting Selection | DEFERRED | Choose hosting region/tier for strategy execution |
| 4 | Profile & Compliance | DEFERRED | Basic profile info + legal acknowledgments |
| 5 | Security Setup | DEFERRED | Optional 2FA, recovery options |

### Step 1: Account Creation (IMPLEMENTED)

Already live. Creates user account with:
- Email (required, unique)
- Password (min 3 chars currently, should increase to 8+)
- Username (optional, defaults to email)

**Current UI:** Shows step indicator "Step 1 of 5", progress bar at 20%, "Coming Next" panel with locked items.

### Step 2: Email Verification (DEFERRED)

**Purpose:** Confirm email ownership before enabling sensitive features.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| verification_token | string | auto | Sent via email, 24h expiry |
| verified_at | timestamp | auto | Set when token confirmed |

**Gated features if unverified:**
- Cannot link live trading accounts
- Cannot deploy strategies to live accounts
- Can still use demo accounts and backtesting

### Step 3: Hosting Selection (DEFERRED)

**Purpose:** User selects where their strategies will execute.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| hosting_region | enum | yes | e.g., `EU_WEST`, `US_EAST`, `ASIA_TOKYO` |
| hosting_tier | enum | yes | e.g., `FREE`, `BASIC`, `PRO` |
| acknowledged_hosting_terms | boolean | yes | Must accept before proceeding |

**Gated features if not selected:**
- Cannot deploy live strategies (no execution environment)
- Can still backtest and paper trade

### Step 4: Profile & Compliance (DEFERRED)

**Purpose:** Capture profile data and legal acknowledgments.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| first_name | string | yes | For account identification |
| last_name | string | yes | For account identification |
| country | enum | yes | Determines regulatory requirements |
| acknowledged_risk_disclosure | boolean | yes | Must accept risk disclosure |
| acknowledged_platform_terms | boolean | yes | Must accept ToS |
| acknowledged_not_advice | boolean | yes | Explicit "not financial advice" confirmation |

**Legal acknowledgment text examples:**
- "I understand that GuvFX provides strategy management tools only and does not provide investment advice."
- "I understand that trading in financial instruments carries risk and past performance does not guarantee future results."
- "I confirm that I am solely responsible for all trading decisions made using this platform."

### Step 5: Security Setup (DEFERRED)

**Purpose:** Optional but encouraged security hardening.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| totp_enabled | boolean | no | 2FA via authenticator app |
| totp_secret | string | no | Encrypted, set during setup |
| recovery_email | string | no | Backup email for account recovery |
| recovery_codes_generated | boolean | no | One-time recovery codes |

---

## Feature Gating Table

| Feature | Step 1 | Step 2 | Step 3 | Step 4 | Step 5 |
|---------|--------|--------|--------|--------|--------|
| Dashboard access | ✓ | ✓ | ✓ | ✓ | ✓ |
| Backtest (historical) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Demo account linking | ✓ | ✓ | ✓ | ✓ | ✓ |
| Strategy creation | ✓ | ✓ | ✓ | ✓ | ✓ |
| Live account linking | — | ✓ | ✓ | ✓ | ✓ |
| Live strategy deployment | — | ✓ | ✓ | ✓ | ✓ |
| Hosting provisioning | — | — | ✓ | ✓ | ✓ |
| Full platform access | — | — | — | ✓ | ✓ |

---

## Security Considerations

### Rate Limiting

| Endpoint | Limit | Window | Notes |
|----------|-------|--------|-------|
| POST /api/auth/register/ | 5 | 15 min | Per IP |
| POST /api/auth/verify-email/ | 10 | 15 min | Per user |
| POST /api/auth/resend-verification/ | 3 | 1 hour | Per user |
| POST /api/auth/2fa/setup/ | 5 | 15 min | Per user |

### Token Security

- Email verification tokens: 32-byte random, URL-safe base64, 24h expiry
- TOTP secrets: 20-byte random, encrypted at rest
- Recovery codes: 8 codes, 10 chars each, bcrypt hashed

### Audit Logging

All registration steps should be logged:
- User ID
- Step completed
- IP address
- User agent
- Timestamp
- Success/failure status

---

## Backend Endpoints Needed

| Endpoint | Method | Purpose | Status |
|----------|--------|---------|--------|
| /api/auth/register/ | POST | Create account | IMPLEMENTED |
| /api/auth/verify-email/ | POST | Verify email token | NOT STARTED |
| /api/auth/resend-verification/ | POST | Resend verification email | NOT STARTED |
| /api/auth/hosting/select/ | POST | Set hosting preferences | NOT STARTED |
| /api/auth/profile/ | PATCH | Update profile & compliance | NOT STARTED |
| /api/auth/2fa/setup/ | POST | Initialize 2FA setup | NOT STARTED |
| /api/auth/2fa/verify/ | POST | Confirm 2FA setup | NOT STARTED |
| /api/auth/registration-status/ | GET | Get current step completion | NOT STARTED |

---

## Database Schema Additions

### User Model Extensions

```python
# These fields would be added to the User model or a related UserProfile model

# Email verification
email_verified = models.BooleanField(default=False)
email_verified_at = models.DateTimeField(null=True, blank=True)
email_verification_token = models.CharField(max_length=64, null=True, blank=True)
email_verification_sent_at = models.DateTimeField(null=True, blank=True)

# Hosting
hosting_region = models.CharField(max_length=32, null=True, blank=True)
hosting_tier = models.CharField(max_length=32, null=True, blank=True)
hosting_terms_accepted_at = models.DateTimeField(null=True, blank=True)

# Profile & Compliance
country = models.CharField(max_length=2, null=True, blank=True)  # ISO 3166-1 alpha-2
risk_disclosure_accepted_at = models.DateTimeField(null=True, blank=True)
platform_terms_accepted_at = models.DateTimeField(null=True, blank=True)
not_advice_acknowledged_at = models.DateTimeField(null=True, blank=True)

# Security
totp_enabled = models.BooleanField(default=False)
totp_secret = models.CharField(max_length=255, null=True, blank=True)  # encrypted
recovery_email = models.EmailField(null=True, blank=True)
recovery_codes_hash = models.TextField(null=True, blank=True)  # JSON array of hashes

# Registration progress
registration_step = models.PositiveSmallIntegerField(default=1)
registration_completed_at = models.DateTimeField(null=True, blank=True)
```

---

## Frontend Implementation Notes

### State Management

- Registration progress stored in backend (source of truth)
- Frontend fetches `/api/auth/registration-status/` on mount
- Local state for form inputs within each step
- No localStorage for sensitive data

### URL Step Tracking

Option A: Query param (`/register?step=2`)
- Allows bookmarking/sharing
- Risk: users may manipulate step param

Option B: Single URL, backend-driven step (`/register`)
- Cleaner URLs
- Step determined by server response
- Recommended approach

### Persistence & Resume

- User can leave and return; progress saved server-side
- Show "Resume registration" banner on dashboard if incomplete
- Allow skipping optional steps (e.g., 2FA) with explicit "Skip for now" button

### Progress Indicator

Current implementation shows:
- Step indicator: "Step 1 of 5"
- Progress bar: 20% per step
- "Coming Next" panel with locked items

This pattern should continue for steps 2-5.

---

## Non-Scope (Explicitly Excluded)

The following are **NOT** in scope for this plan:

- **Actual email verification implementation** — Requires email service integration
- **Working 2FA** — Requires TOTP library integration and encrypted secret storage
- **Hosting billing** — Requires payment integration
- **Compliance gating enforcement** — Requires backend middleware
- **KYC/identity verification** — Out of scope entirely
- **Regulatory reporting** — Out of scope entirely
- **Multi-language legal documents** — Only EN/JP placeholder text for now

---

## i18n Key Reservations

The following keys are reserved in `frontend/src/lib/i18n.ts` for future use:

```
register.step2Title
register.step2Subtitle
register.step2Note
register.verifyEmail
register.verificationSent
register.resendVerification
register.step3Title
register.step3Subtitle
register.step3Note
register.selectRegion
register.selectTier
register.hostingTerms
register.step4Title
register.step4Subtitle
register.step4Note
register.riskDisclosure
register.platformTerms
register.notAdviceAck
register.step5Title
register.step5Subtitle
register.step5Note
register.setup2FA
register.skipForNow
register.generateRecoveryCodes
register.registrationComplete
```

---

## Implementation Order (When Ready)

1. **Phase 1:** Add `registration_step` field to User model, create status endpoint
2. **Phase 2:** Implement email verification (requires email service)
3. **Phase 3:** Add hosting selection UI (backend can be stubbed)
4. **Phase 4:** Add profile/compliance step with legal acknowledgments
5. **Phase 5:** Add optional 2FA setup
6. **Phase 6:** Implement feature gating middleware
7. **Phase 7:** Add "resume registration" prompts throughout app

---

## Related Documents

- `docs/DECISIONS.md` — Architecture decisions
- `docs/KNOWN_ISSUES.md` — Current issues
- `frontend/src/app/register/page.tsx` — Current registration implementation
- `frontend/src/lib/i18n.ts` — Internationalization dictionary
