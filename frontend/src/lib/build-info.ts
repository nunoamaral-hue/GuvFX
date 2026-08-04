/**
 * IPR Area G — frontend build provenance.
 *
 * `NEXT_PUBLIC_*` values are inlined by Next.js at BUILD time, so "which commit + which flags are armed
 * in THIS build" is answerable only by baking them in. The commit + timestamp come from build-args
 * threaded through `NEXT_PUBLIC_GIT_COMMIT` / `NEXT_PUBLIC_BUILD_TIMESTAMP` (see frontend/Dockerfile) —
 * `.git` is dockerignored, so runtime self-discovery is impossible. Everything here is NON-SECRET
 * (commit sha / timestamp / flag booleans — names + booleans only). Defaults to "unknown" when the
 * build-args are absent (local dev), degrading gracefully.
 *
 * This is the frontend half of the deploy-parity oracle: compare `gitCommit` against the intended
 * release SHA. (The shared backend image is fingerprinted separately by GET /api/version/.)
 */
import { brokerConnectivityEnabled, operationsEnabled } from "./flags";

export type BuildInfo = {
  gitCommit: string;
  buildTimestamp: string;
  flags: Record<string, boolean>;
};

export function buildInfo(): BuildInfo {
  return {
    gitCommit: process.env.NEXT_PUBLIC_GIT_COMMIT ?? "unknown",
    buildTimestamp: process.env.NEXT_PUBLIC_BUILD_TIMESTAMP ?? "unknown",
    // The two build-time frontend arming flags, resolved for THIS build (names + booleans only).
    flags: {
      NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED: brokerConnectivityEnabled(),
      NEXT_PUBLIC_OPERATIONS_ENABLED: operationsEnabled(),
    },
  };
}
