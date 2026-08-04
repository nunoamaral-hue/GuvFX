#!/usr/bin/env node
/**
 * WP4.1 (ADR-0031) — Frontend Source-of-Truth parity guard.
 *
 * The permanent, repository-side check that keeps the repo the SINGLE authoritative frontend source. It
 * runs in CI (wired as the npm `prebuild` hook, so every `next build` enforces it) and locally via
 * `npm run verify:parity` / `make frontend-parity`. It is filesystem-based (no git dependency) and fails
 * cleanly with a list of violations. It asserts:
 *
 *   1. NO editor/backup/junk artefacts anywhere in the source tree (*.bak*, ._*, *.orig, *.rej, *.tmp) —
 *      the manual patches / hidden edits WP4.1 eliminated must never return.
 *   2. A .dockerignore exists and excludes dependencies, build output and junk, so the Docker build
 *      context is exactly the authoritative source (reproducible; cannot absorb a stray manual patch).
 *   3. The ROUTE tree matches parity/routes.json (detects added / removed / renamed routes).
 *   4. The COMPONENT set matches parity/components.json (detects added / removed / duplicated components).
 *   5. Every frontend env var (`process.env.*` / `NEXT_PUBLIC_*`) is on the documented allow-list
 *      parity/env-allowlist.json — an undocumented env var (or feature flag) fails the build.
 *
 * Build artefacts (node_modules, .next, out, build, coverage, .git) are excluded from the scan.
 */
import { readFileSync, existsSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(fileURLToPath(import.meta.url), "..", "..");
const SRC = join(ROOT, "src");
const EXCLUDE_DIRS = new Set(["node_modules", ".next", ".git", "out", "build", "coverage"]);
const JUNK = /(\.bak(\..*|_.*)?$)|(^\._)|(\.orig$)|(\.rej$)|(\.tmp$)/;
const REQUIRED_DOCKERIGNORE = ["node_modules", ".next", "*.bak", "._*", ".git", "*.tsbuildinfo"];

const errors = [];
const rel = (p) => relative(ROOT, p).split(sep).join("/");

function walk(dir, acc = []) {
  if (!existsSync(dir)) return acc;
  for (const name of readdirSync(dir)) {
    if (EXCLUDE_DIRS.has(name)) continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, acc);
    else acc.push(full);
  }
  return acc;
}

function readJson(relpath) {
  const p = join(ROOT, relpath);
  if (!existsSync(p)) { errors.push(`missing parity manifest: ${relpath}`); return null; }
  try { return JSON.parse(readFileSync(p, "utf8")); }
  catch (e) { errors.push(`unparseable parity manifest ${relpath}: ${e.message}`); return null; }
}

function compareSet(relpath, actual, label) {
  const expected = readJson(relpath);
  if (expected === null) return;
  const exp = new Set(expected), act = new Set(actual);
  for (const a of actual) if (!exp.has(a)) errors.push(`unexpected ${label} not in ${relpath}: ${a}`);
  for (const e of expected) if (!act.has(e)) errors.push(`${label} in ${relpath} missing from disk: ${e}`);
}

const allFiles = walk(ROOT);

// 1. No junk artefacts.
for (const f of allFiles) {
  const base = f.split(sep).pop();
  if (JUNK.test(base)) errors.push(`junk artefact present (backup/editor file): ${rel(f)}`);
}

// 2. .dockerignore present + excludes junk/build output.
const di = join(ROOT, ".dockerignore");
if (!existsSync(di)) {
  errors.push(".dockerignore missing — the Docker build context would absorb junk / be non-reproducible");
} else {
  const lines = readFileSync(di, "utf8").split("\n").map((l) => l.trim());
  for (const req of REQUIRED_DOCKERIGNORE) {
    if (!lines.includes(req)) errors.push(`.dockerignore missing required exclusion: ${req}`);
  }
}

// 3 + 4. Route and component inventories.
const routeFiles = walk(join(SRC, "app")).map(rel).filter((p) => /\/(page|route|layout)\.tsx?$/.test(p)).sort();
compareSet("parity/routes.json", routeFiles, "route file");
const componentFiles = walk(join(SRC, "components")).map(rel).sort();
compareSet("parity/components.json", componentFiles, "component");

// 5. Env allow-list (env + feature-flag validation).
const allow = new Set(readJson("parity/env-allowlist.json") || []);
const envRefs = new Set();
for (const f of allFiles) {
  if (!rel(f).startsWith("src/") || !/\.(ts|tsx|mjs|cjs|js|jsx)$/.test(f)) continue;
  const txt = readFileSync(f, "utf8");
  for (const m of txt.matchAll(/process\.env\.([A-Z0-9_]+)|(NEXT_PUBLIC_[A-Z0-9_]+)/g)) {
    envRefs.add(m[1] || m[2]);
  }
}
for (const e of [...envRefs].sort()) {
  if (!allow.has(e)) errors.push(`undocumented frontend env var/flag: ${e} — add to parity/env-allowlist.json + ADR-0031`);
}

if (errors.length) {
  console.error(`\n✗ WP4.1 frontend parity FAILED (${errors.length} violation(s)):`);
  for (const e of errors) console.error("    - " + e);
  console.error("\nSee docs/ADRs/0031-frontend-source-of-truth.md. The repository is the single authoritative source.\n");
  process.exit(1);
}
console.log(
  `✓ WP4.1 frontend parity OK — ${routeFiles.length} route files, ${componentFiles.length} components, ` +
  `no junk, .dockerignore present, ${envRefs.size} env var(s) all on the allow-list.`,
);
