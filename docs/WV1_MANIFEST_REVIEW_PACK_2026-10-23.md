# WV1 / ADR-0054 F-8 — Production object-manifest human review pack (2026-10-23)

**Purpose.** Independent human review of the production object manifest + digest that will gate the F-8
runtime-role separation cutover. **Two named human reviewers are required before cutover; the AI agent does not
count as a reviewer.** This pack is **non-sensitive** (no secrets, credentials, DB contents, or host details) and
is safe to circulate to the named reviewers.

## 1. What is being reviewed
The read-only inventory of every application object in production schema `public`, captured 2026-10-23 with
`ops/erasure/cutover.sh --capture` (read-only; `SET default_transaction_read_only`). The cutover
(`separate_runtime_role.sql`) transfers ownership of **exactly** these objects to `guvfx_schema_owner` and will
**refuse** if the live inventory does not match this reviewed manifest.

## 2. Manifest digest (authoritative)
```
MANIFEST_SHA256 = 1ffe99ac920238d5cdc7d188218888f5d753f39520d979054aa089cd2997d539
```
Rows: **272** (`obj_kind|obj_schema|obj_identity|current_owner|classification`). Full manifest file
(`prod_manifest.psv`, 272 lines) is provided to reviewers alongside this pack for line-by-line inspection.

## 3. Object counts by type
| Kind | Count |
|---|---|
| table | 137 |
| sequence | 130 |
| function | 5 (all trigger functions; **0 SECURITY DEFINER**) |
| view / matview / foreign table | 0 |
| **Total** | **272** |

## 4. Classification
| Classification | Count |
|---|---|
| application | 270 |
| **protected (immutable evidence)** | **2** |

## 5. The two protected evidence objects (must be exactly these)
| obj_kind | obj_identity | owner | classification |
|---|---|---|---|
| table | `broker_intelligence_evidenceblob` | guvfx | protected |
| table | `broker_intelligence_brokerevent` | guvfx | protected |

The runtime application role receives `SELECT/INSERT/UPDATE` on these (append-only) and **never** `DELETE`/`TRUNCATE`.

## 6. Ownership summary
- **All 272 objects are currently owned by the bootstrap role `guvfx`** (the sole cluster superuser / DB owner).
- **All objects are in schema `public`.**
- Schema `public` is currently owned by `pg_database_owner` (PostgreSQL 15 default); the cutover re-owns it to
  `guvfx_schema_owner`.

## 7. Unexpected / missing / differently-owned result
**None.** The live inventory equals the manifest exactly: no object outside the manifest, no manifest object
missing live, and no object owned by anything other than the bootstrap `guvfx` role. Object count (272) reconciles
to the expected application model (137 tables + 130 sequences + 5 functions).

## 8. Independently reproduce the digest (reviewer self-check)
Read-only; run against the production database (or the certified restored copy) as the bootstrap role:
```bash
# 1) capture the manifest (read-only; no mutation)
ops/erasure/cutover.sh --capture --db guvfx --app-schema public --out /tmp/review_manifest.psv
# 2) compute the digest (byte-sorted) and compare to §2
LC_ALL=C sort /tmp/review_manifest.psv | shasum -a 256
# 3) confirm exactly two 'protected' rows, both evidence tables:
grep -E '\|protected$' /tmp/review_manifest.psv
# 4) confirm every object is owned by guvfx and in schema public:
awk -F'|' '$2!="public" || $4!="guvfx" {print "ANOMALY:",$0}' /tmp/review_manifest.psv
```
A reviewer independently obtaining `1ffe99ac…2997d539` and the two protected rows corroborates this pack.

## 9. Approval
By signing, each reviewer attests they have independently reproduced the digest in §8, confirmed the two protected
evidence objects in §5, and confirmed the "no unexpected/missing/differently-owned" result in §7.

| Role | Name | Organisation | Date | Signature | Digest reproduced? |
|---|---|---|---|---|---|
| Reviewer 1 | Nuno Rodolfo Ferreira Do Amaral | Virtus Logic UK Ltd | __________ | __________ | ☐ yes |
| Reviewer 2 | **[TO BE NOMINATED — a separate named human with sufficient technical competence]** | __________ | __________ | __________ | ☐ yes |

**Review status: INCOMPLETE — Reviewer 2 not yet nominated.** This pack must not be treated as approved until two
genuine human signatures are present. The AI agent is not a reviewer and has not signed.
