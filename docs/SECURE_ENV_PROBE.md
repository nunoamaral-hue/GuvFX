# Secure environment-variable probing (operator guidance)

**Rule: never dump a full container/process environment, and never rely on a deny-list regex to
redact it.** Reading a live environment to check a revision or a flag is routine; doing it with
`docker inspect … | grep | sed` is how a secret escapes.

## The incident this prevents

An operator probe printed two production `*_KEYRING` secret **values** into a tool transcript.
The redaction (`s/(KEY|TOKEN|SECRET|PASSWORD)=.*/.../`) only matched a keyword *immediately*
before `=`, so a compound name — `HOSTED_EXECUTOR_KEYRING=` (KEY + RING + =) — was not redacted,
and the probe dumped the whole environment. Deny-lists miss unforeseen names; full dumps expose
everything.

## Do this instead

Use the allow-list helper, which (1) considers **only** the variable names you pass and (2)
redacts any name that merely *looks* secret, by name, never consulting the value:

```bash
# locally
python3 scripts/safe_container_env.py GUVFX_GIT_COMMIT GUVFX_RELEASE_ID

# inside a running container (read-only)
docker exec <container> python3 scripts/safe_container_env.py GUVFX_GIT_COMMIT GUVFX_RELEASE_ID
```

Safe, non-secret variables worth reading this way: `GUVFX_GIT_COMMIT`, `GUVFX_RELEASE_ID`,
`DB_HOST`, `DB_NAME`, `DB_PORT`, `PYTHON_VERSION`. A name containing `KEY`/`KEYRING`/`SECRET`/
`TOKEN`/`PASSWORD`/`CREDENTIAL`/`SIGNING`/`HMAC`/`PRIVATE`/`CERT`/… is emitted as
`<redacted:secret-named>`.

## Checking a flag without reading secrets

To confirm a feature flag's state, read only that flag by name (it is not secret-named), or check
behaviour (e.g. an endpoint's gated response) rather than the raw environment.

## If a secret value is ever exposed

Stop, do not reproduce/quote/hash the value, report it with redacted detail, and rotate the
affected secret through the governed secret mechanism before relying on it again (see
`.claude/rules/security.md`). Treat the value as compromised until rotated.

Regression coverage: `tests/test_safe_container_env.py` proves a secret-named variable's value can
never be emitted by the helper.
