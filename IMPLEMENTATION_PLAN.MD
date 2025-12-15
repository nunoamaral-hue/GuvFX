# GuvFX – Implementation Plan (Aligned with Archie)

## 1. System Overview

- Purpose:
  GuvFX is a trading and strategy platform that lets traders design, run, and monitor algorithmic strategies (including AI-assisted ones) with integrated backtesting and live broker connectivity.

- Primary users:
  - Retail traders (initial focus)
  - Internal staff (support, admin, compliance)
  - Later: prop traders / partners

- Tech stack:
  - Backend: Python 3.12 + Django + Django REST Framework
  - Frontend: Next.js (React, TypeScript)
  - Database: PostgreSQL 16
  - Infra: Docker + docker-compose (dev & prod) on VPS
  - Edge: Traefik or Nginx as reverse proxy
  - Later: Celery worker + Redis for backtests & AI jobs

## 2. Services / Containers

1. guvfx-backend (Django + DRF)
   - Apps: users, trading, strategies, backtests, integrations, analytics, audit.
   - Exposes REST/JSON APIs to frontend and external tools.
   - Handles authentication, RBAC, business rules, and orchestrates AI/backtest tasks.

2. guvfx-frontend (Next.js)
   - Trader UI: bots, strategies, performance, backtests.
   - Admin UI: users, system configuration, audit, risk.
   - Talks only to guvfx-backend over HTTPS.

3. guvfx-db (Postgres)
   - Stores users, strategies, accounts, trades, backtests, audit logs.

4. (Later) guvfx-worker
   - Background processing:
     - Backtest runs
     - AI suggestion jobs
     - Scheduled tasks

## 3. Backend Domain Modules

- users:
  - User model, roles (TRADER, ADMIN, SUPPORT), auth endpoints.
- trading:
  - Linked broker/MT5 accounts, trade records, positions, executions.
- strategies:
  - Strategy definitions, parameters, risk settings.
  - Links to AI suggestion engine.
- backtests:
  - Backtest requests, runs, inputs, results, metrics.
- integrations:
  - Broker APIs, data feed adapters, KYC if needed.
- analytics:
  - Performance metrics, risk statistics, dashboards.
- audit:
  - Centralised audit logging for critical actions.

Each module will have:
- Models (Django ORM)
- Serializers (DRF)
- Services (business logic layer)
- Viewsets / endpoints
- Tests

## 4. Security & Compliance Implementation

- Auth & RBAC:
  - Django auth + groups/permissions
  - Roles: TRADER, ADMIN, SUPPORT
- Input validation:
  - DRF serializers for all externally facing endpoints
- Audit trail:
  - AuditLog model and middleware/hooks
- Secrets:
  - Environment variables for DB, broker credentials, AI keys, etc.
- Edge security:
  - HTTPS termination, security headers, limited open ports on VPS

## 5. Phased Implementation

### Phase 1 – Foundations (current)
- Create repo structure (backend, frontend, infra, docs).
- Write docker-compose.dev.yml to run backend, frontend, Postgres.
- Scaffold Django project + basic settings for Postgres.
- Scaffold Next.js project + basic layout.

### Phase 2 – Auth & Identity
- Implement User model, role system.
- Implement registration/login endpoints.
- Basic profile endpoints and me/whoami.

### Phase 3 – Core Trading & Strategies
- Models for trading accounts, trades, strategies.
- APIs and minimal UI for:
  - Strategy CRUD
  - Linking trading accounts
  - Storing trades/performance

### Phase 4 – Backtesting & AI Integration
- Backtest models and APIs.
- Worker service for backtests.
- Basic AI suggestion endpoint integrating with strategy parameters.

### Phase 5 – Admin & Observability
- Admin UI for staff.
- Audit logs view.
- Logs/metrics dashboards.
- VPS deployment with HTTPS and backups.

## 6. Open Questions / To Confirm with Business

- Exact external broker list and connectivity method (MT5 bridge, REST, etc).
- Preferred AI provider/model and how it will be hosted.
- SLAs and uptime expectations for GuvFX.