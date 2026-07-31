# SOP-001 — Golden Creation SOP — ENGINEERING VALIDATION PACKET (Claude, read-only)

Runs **immediately after** the operator's completion report. **Strictly read-only** — `Test-Path` / `Get-Item`
/ `Get-ChildItem` / `Get-FileHash` / `Get-AuthenticodeSignature` / content scan. **No** marker creation, digest,
pinning, promotion, or host mutation. Assesses **two** things: the installation **and** the SOP itself.

## Part 1 — Installation validation (read-only)
Staging path = `C:\GuvFX\golden\staging` (or the name the operator reported).
1. **Install present + complete** — `terminal64.exe`, `metatester64.exe`, `MetaEditor64.exe` present; file
   count; no partial/`.tmp` residue.
2. **Recorded build** — `terminal64.exe` FileVersion; **all three exes report the SAME build** (guards against a
   mixed tree like the drifted golden).
3. **RULE-10 prerequisites (read-only):** dirty files **absent** (`config\accounts.dat`, `config\servers.dat`,
   `config\common.ini`, `config\terminal.ini`, `origin.txt`, `MQL5\experts.dat`); `bases\` = only `Default`;
   `MQL5\` absent at root (non-portable); **content provenance scan = 0 foreign paths** (`C:\GuvFX\terminals`,
   `C:\GuvFX\accounts`, `C:\Users\`). *(Markers are NOT created here — first gated write is the next step.)*
4. **Completely unused** — no runtime files; mtimes ≈ install time; corroborated by the operator's launch/login
   answers.
5. **Authenticode** — three exes Valid, `CN=MetaQuotes Ltd.` (genuine, untampered).

**Returned (the 5 install questions):** installation successful? · build installed? · all RULE-10 prerequisites
met? · completely unused? · can Golden validation proceed (Yes/No)?

## Part 2 — SOP self-assessment (SOP-001 is itself a deliverable)
Assessed from the actual execution evidence + the operator's completion report:
1. Were any **undocumented decisions** required?
2. Were any **assumptions** made (by the operator or the SOP)?
3. Were any **steps ambiguous**?
4. Did any **prerequisite turn out to be missing**?
5. Could **another engineer execute this SOP successfully without Sponsor guidance**?

## Discipline on failure (evidence-first) — applies to BOTH parts
If any Part-1 answer is **No** → **STOP. Do not repair. Do not improvise. Do not mutate.** Return the observed
evidence only; a contaminated/mixed-build staging tree is discarded (operator deletes + re-runs), never patched.

If any Part-2 answer is **No** (the SOP is defective) → **do not modify the host. Update SOP-001. Then repeat the
procedure from the beginning** with the corrected SOP. SOP-001 must reach production quality before Trusted Beta.

## What this step will NOT do
Create markers · compute/record the digest · set `BETA_AGENT_GOLDEN_DIGEST`/`..._MANIFEST_VERSION` · run
`-ValidateGoldenOnly`/`-VerifyOnly` · touch the old golden (pid 5912) · remove the Strategy-Tester firewall
rule · promote anything. Those are later, separately-authorised steps.
