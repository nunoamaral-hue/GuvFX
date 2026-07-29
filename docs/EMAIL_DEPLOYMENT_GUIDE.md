# Email Delivery — Deployment Guide (Google Workspace SMTP)

Genuine customer email delivery for Customer Zero verification. All transport config is
**env-driven**; the SMTP secret is never committed to Git.

## 1. What changed in code
- `guvfx_backend/settings.py` — env-driven `EMAIL_*` block (Google Workspace SMTP defaults).
- `onboarding/emails.py` — `send_verification_email()` (correct From / Reply-To / code + link).
- `onboarding/views.py` — `EmailSendVerificationView` now **actually sends** and returns a
  truthful `502` on transport failure (previously a stub that claimed success and sent nothing).

## 2. Required environment variables (backend)
| Variable | Value | Secret? |
|---|---|---|
| `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` | no (default) |
| `EMAIL_HOST` | `smtp.gmail.com` | no (default) |
| `EMAIL_PORT` | `587` | no (default) |
| `EMAIL_USE_TLS` | `True` | no (default) |
| `EMAIL_HOST_USER` | `support@guvfx.com` | no |
| `EMAIL_HOST_PASSWORD` | *Google Workspace App Password for support@guvfx.com* | **YES — deploy env/secret only** |
| `EMAIL_TIMEOUT` | `20` | no (default) |
| `DEFAULT_FROM_EMAIL` | `GuvFX Support <support@guvfx.com>` | no |
| `SERVER_EMAIL` | `admin@guvfx.com` | no |
| `EMAIL_REPLY_TO` | `support@guvfx.com` | no |
| `FRONTEND_BASE_URL` | `https://guvfx.com` | no |

With `EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD` unset the send path **fails closed** (customer sees a
clear "couldn't send" error) — it never silently drops mail.

## 3. Secret required (operator action — I cannot handle it)
The **Google Workspace App Password** for `support@guvfx.com`:
1. On `support@guvfx.com`, enable **2-Step Verification** (Google Account → Security).
2. Create an **App Password** (Security → App passwords → "Mail" / custom name "GuvFX backend").
   Google shows a 16-character password **once**.
3. Put it in the deploy secret only:
   ```
   # /home/ubuntu/guvfx-prod/email.env  (chmod 600; NOT in Git)
   EMAIL_HOST_PASSWORD=<the 16-char app password>
   ```
   (Alternative: Google Workspace SMTP relay `smtp-relay.gmail.com` with IP allow-listing — heavier
   setup; app password is simplest for a single sender.)

## 4. Deploy
The non-secret vars live in `/home/ubuntu/guvfx-prod/email.env`, referenced by the backend service's
`env_file`. After setting the secret:
```bash
cd /home/ubuntu/guvfx-prod
docker compose up -d --no-deps guvfx-backend        # picks up email.env
```
Rebuild the image only if code changed (`docker build -t guvfx-prod-guvfx-backend:latest ./backend`).

## 5. Verification steps
1. **Egress** — confirm the container can reach SMTP:
   `docker exec guvfx-backend python -c "import socket; socket.create_connection(('smtp.gmail.com',587),10); print('reachable')"`
2. **Auth + TLS + delivery** — send a real test message:
   `docker exec guvfx-backend python manage.py shell -c "from django.core.mail import send_mail; from django.conf import settings; send_mail('GuvFX test','delivery check',settings.DEFAULT_FROM_EMAIL,['<your-inbox>'])"`
   → expect `1`, and the message arrives **From** `GuvFX Support <support@guvfx.com>`, **Reply-To** `support@guvfx.com`.
3. **Full flow** — as Customer Zero, POST `/api/onboarding/email/send-verification/` → receive the code
   → paste on the Email Verification step → `email_verified=True`.
4. **No localhost** — `docker exec guvfx-backend python manage.py shell -c "from django.conf import settings; print(settings.EMAIL_HOST, settings.EMAIL_PORT, settings.DEFAULT_FROM_EMAIL)"` shows `smtp.gmail.com 587 GuvFX Support <support@guvfx.com>` (no `localhost`, no `webmaster@localhost`).

## 6. Rollback
- **Config rollback:** blank `EMAIL_HOST_PASSWORD` (or `EMAIL_HOST=localhost`) in `email.env` and
  `docker compose up -d --no-deps guvfx-backend`. Send then fails closed; no customer data affected.
- **Code rollback:** re-tag the previous backend image to `:latest` and recreate (the send-wiring is
  additive; reverting restores the prior stub behaviour). No migration involved — nothing to reverse.

## 7. Known follow-ups (out of this packet)
- **Password reset** is not implemented (no reset flow exists) — needed before public launch.
- **Account-notification emails** are not implemented (no `send_mail` elsewhere).
- The verification **code is a long token** (48-byte urlsafe); a short numeric OTP would be friendlier
  but is a security/UX change for a later packet.
