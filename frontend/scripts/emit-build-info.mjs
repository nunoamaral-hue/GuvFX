// IPR Area G — emit frontend build provenance as a static, self-surfacing artefact.
//
// Runs in `prebuild` (before `next build`), so it is baked into the image and served at
// /build-info.json — no runtime .git (dockerignored), no route/component, no tree-shaking. Everything
// written is NON-SECRET: commit sha + build timestamp (from build-args threaded as env) and the
// build-time boolean of each frontend arming flag (names + booleans only). Bulletproof: any failure
// logs a warning and the script never exits non-zero (provenance must never break the build).
//
// This is a node build script, not bundled code, so reading the build-time flag env vars here does
// NOT inline anything into the client bundle. Each env var is read by explicit name (never opaquely)
// so the frontend parity guard can validate it against parity/env-allowlist.json.
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const OUT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "public", "build-info.json");

// The two build-time frontend arming flags (ADR-0031). Names only — resolved to booleans below.
const FLAG_NAMES = ["NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED", "NEXT_PUBLIC_OPERATIONS_ENABLED"];

function truthy(v) {
  return ["1", "true", "yes", "on"].includes((v ?? "").toString().trim().toLowerCase());
}

/** Pure: compute the build-info object from an env map + the flag names to record. */
export function computeBuildInfo(env, flagNames) {
  const flags = {};
  for (const name of flagNames) flags[name] = truthy(env[name]);
  return {
    gitCommit: env.GIT_COMMIT || "unknown",
    buildTimestamp: env.BUILD_TIMESTAMP || "unknown",
    flags,
    note: "Frontend build fingerprint. Non-sensitive deploy-parity oracle; compare gitCommit to the release SHA.",
  };
}

function main() {
  // Explicit, per-name access (never opaque) so the parity guard can validate each env var.
  const env = {
    GIT_COMMIT: process.env.GIT_COMMIT,
    BUILD_TIMESTAMP: process.env.BUILD_TIMESTAMP,
    NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED: process.env.NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED,
    NEXT_PUBLIC_OPERATIONS_ENABLED: process.env.NEXT_PUBLIC_OPERATIONS_ENABLED,
  };
  const info = computeBuildInfo(env, FLAG_NAMES);
  try {
    mkdirSync(dirname(OUT), { recursive: true });
    writeFileSync(OUT, JSON.stringify(info, null, 2) + "\n", "utf8");
    console.log(`emit-build-info: wrote ${OUT} (commit=${info.gitCommit})`);
  } catch (e) {
    console.warn(`emit-build-info: could not write ${OUT} (${e.message})`);
  }
}

// Only run the writer when invoked directly (not when imported by a test).
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
