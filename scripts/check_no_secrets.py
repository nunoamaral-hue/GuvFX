#!/usr/bin/env python3
"""GuvFX secret scanner (standard library only).

Scans Git-tracked text files (which includes staged additions, since the Git
index is the source of truth) for high-confidence secrets: private-key headers
and well-known token/key formats. It never prints secret contents — only the
file, line number, and a category.

A fixture-only ignore marker lets the dedicated test file and the test fixture
directory carry deliberately-planted secret-like strings without failing the
scan. The same marker used anywhere else is itself reported, so there is no
general repository bypass.

Exit codes:
    0  no findings
    1  findings present
    2  scanner error (e.g. not a Git repository)

This scanner only reads files that Git already tracks/stages. It does not read
untracked files such as ``.claude/settings.local.json`` or ``.env``, the system
keychain, or anything outside the repository.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# Files larger than this are skipped (treated as data/binary, not source).
MAX_BYTES = 1_500_000

# Fixture-only ignore marker. A line containing this marker is ignored ONLY in
# the dedicated test file or the test fixtures directory. Anywhere else, its
# presence is itself a finding ("ignore-marker-misuse").
# Assembled from parts so the full marker token never appears contiguously in
# this source file (which is not a fixture context and would otherwise self-flag).
IGNORE_MARKER = "guvfx-secret-scan-" + "allow-fixture"

# The only paths (repo-relative, forward-slash) where IGNORE_MARKER is honoured.
DEDICATED_TEST_FILE = "tests/test_no_secrets.py"
FIXTURE_DIR_PREFIX = "tests/fixtures/"

# High-confidence secret patterns. Kept deliberately narrow to avoid false
# positives on ordinary source; broadening requires an approved decision, not a
# drive-by allow-list.
PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("private-key-header",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[0-9A-Za-z_]{22,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe-secret-key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{24,}\b")),
    ("slack-webhook",
     re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}")),
]


def _norm(rel_path: str) -> str:
    return rel_path.replace("\\", "/")


def is_fixture_context(rel_path: str) -> bool:
    """True if IGNORE_MARKER is allowed to suppress findings on this path."""
    norm = _norm(rel_path)
    return norm == DEDICATED_TEST_FILE or norm.startswith(FIXTURE_DIR_PREFIX)


def scan_text(text: str, rel_path: str) -> list[tuple[int, str]]:
    """Scan text content. Returns a list of (line_number, category) findings.

    Secret contents are never included in findings.
    """
    findings: list[tuple[int, str]] = []
    fixture = is_fixture_context(rel_path)
    for lineno, line in enumerate(text.splitlines(), start=1):
        has_marker = IGNORE_MARKER in line
        if has_marker and not fixture:
            # The marker may not be used outside the fixture context.
            findings.append((lineno, "ignore-marker-misuse"))
            continue
        if has_marker and fixture:
            # Legitimately suppressed planted fixture secret.
            continue
        for category, pattern in PATTERNS:
            if pattern.search(line):
                findings.append((lineno, category))
    return findings


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data


def scan_file(abs_path: str, rel_path: str) -> list[tuple[int, str]]:
    """Scan a single file safely. Returns (line_number, category) findings."""
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        return []
    if size > MAX_BYTES:
        return []
    try:
        with open(abs_path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError:
        return []
    if len(data) > MAX_BYTES or _looks_binary(data):
        return []
    text = data.decode("utf-8", errors="replace")
    return scan_text(text, rel_path)


def _git(args: list[str], root: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def repo_root() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out


def gather_files(root: str) -> list[str]:
    """Repo-relative paths of Git-tracked files plus staged additions."""
    paths: set[str] = set()
    # Index entries (tracked + staged-new files).
    tracked = _git(["ls-files", "-z"], root)
    paths.update(p for p in tracked.split("\0") if p)
    # Staged additions/modifications/renames/copies, for completeness.
    staged = _git(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], root
    )
    paths.update(p for p in staged.split("\0") if p)
    return sorted(paths)


def main(argv: list[str] | None = None) -> int:
    try:
        root = repo_root()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("check_no_secrets: not a Git repository", file=sys.stderr)
        return 2

    all_findings: list[tuple[str, int, str]] = []
    for rel in gather_files(root):
        abs_path = os.path.join(root, rel)
        if not os.path.isfile(abs_path):
            continue
        for lineno, category in scan_file(abs_path, rel):
            all_findings.append((rel, lineno, category))

    if all_findings:
        print("Potential secrets detected (contents not shown):")
        for rel, lineno, category in all_findings:
            print(f"  {rel}:{lineno}: [{category}]")
        print(f"\n{len(all_findings)} finding(s). Resolve before committing.")
        return 1

    print("check_no_secrets: no findings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
