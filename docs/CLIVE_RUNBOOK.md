# Clive Runbook for GuvPay / GuvFX

## 0) Non-negotiables (read first)

1. **Docs are source-of-truth for “where we are”**
   Read in this order:
   `docs/STATUS.md → docs/HANDOFF.md → docs/NEXT.md → docs/RUNBOOK.md → docs/KNOWN_ISSUES.md`

2. **Small diffs only**
   No big refactors. No formatting sweeps. No “cleanup” unless it’s the ticket.

3. **No silent deletions**
   If something must be removed, call it out in the plan + commit message and update `docs/NEXT.md`.

4. **Never edit build output**
   Don’t touch `frontend/.next/` or `node_modules/`.

5. **Every session ends with docs updated**
   Update: `docs/HANDOFF.md`, `docs/STATUS.md`, `docs/NEXT.md`, `docs/KNOWN_ISSUES.md` (as needed).

---

## 1) Start-of-session checklist (Clive)

From repo root:

```bash
git checkout main
git pull
make check
```

Then open and read:

* `docs/STATUS.md` (what’s current)
* `docs/HANDOFF.md` (latest handoff and next steps)
* `docs/NEXT.md` (what to do now)
* `docs/RUNBOOK.md` (setup + commands)
* `docs/KNOWN_ISSUES.md` (current blockers)

If `make check` fails:

* Fix if small and in-scope, otherwise record in `docs/KNOWN_ISSUES.md` with:

  * symptom, likely cause, workaround, next debugging step.

---

## 2) Creating a work branch (always)

Pick the top P0 item from `docs/NEXT.md`.

```bash
git checkout -b feat/<short-ticket-name>
```

Examples:

* `feat/broker-autocomplete-flow`
* `fix/ci-postgres-wait`
* `chore/cleanup-import-noise`

---

## 3) How Clive should use Codex safely (VS Code)

Before asking Codex to change anything:

### A) Required “Codex preflight prompt”

Copy/paste this at the top of every Codex request:

> Read `AGENTS.md` and `docs/NEXT.md` first.
> Confirm the exact task you’ll do (1–2 sentences).
> Show a plan (steps + files).
> Make the smallest diff possible.
> Do not delete code.
> Show the diff before finalizing.

### B) If Codex proposes deleting/refactoring

Clive reply must be:

> No deletions. No refactors. Deprecate if needed and add a follow-up to `docs/NEXT.md`.

### C) After Codex edits

Clive must run:

```bash
make check
```

If green, proceed. If not:

* copy/paste the error back to Codex and instruct: “Fix only what is needed to make `make check` pass. No refactors.”

---

## 4) Verification standards (minimum)

Every change must satisfy:

✅ `make check` from repo root
✅ No new lint errors
✅ No broken build
✅ Docs updated when state changes

If change touches backend:

* add/adjust tests where possible

If change touches CI:

* ensure workflow YAML still parses (Actions run)

---

## 5) Commit discipline (small + readable)

Before commit:

```bash
git status
git diff
```

Commit messages:

* `feat: ...`
* `fix: ...`
* `docs: ...`
* `ci: ...`
* `chore: ...`

Example:

```bash
git add <files>
git commit -m "feat: broker server autocomplete keyboard navigation"
```

---

## 6) Pull Request workflow (PR = Pull Request)

PR is the GitHub merge request from your branch into `main`.

Steps:

```bash
git push -u origin <your-branch>
```

On GitHub:

* Base: `main`
* Compare: `<your-branch>`
* Title: match commit intent
* Ensure checks are green ✅

Merge preference:

* **Squash & merge** for messy / multi-fix branches
* Normal merge is fine for clean, single-purpose PRs

---

## 7) End-of-session checklist (must do)

Clive updates these files **before stopping**:

### A) `docs/HANDOFF.md` (most important)

Update with:

* branch name
* last commit hash
* what changed (files)
* how to verify (`make check`)
* known issues/blockers
* exact next steps (ordered list)

### B) `docs/STATUS.md`

Update:

* current focus
* current branch (or main + active work branch)
* last green check date
* active blockers (if any)

### C) `docs/NEXT.md`

* mark done items
* reorder priorities (keep list short)
* add any new follow-ups discovered

### D) `docs/KNOWN_ISSUES.md`

* record any failures and workarounds

Then commit docs (if changed):

```bash
git add docs/HANDOFF.md docs/STATUS.md docs/NEXT.md docs/KNOWN_ISSUES.md
git commit -m "docs: update handoff/status/next"
```

---

## 8) Broker autocomplete MVP instructions (current P0)

Where it lives:

* `frontend/src/app/accounts/page.tsx` already has broker server suggestions.

Clive’s job is to **upgrade** it to meet acceptance criteria:

* debounced requests (300ms)
* cancel stale requests
* keyboard nav (↑/↓/Enter/Esc)
* loading/empty/error states
* selection works reliably

After implementing:

```bash
make check
git commit -m "feat: broker server autocomplete MVP improvements"
git push -u origin feat/broker-autocomplete-flow
```

Update docs:

* mark P0 #3 done in `docs/NEXT.md`
* add any follow-ups to P1

---

## 9) “What to do if context is lost”

If Clive starts a new chat or Codex loses context:

* Everything needed is in: `docs/STATUS.md`, `docs/HANDOFF.md`, `docs/NEXT.md`, `AGENTS.md`.
* Paste the latest `docs/HANDOFF.md` into the new chat and proceed.
