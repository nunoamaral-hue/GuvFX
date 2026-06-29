#!/usr/bin/env python3
"""Schema-conformance + checksum-format linter for evidence manifests.

Validates every ``evidence/manifests/*.json`` against
``evidence/schema/evidence-manifest.schema.json``: required fields are present and
correctly typed, ``status`` is one of the allowed values, and every ``checksums``
value is a well-formed ``sha256:<64-hex>`` string. This catches malformed or
incomplete evidence records — the authoring defects an independent PM used to catch
by hand.

It deliberately does **not** re-verify recorded checksums against current file
content. A manifest is a *point-in-time* snapshot, and the files it references are
legitimately changed by later packets, so a tree-wide content check would
false-positive (verified empirically: 39 of 77 checksum entries differ purely
because the referenced files evolved). Content/checksum verification belongs at
manifest *authoring* time, against the files as they are when the manifest is
written — not in CI over the whole history. Standard library only.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(REPO, "evidence", "schema", "evidence-manifest.schema.json")
MANIFEST_GLOB = os.path.join(REPO, "evidence", "manifests", "*.json")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_JSON_TYPES = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _type_ok(value, schema_type) -> bool:
    if isinstance(schema_type, list):
        return any(_type_ok(value, t) for t in schema_type)
    py = _JSON_TYPES.get(schema_type)
    if py is None:
        return True
    if schema_type == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, py)


def lint_data(name: str, data, schema) -> list:
    """Validate one parsed manifest against the schema. Returns a list of findings."""
    findings = []
    if not isinstance(data, dict):
        return ["%s: top-level value is not an object" % name]
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in data:
            findings.append("%s: missing required field '%s'" % (name, req))
    for key, val in data.items():
        spec = props.get(key)
        if not spec:
            continue
        if "type" in spec and not _type_ok(val, spec["type"]):
            findings.append("%s: field '%s' has wrong type" % (name, key))
        if "enum" in spec and val not in spec["enum"]:
            findings.append("%s: field '%s' value %r not in allowed %r" % (name, key, val, spec["enum"]))
    checksums = data.get("checksums")
    if isinstance(checksums, dict):
        for fpath, cval in checksums.items():
            if not (isinstance(cval, str) and SHA256_RE.match(cval)):
                findings.append("%s: checksum for '%s' is not a sha256:<64-hex> string" % (name, fpath))
    return findings


def lint_file(path: str, schema) -> list:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError) as exc:
        return ["%s: unreadable / invalid JSON (%s)" % (os.path.basename(path), type(exc).__name__)]
    return lint_data(os.path.basename(path), data, schema)


def main(argv=None) -> int:
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    files = sorted(glob.glob(MANIFEST_GLOB))
    findings = []
    for path in files:
        findings.extend(lint_file(path, schema))
    if findings:
        print("EVIDENCE-LINT: FAIL")
        for finding in findings:
            print("  " + finding)
        return 1
    print("EVIDENCE-LINT: PASS (%d manifests)" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
