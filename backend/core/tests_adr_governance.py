"""Governance: ADR numbers must be unique programme-wide.

ADRs live in two directories with two naming conventions:
  - docs/architecture/ADR-0054-NAME.md
  - docs/ADRs/0052-name.md
A duplicate ADR number (as happened when the Withdrawal controlled-erasure ADR was first filed as 0051,
already held by the MT5 copier) is a governance defect. This test fails if any 4-digit ADR number is used
by more than one ADR file across BOTH directories.
"""
import re
from collections import defaultdict
from pathlib import Path

from django.test import SimpleTestCase

# backend/core/tests_adr_governance.py -> repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADR_DIRS = [_REPO_ROOT / "docs" / "architecture", _REPO_ROOT / "docs" / "ADRs"]
_NUM_RE = re.compile(r"(?:ADR-)?(\d{4})[-_]", re.IGNORECASE)


def _adr_files():
    for d in _ADR_DIRS:
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            m = _NUM_RE.match(p.name)
            if m:
                yield m.group(1), p


class AdrNumberUniquenessTests(SimpleTestCase):
    # Pre-existing cross-series overlaps between docs/architecture/ and docs/ADRs/ that PREDATE the controlled-
    # erasure work. The two directories historically ran independent numbering that collided here. Renumbering
    # historical ADRs is out of scope for this PR (and we must not modify other teams' ADRs); these are tracked
    # as a known debt to reconcile separately. The test still fails on ANY NEW duplicate.
    _KNOWN_HISTORICAL_OVERLAPS = {"0049", "0050"}

    def test_no_new_duplicate_adr_numbers(self):
        by_number = defaultdict(list)
        for num, path in _adr_files():
            by_number[num].append(str(path.relative_to(_REPO_ROOT)))
        duplicates = {n: fs for n, fs in by_number.items()
                      if len(fs) > 1 and n not in self._KNOWN_HISTORICAL_OVERLAPS}
        self.assertEqual(duplicates, {}, f"New duplicate ADR numbers found (must be unique programme-wide): {duplicates}")

    def test_controlled_erasure_is_0054_not_0051(self):
        """The Withdrawal controlled-erasure ADR must be 0054 (0051/0052/0053 are the MT5 copier's)."""
        nums = {num for num, path in _adr_files() if "controlled-erasure" in path.name.lower()}
        self.assertIn("0054", nums)
        self.assertNotIn("0051", nums)

    def test_at_least_some_adrs_discovered(self):
        """Guard against the glob silently finding nothing (which would make the uniqueness test vacuous)."""
        self.assertGreater(len(list(_adr_files())), 10)
