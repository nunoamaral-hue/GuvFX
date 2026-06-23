# Rule — Evidence

Scope: read when producing evidence for a packet, handoff, or acceptance check.

- **Machine-readable.** Evidence is captured in a structured, parseable form (the evidence
  manifest schema under `evidence/schema/`), not only as prose.
- **Exact commands and results.** Record the exact commands run and their actual results.
  Do not paraphrase a "should pass" — capture what happened.
- **State limitations.** Every evidence record lists what was *not* covered, skipped, or
  assumed. Silence is not coverage.
- **Checksums where relevant.** Include checksums for key files or artefacts whose
  integrity matters to the claim.
- **PASS only when criteria actually ran.** Mark `PASS` only when the applicable acceptance
  criteria were executed and met. If something could not run, use `PARTIAL`/`FAIL` and say
  why. Never assert a green result that was not produced.
