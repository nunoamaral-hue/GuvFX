# Rule — Research

Scope: read when designing, running, or evaluating strategies, backtests, or any
predictive/quantitative work.

- **State a hypothesis first.** Each research effort begins with an explicit, falsifiable
  hypothesis — not a search for any pattern that happens to be profitable.
- **Establish a simple baseline.** Compare against a trivial baseline (e.g. buy-and-hold,
  random, or naive rule) before claiming an effect.
- **Reference exactly.** Record the precise dataset, code version/commit, config, and the
  computational/data cost used to produce a result, so it can be reproduced.
- **Validate chronologically.** Use out-of-sample, forward-in-time validation. No tuning
  on the test window; no shuffling that breaks time order.
- **Retain failed results.** Negative and failed experiments are kept, not discarded.
  They are evidence and prevent re-running dead ends.
- **A profitable backtest is not promotion.** No strategy advances toward paper or live
  on backtest performance alone. Promotion requires the governance decision path.
