# Rule — Security

Scope: read when touching credentials, auth, broker/MT5 connectivity, deployment, logging,
or anything that could expose secrets or widen access.

- **No secrets in Git, Notion, prompts, or logs.** API keys, tokens, passwords, broker or
  MT5 credentials, and private keys never appear in tracked files, issue text, chat
  prompts, or log output. Use environment variables / secret stores.
- **Least privilege.** Components and identities get only the access they need. Per-account
  runtime isolation is preserved; do not broaden a scope as a convenience.
- **Separate research, paper, and production permissions.** Credentials and access for
  research, paper trading, and live/production are distinct and not interchangeable.
- **No public admin exposure.** Admin interfaces, internal agents, and management ports are
  never exposed to the public internet.
- **Redact in evidence.** When recording evidence or handoffs, redact file/path/category of
  any sensitive finding rather than reproducing the secret.
- **Stop and report on suspected exposure.** If a secret may have leaked, stop, do not
  commit, and report with redacted detail so it can be rotated.

## Permanent operational rules (adopted 2026-07-22, post bridge-token rotation)

Standing rules, not advice. They were paid for by a real incident — see
`docs/POST_INCIDENT_REVIEW_BRIDGE_TOKEN.md`.

- **RULE 1 — Never start a long-running production service from an interactive SSH session.** Use only its
  supported service/task mechanism (scheduled task, systemd unit, compose service). A process launched via
  `Start-Process`/`nohup` over SSH is session-bound and dies when the session closes — this silently took
  the live bridge down mid-rotation.
- **RULE 2 — Credential rotation is the preferred method for discovering hidden coupling.** Unexpected
  failures surfaced by a rotation are **architectural findings, not operational failures**. Record them as
  such; do not paper over them to make a window look clean.
- **RULE 3 — No production service may silently substitute another service's credential.** A service
  requires its own secret. Several environment NAMES for the SAME secret (aliases) are permitted and must
  agree; falling back to a DIFFERENT secret is forbidden. Missing credential ⇒ startup failure with a clear
  diagnostic, never silent substitution. Canonical helper: `backend/core/credentials.resolve_secret`.
- **RULE 4 — All secret rotations begin from the canonical inventory** (`docs/SECRET_INVENTORY.md`).
  Enumerate every consumer from it before touching anything; update it afterwards.

Corollaries proven in the same incident: authentication must **fail closed** (an unset credential denies
every request and refuses startup); credential comparison must be constant-time and total (compare bytes, so
a malformed credential yields `401`, not `500`); and auth rejections must be logged, or "no auth errors" is
not a provable claim.
