# GuvFX Copy Trading host emitter bundle (ADR-0077) - repository artifact, DARK

This is the repository-side account-scoped Windows emitter bundle. It is NOT installed by the build packet
(GFX-COPIER-HOST-EMITTER-BUILD-006) and performs no host contact. Installation, credential provisioning,
attestation on a live host, and any enablement are SEPARATE Sponsor gates.

## What it does (and does not)
- Observes a master account's open-position state through the host's existing READ-ONLY bridge
  (/mt5/positions, /mt5/snapshots/account) and emits SIGNED lifecycle observations to the backend ingestion
  contract. It never places, modifies, recovers or closes a broker order - the adapter has no such method.
- The one-shot 'attest' mode proves build/host/account/broker/terminal/bridge identity WITHOUT reading
  positions.
- Every gate (attestation_allowed, observation_allowed, delivery_allowed) defaults OFF; a malformed config
  evaluates OFF; the service cannot start without a complete immutable identity.

## Files
- run_emitter.py                   launcher -> trade_copier.emitter.app.main (adds lib/ to sys.path)
- Install-GuvfxCopierEmitter.ps1   register the account-scoped task DISABLED (no trigger/watchdog/auto-restart); dry-run default
- Uninstall-GuvfxCopierEmitter.ps1 idempotent, account-scoped, reversible removal (task only)
- Status-GuvfxCopierEmitter.ps1    read-only task state probe
- Attest-GuvfxCopierEmitter.ps1    one-shot identity attestation (no positions, no loop)
- run_oneshot_attest.py            NON-INTERACTIVE one-shot attest+POST entrypoint (ADR-0104); host-local ephemeral key, SECRET_UNAVAILABLE fail-closed, one POST, no retry
- Install-GuvfxCopierEmitterOneShotTask.ps1  governed installer for the DISABLED/triggerless account-46 one-shot ATTEST task (ADR-0104); PLAN default, manifest+ParseFile gated, renders the reviewed template, registers DISABLED, authoritative read-back, idempotent, rollback on drift; never enables/runs, never handles the key
- emitter_task_template.xml        canonical DISABLED task shape (Settings.Enabled=false, no Triggers)
- oneshot_attest_task_template.xml canonical DISABLED one-shot ATTEST task (ADR-0104); enabled for one invocation + re-disabled by the host primitive
- config.template.json             account-scoped config; no secrets (env-var names only); all gates OFF
- MANIFEST.json                    bundle file hashes + the pinned source subset to vendor into lib/

## Independent one-shot attestation path (ADR-0104) - DARK
`run_oneshot_attest.py` + `oneshot_attest_task_template.xml` let an authorized operator run account 46's emitter
identity attestation WITHOUT an interactive PowerShell desktop (the #46 web workspace is MT5-only). The signed
host-executor op `TRIGGER_EMITTER_ONESHOT_ATTEST` (backend command `request_copier_emitter_oneshot_attest`)
enables the pre-installed DISABLED one-shot task for EXACTLY ONE invocation, triggers it, and ALWAYS re-disables
it (`Trigger-GuvfxCopierEmitterOneShot.ps1`). The task entrypoint reads the ingest secret ONLY from a host-local
EPHEMERAL key file (shredded after one use); absent => SECRET_UNAVAILABLE and nothing is attested or posted. It
builds the attestation (identity only), POSTs it ONCE (no retry) to the backend ATTEST verifier (never the
event-ingest route), and writes a SANITISED result (no envelope/secret). It is attestation-only: it arms no
observation/replication/execution/event-ingestion, contacts no follower account, and places no order. Everything
is default-OFF and repository-only: nothing here installs, enables, triggers, or contacts a host.

### Installing the DISABLED one-shot task (ADR-0104)
`Install-GuvfxCopierEmitterOneShotTask.ps1` is the governed installer a future (separately Sponsor-gated) host step
runs to register the DISABLED, triggerless, account-46-pinned task `GuvFX_CopierEmitterOneShot_46`. It installs
NOTHING by itself (PLAN is the default; `-Apply` registers). It is pinned to account 46 (refusing account 48,
Customer Zero and every reserved identity), accepts no arbitrary task name / executable / script path / endpoint /
account / operation / command (every value is derived from account 46 + the reviewed staged package), validates
the required runtime files against `MANIFEST.json` and ParseFile-checks the bundle `.ps1` before touching the
scheduler, renders only the reviewed template placeholders (XML-escaped), registers the task DISABLED, and then
**authoritatively reads it back** - failing closed unless the task is exactly named, disabled, triggerless, carries
the fixed action, is pinned to `guvfx_u_46`, and contains no secret. It is idempotent (a byte-identical compliant
task is a no-change; drift needs an explicit `-Replace`, which first exports a rollback of the previous
definition), and a failed replacement restores that export left disabled. It NEVER enables or runs the task, NEVER
provisions or reads the real HMAC key, NEVER opens a verifier window or contacts the backend/broker, and NEVER
alters the run-mode emitter task. Host staging, the ephemeral secret, the verifier window, the feature flag and
triggering all remain separate gates.

## Packaging
At bundle-build time lib/ is populated with the pinned source subset listed in MANIFEST.json.lib_subset (the
Django-free emitter modules). The source_sha is stamped to the build commit. The emitter runs on Python 3.11+
stdlib only - no third-party dependency.

## Conventions mirrored
ASCII-only PowerShell, ParseFile-validated before first run (security RULE 9/11); one compact JSON object as the
final line; -Apply dry-run default; reserved account ids (<= 1) refused; never a broad recursive delete; never
edits the golden MT5 template; never installs an Expert Advisor.
