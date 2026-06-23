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
