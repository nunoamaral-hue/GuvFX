# Validation Agent — Operational Runbooks

**Scope:** the beta validation agent (`GuvFXBetaAgent`, the signed `POST /provision` listener on `:8791`).
Every runbook **begins with Evidence** (gather before acting) and **ends with Escalation**. Machine-checkable
index: [runbook-index.json](runbook-index.json). Design context:
[VALIDATION_AGENT_PRODUCTION_HARDENING.md](../../VALIDATION_AGENT_PRODUCTION_HARDENING.md). Section contract
per runbook: **Evidence → Diagnosis → Permitted → Prohibited → Verify → Escalation.**

> **Standing rules.** Never start the agent from an interactive SSH session (security RULE 1 — it dies with
> the session); use only its supported service mechanism. Never restart on a *downstream* (broker/MT5/IPC)
> failure alone — a restart cannot fix a broker/host condition and destroys in-flight evidence. Read-only
> evidence first, always. #12 and #1 are never touched by these procedures.

---

### Agent unavailable (agent-unavailable)
#### Evidence
Gather (read-only) BEFORE acting: the `GuvFXBetaAgent` service state; whether `:8791` is LISTENING; the last
`agent_readiness` probe result; the latest `BrokerAccountValidationAttempt` rows (are customers seeing
`validation_agent_unreachable`/`validation_agent_timeout`?); the last agent/WinSW log write time and the last
diagnostic-artefact time (when did it last serve?).
#### Diagnosis
Distinguish: (a) service Stopped / no listener → **UNAVAILABLE** by absence; (b) listener up but NEGOTIATE
failing → see *NEGOTIATE failed*; (c) a ~10s connect timeout from the backend → firewall/port vs process-down.
#### Permitted
Confirm the config is correct (`BETA_AGENT_BASE_URL` ↔ bind); start the agent via its **supported service
mechanism** only; confirm readiness (NEGOTIATE) after start.
#### Prohibited
Interactive-SSH launch; changing `BETA_AGENT_BASE_URL`/ports unless evidence proves the config wrong; touching
`:8788` (the trade bridge) or any customer runtime.
#### Verify
`agent_up == 1`, socket listening, one successful signed NEGOTIATE, and a subsequent real validation returns a
structured result (not a transport failure).
#### Escalation
If it re-fails within the confirm window → *Restart procedure* (crash-loop path) and escalate to Engineering.

---

### Agent unhealthy — degraded (agent-unhealthy)
#### Evidence
Read the current health state and the dominant failing `reason_code` over the window; confirm NEGOTIATE still
succeeds (agent is UP). Pull the affected correlation ids into the support timeline.
#### Diagnosis
The agent is up but validations fail at an elevated rate. Identify the layer from the reason: host IPC / MT5 /
broker / transport. Route to the matching runbook.
#### Permitted
Triage per the layer-specific runbook; advise customers the service is temporarily unavailable (no credential
re-entry); watch the failure-rate trend.
#### Prohibited
Restarting the agent on downstream failures alone; telling customers their details are wrong.
#### Verify
Failure rate returns below the recovery threshold over the window → state returns to HEALTHY.
#### Escalation
Persistent DEGRADED beyond the SLA window → Engineering (host/broker), Sponsor if a policy call is needed.

---

### Agent wedged — up but not completing validations (agent-wedged)
#### Evidence
Confirm NEGOTIATE succeeds (agent answers) BUT `oldest_inflight_validation_seconds` exceeds the expected max
(~`login_timeout_ms`+grace) and/or `validation_busy_rate` is high; pull the stuck correlation id and its
diagnostic artefact. NEGOTIATE is a shallow probe — a green handshake does NOT prove a validation can complete.
#### Diagnosis
A validation holds the single-flight lock and never completes (host/MT5 hang), so the agent reports HEALTHY
while no new validation can run — a *silent* degradation the shallow probe misses.
#### Permitted
Confirm the host/runner/MT5 state with the operator; if the in-flight probe/runner is genuinely hung, a
controlled restart is warranted (this IS a readiness-affecting condition, unlike an ordinary downstream
failure) — capture evidence first, then restart via the supported service.
#### Prohibited
Killing arbitrary processes; restarting on a single slow-but-completing validation (that is latency-creep, not
a wedge); interactive-SSH launch.
#### Verify
`oldest_inflight_validation_seconds` returns to normal; a fresh validation completes with a structured result.
#### Escalation
Repeated wedges → Engineering/Operator (host/runner); reference the IPC reliability investigation.

---

### NEGOTIATE failed (negotiate-failed)
#### Evidence
Confirm `:8791` is listening but the signed NEGOTIATE returns denied/timeout; capture the reason
(`impl_integrity_mismatch`, contract/version mismatch, keyring) and the manifest/version.
#### Diagnosis
Listener up, handshake failing → an integrity/version/keyring problem (not a broker/customer fault).
#### Permitted
Verify the deployed agent manifest/version against the approved bundle; verify the signing keyring is
provisioned; gather logs.
#### Prohibited
Bypassing the integrity/version gate; re-signing or editing the manifest as a "fix"; live validation attempts.
#### Verify
A signed NEGOTIATE returns a compatible contract and `VALIDATE_LOGIN` is advertised.
#### Escalation
Integrity mismatch → Engineering immediately (possible tamper/stale bundle); do not proceed to validations.

---

### VALIDATE_LOGIN failed (validate-login-failed)
#### Evidence
For the correlation id, open the support timeline; read the furthest stage + reason; confirm keyring presence
and that `VALIDATE_LOGIN` is advertised in NEGOTIATE.
#### Diagnosis
Separate a *service-side* unavailability (`validation_unconfigured` — keyring/config) from a *downstream*
failure (IPC/MT5/broker) using the timeline stage.
#### Permitted
If `validation_unconfigured`: confirm arming/keyring state with the operator. If downstream: route to the
layer runbook.
#### Prohibited
Presenting a service/config failure as a customer credential error; retry storms.
#### Verify
A subsequent validation reaches at least `mt5_launched`/`broker_login` (or succeeds).
#### Escalation
`validation_unconfigured` that should be armed → Engineering (arming/keyring); otherwise per layer runbook.

---

### Repeated IPC failures (repeated-ipc-failures)
#### Evidence
Count `validation_ipc_unavailable` over the window; pull the diagnostic artefacts (`broker_tcp_observed`,
`first_failing_stage`, `mt5_package_version`) for the affected correlation ids; note Session-0 vs interactive.
#### Diagnosis
Local MT5 Python↔terminal IPC not coming up (Session-0 GUI/IPC readiness) — a host condition, broker never
contacted. See the IPC reliability investigation.
#### Permitted
Confirm the validation host/session state with the operator; advise retry-later to customers (no credential
implication).
#### Prohibited
Blaming the broker or the customer's credentials; changing the validation host without a controlled test.
#### Verify
IPC failure rate falls below threshold; a validation reaches `broker_login`.
#### Escalation
Sustained IPC failure → Engineering/Operator (host session model); reference the controlled-reliability plan.

---

### Repeated broker failures (repeated-broker-failures)
#### Evidence
Count broker-reached reasons (`server_unavailable`/`invalid_*`/`account_disabled`) over the window; confirm
independently whether the broker is reachable (do NOT infer from validation alone).
#### Diagnosis
Broker WAS reached and rejected/was unavailable — distinct from host/agent failures.
#### Permitted
For credential rejections: advise re-check/replace. For `server_unavailable`: advise retry; confirm broker
status out-of-band.
#### Prohibited
Restarting the agent (does not affect the broker); auto-retrying customer credentials.
#### Verify
Broker failure rate falls; healthy validations resume.
#### Escalation
Broker-wide outage → inform Sponsor; not an agent defect.

---

### Repeated MT5 failures (repeated-mt5-failures)
#### Evidence
Read the diagnostic artefacts for `mt5_unavailable`/`validation_ipc_unavailable`/`could_not_verify`; check
`mt5_package_version`, terminal build, and cleanup/baseline-restore results.
#### Diagnosis
MT5 terminal/package not initialising or the baseline dirty — a host/runner condition.
#### Permitted
Confirm the golden/baseline + runner task state with the operator; advise retry-later.
#### Prohibited
Promoting a production terminal to golden; editing the baseline as a "fix"; live attempts.
#### Verify
MT5 initialises (a validation reaches `broker_login`), baseline restores clean.
#### Escalation
Persistent MT5 init failure → Operator/Engineering (host image/runner).

---

### Restart procedure (restart-procedure)
#### Evidence
BEFORE restart, capture the exit evidence: last log/artefact time, service state, any crash/WER event, the
last-served correlation id. (This is the evidence that is otherwise lost on restart.)
#### Diagnosis
Decide restart is warranted (readiness genuinely UNAVAILABLE), not a downstream DEGRADED.
#### Permitted
Start via the supported service mechanism; confirm readiness (NEGOTIATE) and stability for the confirm window.
#### Prohibited
Interactive-SSH launch; looping restarts (crash-loop) without escalation; restarting to "clear" a downstream
failure.
#### Verify
HEALTHY and stable across the confirm window; a real validation returns a structured result.
#### Escalation
Two failed restarts within the confirm window → crash-loop; STOP restarting and escalate to Engineering with
the captured evidence.
#### Rollback
If the failure began after a version change, follow *Rollback procedure*.

---

### Rollback procedure (rollback-procedure)
#### Evidence
Identify the last-known-good agent bundle/version and the change that preceded the failure; capture current
manifest/version.
#### Diagnosis
Confirm the regression correlates with a version/bundle change (not a host/broker condition).
#### Permitted
Re-stage the last-known-good, integrity-pinned bundle via the supported install path; re-run NEGOTIATE.
#### Prohibited
Rolling back config/secrets as a side effect; skipping the integrity/parse gate; production data changes.
#### Verify
NEGOTIATE compatible on the rolled-back version; a validation returns a structured result.
#### Escalation
If rollback does not restore health → Engineering (the failure is not the version).

---

### Escalation criteria (escalation-criteria)
#### Evidence
Summarise the state, the dominant reason, the failure/restart counters, and what evidence is missing.
#### Diagnosis
Map severity: agent UNAVAILABLE with customer impact = HIGH; crash-loop = HIGH; integrity mismatch = HIGH;
sustained DEGRADED = MEDIUM.
#### Permitted
Escalate per the ladder: Operator (host/agent), Engineering (code/integrity/regression), Sponsor (policy,
flag/arming, broker outage).
#### Prohibited
Silent retries; production changes without the sanctioned gate; acting on assumptions.
#### Verify
The receiving owner has the evidence bundle and the correlation ids.
#### Escalation
This IS the escalation runbook — Sponsor is the final authority for Red actions (arming, flags, live paths).
