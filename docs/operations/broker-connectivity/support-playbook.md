# Support Playbook — Broker Connectivity (WP5.4 Workstream G)

Operator workflows for each customer-facing situation. **Every workflow is read-first**: inspect
authoritative state, then the operational-event projection, then act only within the permitted actions.

> **Load-bearing rule — operational events are NOT authoritative business state.** The `OperationalEvent`
> read model is a **rebuildable projection (a cache)** of the authoritative sources. Never quote it as the
> source of truth for eligibility, validation, health, or pause. The authoritative sources are:
> - **Validation state:** `TradingAccount.validation_status` (`NEVER` / `VALIDATED` / `CONNECTION_FAILED` /
>   `TECHNICAL_ERROR`) + `validated_at`; append-only `BrokerAccountValidationAttempt` history.
> - **Health:** `BrokerAccountHealth.contract()` (`state`, `eligible`, `pause_required`, `resume_eligible`,
>   `state_version`) — only meaningful when `BROKER_CONNECTIVITY_HEALTH_ENABLED` is ON, else `None`.
> - **Pause:** `execution.BrokerRuntimePause` (`runtime_pause.pause_state` / `is_broker_paused`).
> - **Disconnect / lifecycle:** `TradingAccount.disconnected_at` + `is_active`.
> - **Credential access:** `core.audit.AuditEvent` (`CREDENTIAL_ACCESSED` etc.).

**Customer wording principle.** Use the customer-safe reason mapping, never operator internals. Validation
outcomes map to three customer meanings: **HEALTHY** (connected), **NEEDS_ATTENTION** (credential/account
fault — customer can fix; *not* retryable automatically), **UNAVAILABLE** (platform condition — retryable;
never ask the customer to re-enter a correct password). Never expose host paths, internal reason codes,
`state_version`, job/plan ids, or raw enums.

Each workflow: **See · Authoritative state · Events · Do not infer · Customer wording · Permitted · Prohibited
· Escalation · Sponsor approval.**

---

### 1. User cannot add an account
- **See:** the add form errors, or a 404 on the broker-accounts surface.
- **Authoritative state:** is `BROKER_CONNECTIVITY_ENABLED` / `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED`
  armed at all? (`customer_frontend_only` / `customer_backend_only` in [rollback-matrix.md](rollback-matrix.md)).
  `brokeridentity_present` constraint requires a `broker_server` or non-empty `broker_name`.
- **Events:** CONNECTIVITY category, if any.
- **Do not infer:** that the broker is down — a 404 means the surface is DARK, not a broker fault.
- **Customer wording:** "Account onboarding isn't available for your account yet." (if DARK) / "Please
  select a broker server before continuing." (if identity missing).
- **Permitted:** confirm the flag/arming state; guide the customer to provide broker identity.
- **Prohibited:** enabling any flag; editing the account on the customer's behalf.
- **Escalation:** Engineering if the surface should be armed but 404s (arming mismatch).
- **Sponsor approval:** required to change any flag state.

### 2. Connection test fails
- **See:** the test-connection result shows a failure.
- **Authoritative state:** the latest `BrokerAccountValidationAttempt` (`status`, `reason_code`,
  `retryable`, masked login/server); `validation_status`.
- **Events:** VALIDATION `broker_validation_result` (severity from HEALTHY→INFO / NEEDS_ATTENTION→WARNING /
  UNAVAILABLE→ERROR).
- **Do not infer:** the cause from the projection — read the authoritative attempt's `status`/`retryable`.
- **Customer wording:** for NEEDS_ATTENTION: "We couldn't sign in with those details — please check them."
  for UNAVAILABLE: "The broker service is temporarily unavailable — please try again shortly."
- **Permitted:** ask the customer to retry (UNAVAILABLE) or re-check credentials (NEEDS_ATTENTION).
- **Prohibited:** reading or asking for the plaintext password; retrying on the customer's behalf with their
  credentials.
- **Escalation:** if `isolation_check_failed` / `impl_integrity_mismatch` appears → **platform fault**,
  escalate to Engineering (customer cannot fix it).
- **Sponsor approval:** no (informational support).

### 3. Technical validation unavailable
- **See:** repeated UNAVAILABLE outcomes.
- **Authoritative state:** attempt `reason_code` in the UNAVAILABLE set (`server_unavailable`,
  `login_timeout`, `mt5_unavailable`, `bridge_unavailable`, `runtime_unavailable`, `validation_unconfigured`,
  `validation_busy`, `validation_runner_*`, `validation_baseline_dirty`, ...).
- **Events:** VALIDATION severity ERROR.
- **Do not infer:** invalid credentials — UNAVAILABLE means a **platform** condition, never a credential fault.
- **Customer wording:** "Validation is temporarily unavailable — no action needed on your side; please try
  again shortly."
- **Permitted:** confirm the agent/validation-image state with the operator ([rollback-matrix.md](rollback-matrix.md)
  `agent_unavailable` / `validation_image_failure`); advise retry.
- **Prohibited:** telling the customer to re-enter credentials; disabling isolation checks.
- **Escalation:** Operator (host: agent/image), Engineering if persistent.
- **Sponsor approval:** required to revert the validation image or change flags.

### 4. Invalid credentials
- **See:** NEEDS_ATTENTION with `invalid_password` / `invalid_login` / `server_not_found` /
  `account_disabled`.
- **Authoritative state:** the attempt (`retryable=false`) + `validation_status=CONNECTION_FAILED`.
- **Events:** VALIDATION severity WARNING.
- **Do not infer:** a platform fault — this is a customer-fixable credential/account issue.
- **Customer wording:** "Those sign-in details weren't accepted by the broker — please double-check and
  update them."
- **Permitted:** ask the customer to re-enter/replace credentials (they do it; see workflow 7).
- **Prohibited:** handling the customer's plaintext credentials; auto-retrying (it is not retryable).
- **Escalation:** none unless the customer insists the details are correct (then treat as a broker-side
  classification question, not a platform fault).
- **Sponsor approval:** no.

### 5. Live account detected where demo is required
- **See:** `live_detected` where a demo was expected.
- **Authoritative state:** attempt `is_demo=false`, `status=HEALTHY` (a *connected*, correctly-classified
  session — not a failure). Classification: `trade_mode` 0=DEMO, 1=CONTEST, 2=REAL; `is_demo = trade_mode==0`.
- **Events:** VALIDATION (INFO) with `is_demo` in the allow-listed metadata.
- **Do not infer:** a failure — a live/contest account validates as a healthy connected session. Note:
  `classification_mismatch` exists in the taxonomy but has **no producer in the merged code** — do not
  expect it.
- **Customer wording:** "This looks like a live account. For Trusted Beta, please connect a **demo** account."
- **Permitted:** ask the customer to provide a demo account.
- **Prohibited:** proceeding to any execution arming with a live account during Trusted Beta.
- **Escalation:** Sponsor if a policy exception is requested.
- **Sponsor approval:** required for any live-account exception.

### 6. Account disconnected
- **See:** the account shows disconnected/inactive.
- **Authoritative state:** `TradingAccount.disconnected_at` set + `is_active=false` + `validation_status=NEVER`;
  **the row is retained** (tombstone, never deleted); `row_deleted: false`.
- **Events:** CONNECTIVITY `broker_disconnect` (once per account, first-wins).
- **Do not infer:** data loss — disconnect is a tombstone with verified credential destruction, not a delete.
- **Customer wording:** "This account is disconnected. You can reconnect by adding it again."
- **Permitted:** explain the tombstone; guide re-add.
- **Prohibited:** row-deleting the account; "undeleting" credentials (they were destroyed).
- **Escalation:** Engineering if the disconnect looks unintended.
- **Sponsor approval:** no.

### 7. Credential replaced
- **See:** the customer updated their credentials.
- **Authoritative state:** `replace_credentials` re-encrypts and **invalidates prior eligibility** —
  `validation_status→NEVER`, `validated_at→None`, health reset to `UNKNOWN`; append-only attempt history
  preserved.
- **Events:** CREDENTIAL `broker_credential_replaced` (customer) + `broker_health_credential_invalidated`
  (operator).
- **Do not infer:** that the account is still validated — replacement **resets** validation deliberately.
- **Customer wording:** "Your details were updated. We'll re-validate the connection."
- **Permitted:** confirm re-validation is needed; the customer triggers it.
- **Prohibited:** handling plaintext credentials; skipping re-validation.
- **Escalation:** none.
- **Sponsor approval:** no.

### 8. Broker health degraded
- **See:** an account flagged degraded (only if `BROKER_CONNECTIVITY_HEALTH_ENABLED` is ON).
- **Authoritative state:** `BrokerAccountHealth.contract()` → `state=DEGRADED`, `eligible=false`,
  `pause_required=true`; reached after `failure_threshold` sustained soft failures.
- **Events:** HEALTH `broker_health_*` (deduped on `state_version`).
- **Do not infer:** an execution effect **unless the execution gate is also ON** — health alone only signals.
- **Customer wording:** "We're seeing sign-in issues with this account and are monitoring it."
- **Permitted:** inspect `get_contract`, validation-attempt history; advise the customer to re-check
  credentials if the cause is credential-side.
- **Prohibited:** manually editing health rows; forcing a state.
- **Escalation:** Engineering if degraded without a matching validation cause.
- **Sponsor approval:** no.

### 9. Broker health stale
- **See:** an account marked stale.
- **Authoritative state:** `state=STALE` — was HEALTHY but no recent successful validation
  (`stale_timeout_s`, default 3600s framework value). May escalate to DEGRADED/DISCONNECTED on sustained
  failures.
- **Events:** HEALTH stale transition.
- **Do not infer:** a broker outage — stale means "no fresh evidence," not "failing."
- **Customer wording:** "We haven't re-checked this account recently — a fresh validation will refresh it."
- **Permitted:** invite the customer to run a validation to refresh evidence.
- **Prohibited:** treating stale as disconnected.
- **Escalation:** none.
- **Sponsor approval:** no.

### 10. Runtime paused
- **See:** the account's runtime is paused (only with gate+health armed).
- **Authoritative state:** `execution.BrokerRuntimePause` (`is_broker_paused`), version-keyed; distinct from
  `AccountRuntime.state`. Pause **never** auto-resumes.
- **Events:** RUNTIME `broker_runtime_paused` etc.
- **Do not infer:** that resume is automatic — it is **explicit-caller-only** (workflow 11).
- **Customer wording:** "Trading on this account is paused while we resolve a connection issue."
- **Permitted:** inspect the pause record + reason; explain it will resume once eligible and confirmed.
- **Prohibited:** auto-resuming; editing the pause record.
- **Escalation:** Engineering/Operator for a controlled resume.
- **Sponsor approval:** required to resume (see 11).

### 11. Controlled resume requested
- **See:** a request to resume a paused runtime.
- **Authoritative state:** `request_broker_runtime_resume` is the **sole** path that clears a pause; it
  re-checks the live contract and **fails closed** (refuses if `pause_required`, not `eligible`, or the
  version is stale). It has **no automatic caller** — nothing auto-resumes.
- **Events:** RUNTIME `broker_resume_completed` / `broker_resume_idempotent` / `broker_resume_refused`.
- **Do not infer:** that resume is safe just because the customer asks — it must pass the live contract.
- **Customer wording:** "We'll resume trading once the connection is healthy again."
- **Permitted:** verify eligibility via `get_contract`; escalate for the explicit, authorised resume call.
- **Prohibited:** resuming a still-ineligible account; bypassing the contract re-check.
- **Escalation:** Sponsor decision authority for the resume.
- **Sponsor approval:** **required** (resume is a Red action).

### 12. Execution refused
- **See:** an order was not placed; a refusal appears.
- **Authoritative state:** `EXECUTION_GATE_REFUSED` / `EXECUTION_DISPATCH_REFUSED` `AuditEvent` with a
  customer-safe `SR_*` reason (e.g. `broker_validation_required`, `broker_health_degraded`); the account's
  `validation_status` + health contract. Only meaningful with the gate ON.
- **Events:** EXECUTION `broker_execution_gate_refused` / `broker_execution_dispatch_refused`.
- **Do not infer:** a bug — refusal is the gate **working**: an ineligible account is correctly withheld.
  But a refusal for an **eligible** account is a defect (SEV-2 refusal spike).
- **Customer wording:** "This account isn't validated for trading yet — please complete validation first."
  (never expose reason codes).
- **Permitted:** confirm eligibility; guide the customer to validate. If eligible accounts are refused,
  raise a refusal-spike incident.
- **Prohibited:** re-running the refused job; forcing execution.
- **Escalation:** Engineering for eligible-account refusals; Sponsor to disarm the gate.
- **Sponsor approval:** required to change the gate flag.

### 13. Operational timeline empty
- **See:** the operator UI shows no events for an account.
- **Authoritative state:** is `OPERATIONS_EVENTS_ENABLED` ON? (OFF → API 404 / empty). Is the frontend flag
  armed? The **authoritative** account state (validation/health/pause) is unaffected by an empty timeline.
- **Events:** none (that's the point).
- **Do not infer:** that nothing happened — an empty projection may mean DARK, a rebuildable cache not yet
  populated, or a recorder failure. Check `guvfx.operational_events` logs and authoritative rows.
- **Customer wording:** n/a (operator-only surface).
- **Permitted:** confirm flag state; check recorder logs; rebuild the projection if needed.
- **Prohibited:** telling a customer their account "has no history" based on an empty projection.
- **Escalation:** Engineering if recorder failures are present.
- **Sponsor approval:** no.

### 14. Event visible to operator but not customer
- **See:** an event the operator sees is not in the customer's own view.
- **Authoritative state:** by design — non-staff receive `customer_visible=true` events only; staff receive
  all. Owner-scoping (IDOR-safe) means a customer only ever sees their **own** account's customer-visible
  events.
- **Events:** operator-only events carry `customer_visible=false`.
- **Do not infer:** a leak — this is the intended visibility split. A customer seeing **another owner's**
  event would be the leak (SEV-1).
- **Customer wording:** n/a.
- **Permitted:** confirm the visibility flag on the event; explain the split to internal staff.
- **Prohibited:** exposing operator-only events to a customer.
- **Escalation:** SEV-1 only if cross-owner visibility is observed.
- **Sponsor approval:** no.

### 15. Duplicate or missing operational event
- **See:** an event appears twice, or an expected event is missing.
- **Authoritative state:** dedup is enforced by the partial-unique `dedup_key` + `get_or_create`; a genuine
  duplicate should be impossible for keyed events. A "missing" event does **not** mean the authoritative
  action didn't happen — check the authoritative model/audit.
- **Events:** inspect `dedup_key` and `correlation_id`.
- **Do not infer:** business impact — the projection is a cache; the authoritative action is recorded in the
  WP1A/WP3/WP2 models + `AuditEvent` regardless.
- **Customer wording:** n/a.
- **Permitted:** rebuild the projection from authoritative sources; check recorder-failure logs.
- **Prohibited:** treating a projection gap as data loss; editing events by hand.
- **Escalation:** Engineering if recorder failures correlate.
- **Sponsor approval:** no.

### 16. User asks to delete broker account
- **See:** a deletion request.
- **Authoritative state:** deletion policy = **soft-disconnect + credential destruction + tombstone, never
  row-delete** (ADR-0026). `disconnect_account` sets `is_active=false`, `disconnected_at`,
  `validation_status=NEVER`, destroys the credential (verified), `row_deleted:false`.
- **Events:** CONNECTIVITY `broker_disconnect`; CREDENTIAL destruction audit.
- **Do not infer:** that a hard delete is available or appropriate — it is not.
- **Customer wording:** "We'll disconnect the account and securely remove its stored credentials."
- **Permitted:** guide the customer to disconnect (the sanctioned path).
- **Prohibited:** row-deleting the account or its history; emptying any audit trail.
- **Escalation:** Sponsor if a hard-delete/GDPR erasure is demanded (policy decision, out of this playbook).
- **Sponsor approval:** required for any deviation from the tombstone policy.

### 17. User requests credential removal
- **See:** a request to remove stored credentials without full account deletion.
- **Authoritative state:** credential destruction is part of `disconnect_account` (P3-D
  `destroy_customer_credential`, verified secure clear + audit); credentials are stored Fernet-encrypted and
  decrypted only at point of use with a `CREDENTIAL_ACCESSED` audit.
- **Events:** CREDENTIAL destruction audit.
- **Do not infer:** that credentials can be "read back" to confirm — they are never exposed; destruction is
  verified, not displayed.
- **Customer wording:** "We'll securely remove the stored credentials for this account."
- **Permitted:** use the sanctioned disconnect/destruction path.
- **Prohibited:** printing/echoing any credential; a non-verified clear.
- **Escalation:** Engineering if destruction verification fails.
- **Sponsor approval:** no (uses the sanctioned path); Sponsor for any non-standard request.

---

## Escalation ladder (summary)

| Trigger | First responder | Escalate to | Sponsor? |
|---------|-----------------|-------------|----------|
| Platform fault (`isolation_check_failed`, `impl_integrity_mismatch`, agent/image down) | Operator | Engineering | to change flags/image |
| Eligible-account execution refused (refusal spike) | Operator | Engineering | to disarm the gate |
| Cross-owner visibility / secret exposure | Operator | **SEV-1** ([incident-response.md](incident-response.md)) | to re-arm |
| Controlled resume | Operator/Engineering | Sponsor | **yes** |
| Live account exception / hard-delete request | Operator | Sponsor | **yes** |
