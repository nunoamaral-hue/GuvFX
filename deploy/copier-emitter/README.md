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
- emitter_task_template.xml        canonical DISABLED task shape (Settings.Enabled=false, no Triggers)
- config.template.json             account-scoped config; no secrets (env-var names only); all gates OFF
- MANIFEST.json                    bundle file hashes + the pinned source subset to vendor into lib/

## Packaging
At bundle-build time lib/ is populated with the pinned source subset listed in MANIFEST.json.lib_subset (the
Django-free emitter modules). The source_sha is stamped to the build commit. The emitter runs on Python 3.11+
stdlib only - no third-party dependency.

## Conventions mirrored
ASCII-only PowerShell, ParseFile-validated before first run (security RULE 9/11); one compact JSON object as the
final line; -Apply dry-run default; reserved account ids (<= 1) refused; never a broad recursive delete; never
edits the golden MT5 template; never installs an Expert Advisor.
