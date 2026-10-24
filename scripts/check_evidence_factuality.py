#!/usr/bin/env python3
"""Verify point-in-time evidence claims against an immutable Git commit.

The manifest remains data: this tool never executes commands recorded in it.
It checks that the recorded commit exists in the current history, referenced
artefacts exist at that commit, checksums match the committed bytes, declared
tracked-file counts are exact, and saved unittest output supports any test-count
claim. An optional clean-tree gate covers handoff-time workspace state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import sys


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
UNITTEST_COUNT = re.compile(rb"(?:^|\n)Ran ([0-9]+) tests? in ")
UNITTEST_OK = re.compile(rb"(?:^|\n)OK(?:\s|$)")


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str, check: bool = True) -> bytes:
    completed = subprocess.run(
        ["git", "-C", repo, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise GitError(completed.stderr.decode("utf-8", "replace").strip() or "git command failed")
    return completed.stdout


def _safe_path(value: str) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and value == path.as_posix()


def _commit_bytes(repo: str, commit: str, path: str) -> bytes:
    return _git(repo, "show", f"{commit}:{path}")


def verify_data(name: str, data, repo: str = REPO) -> list[str]:
    findings: list[str] = []
    if not isinstance(data, dict):
        return [f"{name}: top-level value is not an object"]
    commit = data.get("head_commit")
    if not isinstance(commit, str) or not FULL_SHA.fullmatch(commit):
        return [f"{name}: head_commit must be a full immutable SHA"]
    try:
        _git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
        ancestor = subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", commit, "HEAD"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ancestor.returncode != 0:
            findings.append(f"{name}: head_commit is not an ancestor of current HEAD")
    except GitError:
        return [f"{name}: head_commit does not exist in this repository"]

    for path in data.get("artefact_locations", []):
        if not _safe_path(path):
            findings.append(f"{name}: unsafe artefact path {path!r}")
            continue
        try:
            _git(repo, "cat-file", "-e", f"{commit}:{path}")
        except GitError:
            findings.append(f"{name}: artefact is absent at head_commit: {path}")

    for path, expected in data.get("checksums", {}).items():
        if not _safe_path(path):
            findings.append(f"{name}: unsafe checksum path {path!r}")
            continue
        try:
            content = _commit_bytes(repo, commit, path)
        except GitError:
            findings.append(f"{name}: checksummed file is absent at head_commit: {path}")
            continue
        actual = "sha256:" + hashlib.sha256(content).hexdigest()
        if actual != expected:
            findings.append(f"{name}: checksum mismatch at head_commit: {path}")

    factuality = data.get("factuality", {})
    if not isinstance(factuality, dict):
        findings.append(f"{name}: factuality must be an object")
        return findings
    expected_files = factuality.get("tracked_file_count")
    if expected_files is not None:
        actual_files = len(_git(repo, "ls-tree", "-r", "--name-only", commit).splitlines())
        if actual_files != expected_files:
            findings.append(
                f"{name}: tracked_file_count is {expected_files}, commit contains {actual_files}"
            )
    for run in factuality.get("test_runs", []):
        path = run.get("output_path") if isinstance(run, dict) else None
        if not _safe_path(path):
            findings.append(f"{name}: unsafe test output path {path!r}")
            continue
        try:
            output = _commit_bytes(repo, commit, path)
        except GitError:
            findings.append(f"{name}: test output is absent at head_commit: {path}")
            continue
        match = UNITTEST_COUNT.search(output)
        if match is None:
            findings.append(f"{name}: unittest count not found in {path}")
        elif int(match.group(1)) != run.get("expected_count"):
            findings.append(
                f"{name}: test count for {path} is {int(match.group(1))}, expected {run.get('expected_count')}"
            )
        if run.get("expected_status") == "PASS" and UNITTEST_OK.search(output) is None:
            findings.append(f"{name}: unittest PASS marker not found in {path}")
    if factuality.get("require_clean_tree"):
        dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
        if dirty:
            findings.append(f"{name}: working tree is not clean")
    return findings


def verify_file(path: str, repo: str = REPO) -> list[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return [f"{os.path.basename(path)}: unreadable / invalid JSON ({type(exc).__name__})"]
    return verify_data(os.path.basename(path), data, repo)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", help="evidence manifest to verify")
    parser.add_argument("--repo", default=REPO, help="repository containing the recorded commit")
    args = parser.parse_args(argv)
    findings = verify_file(args.manifest, os.path.abspath(args.repo))
    if findings:
        print("EVIDENCE-FACTUALITY: FAIL")
        for finding in findings:
            print("  " + finding)
        return 1
    print("EVIDENCE-FACTUALITY: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
