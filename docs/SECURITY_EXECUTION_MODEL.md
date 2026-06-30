# GuvFX Security & Execution Model

**Version:** 1.0 (MVP)
**Status:** Execution Disabled by Design
**Last Updated:** 2025-02-10

---

## Executive Summary

GuvFX is a technology platform for strategy management and backtesting. This document describes the security controls implemented for MVP launch, with execution features intentionally disabled.

**Key Points:**
- All execution endpoints return 501 Not Implemented
- Comprehensive audit logging for all security-relevant events
- Rate limiting on all API endpoints
- CSRF protection on all mutating endpoints
- Security headers on all frontend routes

---

## 1. What We Implemented (MVP)

### 1.1 Audit Logging

**Model:** `core.models.AuditEvent`

An append-only audit log table capturing:
- User authentication events (login, logout, failed attempts)
- Strategy CRUD operations
- Backtest configuration and run creation
- Account linking/unlinking
- Strategy assignment operations
- Execution control attempts (logged as WARN)

**Key Properties:**
- UUID primary key
- Immutable records (save() blocks updates, delete() raises)
- Fail-open logging (never blocks business operations)
- Sensitive data sanitization in metadata

**Events Tracked:**

| Event Type | Severity | Trigger |
|------------|----------|---------|
| AUTH_LOGIN | INFO | Successful login |
| AUTH_LOGOUT | INFO | User logout |
| AUTH_FAILED | WARN | Failed login attempt |
| STRATEGY_CREATED | INFO | New strategy |
| STRATEGY_UPDATED | INFO | Strategy modified |
| STRATEGY_DELETED | INFO | Strategy removed |
| BACKTEST_CONFIG_CREATED | INFO | New backtest config |
| BACKTEST_RUN_CREATED | INFO | New backtest run |
| BACKTEST_PROCESSED | INFO | Batch processing |
| ASSIGNMENT_CREATED | INFO | Strategy assigned |
| EXECUTION_ENABLE_ATTEMPT | WARN | 501 stub called |
| EXECUTION_DISABLE_ATTEMPT | WARN | 501 stub called |
| EXECUTION_KILL_ATTEMPT | WARN | 501 stub called |
| RATE_LIMIT_EXCEEDED | WARN | Rate limit hit |

### 1.2 Rate Limiting

**Classes:** `core.throttling.GuvFXUserRateThrottle`, `GuvFXIPRateThrottle`

| Scope | Limit | Applies To |
|-------|-------|------------|
| User | 100/min | Authenticated users |
| IP | 1000/min | All requests |
| CSRF | 60/min | CSRF token endpoint |

**Cache Backend:** LocMemCache (default) or Redis (if configured via `REDIS_URL`)

### 1.3 CSRF Protection

**Configuration:**
- Double-submit cookie pattern
- `X-CSRFToken` header required on all mutating requests
- Cookie settings: HttpOnly=False (so JS can read), SameSite=Lax, Secure=True (prod)
- Trusted origins: guvfx.com, www.guvfx.com, api.guvfx.com

**Frontend Integration:**
- `apiFetch()` automatically fetches CSRF token before POST/PUT/DELETE
- Token attached via `X-CSRFToken` header

### 1.4 Execution Control Stubs

**Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/execution/enable/<account_id>/` | POST | Enable execution (stub, 501) |
| `/api/execution/disable/<account_id>/` | POST | Disable execution (stub, 501) |
| `/api/execution/kill-all/` | POST | Global kill switch — **functional (EXEC-E1a)** |

All attempts are logged to audit log with severity WARN.

**EXEC-E1a — kill switch is now functional for the signal-proposal path.**
`POST /api/execution/kill-all/` (admin-only) engages a DB-backed
`execution.ExecutionControl` singleton (`kill_switch_engaged`) and returns `200`.
The signal→proposal bridge (`execution.signal_proposals`) fails closed while it
is engaged, and the legacy `GUVFX_EXECUTION_DISABLED` env flag remains honoured
as defence-in-depth. Releasing the switch is **not** exposed over the API
(admin/server-side only) so the web surface can only fail safe. There is still no
live execution path for it to stop — proposals place no orders (see below).

### 1.4a EXEC-E1a — signal → ProposedSignalOrder bridge (no order)

The execution-side bridge turns an **APPROVED** `signal_intake.PendingSignalApproval`
into a **non-executable** `execution.ProposedSignalOrder` on a **demo** account.
A `ProposedSignalOrder` is **not** an `ExecutionJob` and has no PENDING/worker-claim
path, so the MT5 worker (`ExecutionJob.objects.filter(status=PENDING)`) can never
see it: "no order" is a structural guarantee, tested by asserting
`ExecutionJob.objects.count()` is unchanged. Gates: approved-only, kill switch /
signal disable, demo-only (rejects live accounts and live broker environments),
symbol allowlist, lot cap `SIGNAL_MAX_LOT_SIZE`, daily/concurrent caps, and
one-proposal-per-approval. All outcomes write an append-only
`execution.ProposalAuditEvent` linked to the approval. Full detail:
`backend/execution/SIGNAL_PROPOSALS.md`.

### 1.4b EXEC-HARDEN-JOBS — locked-down ExecutionJob creation

`ExecutionJob`s are created **only** through sanctioned, gated server-side
paths: strategy automation (`strategies.signal_engine` / schedulers), the
entitlement+ownership-gated `OpenTradeJobView` → `create_open_trade_job`, the
demo-only `CreateDemoTradeJobView`, and the staff `admin_ops` retry. The generic
DRF write surface on `ExecutionJobViewSet` (`POST/PUT/PATCH/DELETE`) is
**disabled** (405) — closing a pre-existing gap where an ordinary authenticated
user could post or mutate an order-bearing job directly. Order-defining fields
(`job_type`, `account`, `strategy`, `assignment`, `terminal_node`, `payload`)
are read-only on the serializer. The functional kill switch is now enforced at
the **model layer**: `ExecutionJob.save()` blocks creation of any
order-opening job type (`OPEN_TRADE` / `PLACE_ORDER` / `PLACE_TEST_ORDER`) while
`ExecutionControl.kill_switch_engaged` or `GUVFX_EXECUTION_DISABLED` is set —
covering every creation path, not just the proposal bridge. `CLOSE_TRADE` is
intentionally exempt so positions can still be flattened. Single source of truth:
`execution.models.order_creation_kill_reason`.

**EXEC-HARDEN-JOBS-R2 (follow-up).** The worker-protocol actions `next` (claim)
and `complete` on `ExecutionJobViewSet` are gated to validated worker credentials
(or staff) via `IsWorkerToken` — ordinary authenticated users can no longer claim
or complete jobs (closes a pre-existing claim-hijack). The kill-switch exception
(`ExecutionKillSwitchEngaged`) is now caught and translated to a clean 503 on the
`run_signal` and admin-retry endpoints, and to a labelled clean skip in the H1/M5
schedulers — instead of an unhandled 500. In every case no order is placed (the
model guard fails closed first).

### 1.5 Security Headers (Frontend)

| Header | Value | Purpose |
|--------|-------|---------|
| Strict-Transport-Security | max-age=31536000; includeSubDomains; preload | Force HTTPS |
| X-Frame-Options | DENY | Prevent clickjacking |
| X-Content-Type-Options | nosniff | Prevent MIME sniffing |
| Referrer-Policy | strict-origin-when-cross-origin | Control referrer |
| Permissions-Policy | camera=(), microphone=(), ... | Restrict APIs |
| X-XSS-Protection | 1; mode=block | Legacy XSS protection |
| Content-Security-Policy-Report-Only | (see config) | CSP in report mode |

**Note:** CSP is in Report-Only mode for MVP. Will be enforced post-testing.

---

## 2. How to Verify

### 2.1 Audit Logging

```bash
# Check audit log entries via Django shell
python manage.py shell -c "from core.models import AuditEvent; print(AuditEvent.objects.count())"

# View recent events
python manage.py shell -c "
from core.models import AuditEvent
for e in AuditEvent.objects.all()[:10]:
    print(f'{e.created_at} [{e.severity}] {e.event_type} - {e.entity_type}:{e.entity_id}')
"
```

### 2.2 Rate Limiting

```bash
# Test rate limiting (should get 429 after limit)
for i in {1..110}; do
  curl -s -o /dev/null -w "%{http_code}\n" https://api.guvfx.com/api/strategies/
done | sort | uniq -c
```

### 2.3 CSRF Protection

```bash
# Verify CSRF token required (should fail without token)
curl -X POST https://api.guvfx.com/api/auth/cookie/login/ \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.com","password":"test"}' \
  -w "\nStatus: %{http_code}\n"

# Should return 403 Forbidden (CSRF token missing)
```

### 2.4 Execution Stubs

```bash
# Test execution enable stub (should return 501)
curl -X POST https://api.guvfx.com/api/execution/enable/123/ \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "X-CSRFToken: YOUR_CSRF_TOKEN" \
  -w "\nStatus: %{http_code}\n"

# Expected: 501 Not Implemented
```

### 2.5 Security Headers

```bash
# Check security headers
curl -I https://guvfx.com/ 2>/dev/null | grep -E "(Strict-Transport|X-Frame|X-Content-Type|Referrer-Policy|Permissions-Policy|X-XSS)"
```

---

## 3. What Remains Post-MVP

### 3.1 Execution Pipeline

- [ ] Implement real execution enable/disable logic
- [ ] MT5 terminal management (start, stop, health check)
- [ ] Strategy compilation to EA
- [ ] Per-account terminal isolation
- [x] Kill switch implementation — DB-backed `ExecutionControl`, functional for
  the signal-proposal path (EXEC-E1a). Extend to live execution when that path exists.

### 3.2 Security Enhancements

- [ ] Move CSP from Report-Only to Enforce mode
- [ ] Implement account lockout after N failed attempts
- [ ] Add 2FA support
- [ ] Implement session management UI (view/revoke active sessions)
- [ ] Add IP allowlisting for admin endpoints

### 3.3 Monitoring

- [ ] Set up CSP violation reporting endpoint
- [ ] Configure alerting for WARN/ERROR audit events
- [ ] Implement rate limit monitoring dashboard
- [ ] Add anomaly detection for authentication patterns

---

## 4. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         FRONTEND (Next.js)                      │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Security Headers (HSTS, X-Frame-Options, CSP, etc.)     │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ apiFetch() with automatic CSRF handling                 │   │
│  └─────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                         BACKEND (Django/DRF)                    │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Rate Limiting (100/min per user, 1000/min per IP)       │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ CSRF Protection (double-submit cookie pattern)          │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Cookie JWT Authentication (HttpOnly, Secure, SameSite)  │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Audit Logging (append-only, fail-open)                  │   │
│  └─────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ Execution Stubs (501 Not Implemented)                   │   │
│  └─────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                         DATABASE (PostgreSQL)                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ core_auditevent (append-only audit log)                 │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 5. Contact

For security concerns or questions about this implementation:
- Review audit logs in Django admin or via shell
- Check application logs for AUDIT: prefix entries
- Escalate to platform team for production issues
