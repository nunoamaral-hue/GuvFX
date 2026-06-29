# Rule — Handoff

Scope: read when closing a packet or task and producing the PM handoff.

- **Use the PM handoff structure** in `docs/HANDOFF_TEMPLATE.md`. Fill every heading; write
  `None` where a heading is empty.
- **Separate verified fact from assumption.** State clearly what was observed/verified
  versus what was assumed or inferred. Do not present an assumption as a fact.
- **Record deviations.** Any departure from the packet (scope, method, order) is listed
  explicitly, with reason.
- **Exact tests.** Name the exact test/validation commands run and their results, not a
  summary judgement.
- **Commit and branch state.** Record the branch, base commit, and resulting commit(s),
  and whether anything was pushed.
- **One bounded next action.** Recommend a single, well-scoped next step — not an open list.
