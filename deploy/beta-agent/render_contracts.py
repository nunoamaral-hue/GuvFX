"""Render STAGE_CONTRACTS to Markdown. The doc is GENERATED so it cannot drift from the code.

Not part of the agent runtime (like ``validate.py``, it is a tool, not a shipped module) and therefore
deliberately outside the integrity manifest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lifecycle import MUTATING_STAGES, STAGE_CONTRACTS   # noqa: E402

HEADER = """# CVM-Inc-3 B3P-2 — Per-stage contracts

<!-- GENERATED FILE — do not edit by hand.
     Source of truth: deploy/beta-agent/lifecycle.py :: STAGE_CONTRACTS
     Regenerate: python3 deploy/beta-agent/render_contracts.py > docs/B3P2_STAGE_CONTRACTS.md
     A test asserts this file matches the code, so an edit here without a code change FAILS CI. -->

Every mutating stage states what had to be true **before** it ran, what must hold **throughout**, and what
is true **after**. These are held as data in `lifecycle.STAGE_CONTRACTS`; the statuses each stage declares
are checked against the statuses its implementation can actually produce, so a stage cannot grow a new
outcome while keeping an old contract.

"""


def render() -> str:
    out = [HEADER]
    for stage in MUTATING_STAGES:
        c = STAGE_CONTRACTS[stage]
        out.append(f"## `{stage}`\n")
        out.append("**Preconditions**\n")
        out.extend(f"- {p}\n" for p in c["preconditions"])
        out.append(f"\n**Invariant** — {c['invariant']}\n")
        out.append("\n**Postconditions**\n")
        out.extend(f"- {p}\n" for p in c["postconditions"])
        out.append(f"\n**Statuses it may report** — {', '.join(c['statuses'])}\n\n")
    return "".join(out)


if __name__ == "__main__":
    sys.stdout.write(render())
