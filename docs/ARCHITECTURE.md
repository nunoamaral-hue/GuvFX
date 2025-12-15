# Architecture (high-level)

> Keep this concise. Link to deeper docs/diagrams if needed.

## System overview
- Frontend: Next.js app (UI, auth flows, dashboards)
- Backend: Django + REST API (accounts, transactions, strategies, etc.)
- Data: PostgreSQL
- Async: Celery + Redis (if enabled)
- Observability: (fill in)

## Key flows (bullets)
- Auth: <how login works, JWT/session, etc.>
- Core domain objects:
  - Customer / Profile
  - Account
  - Transactions
  - Strategies / Backtests (if applicable)

## API surface (top endpoints)
- `GET /api/...` — ...
- `POST /api/...` — ...

## Security posture (minimum)
- Auth required by default
- Role-based access (staff/admin/customer)
- Avoid PII in logs
- Rate limiting on auth endpoints (if implemented)

## Diagrams
- C4 Context/Container: (add link or image)
