#!/usr/bin/env python3
"""
Remove numbered duplicate files like:
- manage 2.py  -> manage.py
- page 2.tsx   -> page.tsx
- .env 2.local -> .env.local
- package-lock 2.json -> package-lock.json
- .gitignore 3 -> .gitignore

Safety:
- Matches files whose *stem* ends with: " <number>" (space + digits), number >= 2
- By default: DRY RUN (prints only)
- With --apply: moves duplicates to .trash_duplicates/<timestamp>/... (mirrors relative paths)
- Only removes/moves if base file exists unless --force
- NEW: --rename-missing-base promotes the lowest-numbered duplicate to become the base if base is missing
"""

from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Tuple, Dict

DEFAULT_EXCLUDES = {
    ".git", ".venv", "venv", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache",
    "dist", "build",
    ".trash_duplicates",
}

DUP_RE = re.compile(r"^(?P<base>.+)\s+(?P<num>\d+)$")


@dataclass
class Match:
    dup_path: Path
    base_path: Path
    num: int


def iter_files(root: Path, excludes: set[str]) -> Iterable[Path]:
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in excludes for part in p.parts):
            continue
        if p.is_symlink():
            continue
        yield p


def find_numbered_duplicates(root: Path, excludes: set[str]) -> List[Match]:
    matches: List[Match] = []
    for p in iter_files(root, excludes):
        stem = p.stem  # filename without last suffix
        m = DUP_RE.match(stem)
        if not m:
            continue

        num = int(m.group("num"))
        if num < 2:
            continue

        base_stem = m.group("base")

        # base filename keeps same suffix (handles: page 2.tsx -> page.tsx, .env 2.local -> .env.local)
        base_name = base_stem + p.suffix  # suffix may be "" for no-extension files
        base_path = p.with_name(base_name)

        matches.append(Match(dup_path=p, base_path=base_path, num=num))
    return matches


def move_to_trash(dups: List[Match], trash_dir: Path, root: Path) -> None:
    trash_dir.mkdir(parents=True, exist_ok=True)
    for m in dups:
        rel = m.dup_path.relative_to(root)
        dest = trash_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"MOVE: {m.dup_path} -> {dest}")
        shutil.move(str(m.dup_path), str(dest))


def delete_permanently(dups: List[Match]) -> None:
    for m in dups:
        print(f"DELETE: {m.dup_path}")
        m.dup_path.unlink(missing_ok=True)


def rename_missing_bases(groups: Dict[Path, List[Match]], *, lowercase_ext: bool) -> List[Tuple[Path, Path]]:
    """
    For any group where base_path does not exist, rename the lowest-numbered duplicate to base_path.
    Returns list of (old_path, new_path) rename operations performed.
    """
    renames: List[Tuple[Path, Path]] = []

    for base_path, items in groups.items():
        if base_path.exists():
            continue

        # choose the smallest number as the canonical base
        items_sorted = sorted(items, key=lambda x: x.num)
        chosen = items_sorted[0]

        target = base_path
        if lowercase_ext and target.suffix:
            target = target.with_name(target.stem + target.suffix.lower())

        # avoid clobbering anything
        if target.exists():
            print(f"SKIP RENAME (target exists): {chosen.dup_path} -> {target}")
            continue

        print(f"RENAME: {chosen.dup_path} -> {target}")
        chosen.dup_path.rename(target)
        renames.append((chosen.dup_path, target))

    return renames


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", default=".", help="Project root to scan (default: current directory).")
    ap.add_argument("--apply", action="store_true", help="Actually perform changes (default is dry-run).")
    ap.add_argument("--permanent", action="store_true", help="Permanently delete instead of moving to trash.")
    ap.add_argument("--force", action="store_true", help="Remove even if base file does not exist.")
    ap.add_argument("--exclude", action="append", default=[], help="Additional directory names to exclude (repeatable).")

    ap.add_argument(
        "--rename-missing-base",
        action="store_true",
        help="If base file is missing, rename the lowest-numbered duplicate to become the base (e.g. README 2.md -> README.md).",
    )
    ap.add_argument(
        "--lowercase-ext",
        action="store_true",
        help="When renaming missing bases, lowercase the extension (e.g. .MD -> .md).",
    )

    args = ap.parse_args()
    root = Path(args.root).resolve()
    excludes = set(DEFAULT_EXCLUDES) | set(args.exclude)

    matches = find_numbered_duplicates(root, excludes)
    matches.sort(key=lambda x: str(x.dup_path))

    # group by intended base path
    groups: Dict[Path, List[Match]] = {}
    for m in matches:
        groups.setdefault(m.base_path, []).append(m)

    print(f"Scanning: {root}")
    print(f"Excluded dirs: {sorted(excludes)}")
    print(f"Found {len(matches)} candidate numbered duplicates.\n")

    for m in matches:
        base_status = "OK" if m.base_path.exists() else "MISSING"
        print(f"- {m.dup_path}  (base: {m.base_path.name} = {base_status})")

    if not args.apply:
        if args.rename_missing_base:
            print("\nNOTE: --rename-missing-base is set (dry-run). On apply, the lowest-numbered duplicate will be renamed into the missing base name.")
        print("\nDRY RUN only. Re-run with --apply to actually remove/move files.")
        return 0

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    trash_dir = root / ".trash_duplicates" / timestamp

    # 1) Optionally rename missing bases (promote one file to become the base)
    if args.rename_missing_base:
        rename_missing_bases(groups, lowercase_ext=args.lowercase_ext)

    # 2) Recompute after renames (some bases now exist)
    matches2 = find_numbered_duplicates(root, excludes)
    matches2.sort(key=lambda x: str(x.dup_path))

    # Decide which files to remove/move
    to_remove: List[Match] = []
    skipped = 0
    for m in matches2:
        if m.base_path.exists() or args.force:
            to_remove.append(m)
        else:
            skipped += 1
            print(f"SKIP (no base file): {m.dup_path} (expected {m.base_path})")

    print()
    if args.permanent:
        delete_permanently(to_remove)
        print(f"\nDone. Deleted: {len(to_remove)}, Skipped: {skipped}")
    else:
        move_to_trash(to_remove, trash_dir=trash_dir, root=root)
        print(f"\nDone. Moved to trash: {len(to_remove)}, Skipped: {skipped}")
        print(f"Trash location: {trash_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())