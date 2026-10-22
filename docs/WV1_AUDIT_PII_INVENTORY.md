# WV1 — Audit-Table Personal-Data Inventory (Finding 8)

- **Date:** 2026-10-21 · Classifies every field of the "retained" audit tables for personal data. Personal
  data can exist without message content — so "no message body" is **not** a retention justification.
- **Classification:** **Delete** · **Anonymise** (clear the identifier, keep the action/outcome for
  accountability) · **Retain-under-basis** (legal/security) · **Retain-with-expiry**.

## `broker_intelligence.BrokerEmailIdentityAudit`
| Field | Personal data? | Classification on subject erasure |
|---|---|---|
| `email` | **Yes — subject email address** | **Anonymise** (clear/hash) — keep the row for accountability |
| `actor` | user label (operator or 'system') | Retain-under-basis (accountability) — not the data subject |
| `broker_name` | no (broker) | Retain |
| `subject_user` (FK) | **Yes — links to the subject** | SET_NULL on account delete; on erasure **Anonymise** (null) |
| `trading_account` / `connected_mailbox` (FK) | indirect | SET_NULL / anonymise |
| `action`, `outcome`, `reason`, `status_before/after` | sanitised | Retain-under-basis (accountability) |
| `proofs` (JSON) | structured proof summary (may contain addresses) | **Review + anonymise** identifying entries |
| `created_at` | no | Retain |

## `broker_intelligence.TradingAccountBrokerContactAudit`
| Field | Personal data? | Classification |
|---|---|---|
| `subject_user` (FK) | **Yes** | Anonymise (null) |
| `trading_account` / `broker_email_identity` / `connected_mailbox` (FK) | indirect | SET_NULL / anonymise |
| `action`, `outcome`, `reason`, `status_before/after` | sanitised | Retain-under-basis |
| `proofs` (JSON) | may contain addresses | **Review + anonymise** |
| `created_at` | no | Retain |

## `broker_intelligence.BrokerProfileAudit`
Broker **config** change history (who changed broker settings). No data-subject personal data beyond the
`actor` operator label. **Retain** (operational/accountability).

## `core.AuditEvent`
| Field | Personal data? | Classification |
|---|---|---|
| `user` (FK) | **Yes** | Anonymise (null) on erasure, except security-evidence rows |
| `ip_address` | **Yes — IP address** | **Anonymise/truncate** after the security-forensics window; Retain-with-expiry for security events |
| `user_agent` | linkable | Anonymise after window |
| `path`, `method`, `entity_type`, `entity_id` | low | Retain-under-basis |
| `metadata` (JSON) | **may contain personal data** | **Review + anonymise** identifying entries |
| `event_type`, `severity`, `created_at` | no | Retain |

## Consequences (corrected design)
- The erasure process must **anonymise** the subject-identifying fields (`email`, `subject_user`,
  `ip_address`, `user_agent`, identifying JSON) rather than simply "retain the audit." The accountability
  record (action/outcome/timestamp) is kept under a documented legal basis.
- These tables are append-only (delete blocked); anonymisation is an **UPDATE**, which the privileged backend
  performs under the same role-enforced boundary (and any UPDATE triggers must be made to permit the governed
  anonymisation, mirroring the delete gate). This is part of the privileged-backend increment.
- `core.AuditEvent` needs the operational-vs-compliance **retention split** (security events with IP kept
  longer under a lawful basis; others anonymised/purged on the retention schedule).

**DPIA + retention schedule updated** to reflect that audit tables hold personal data requiring
anonymise/retain-under-basis handling (not blanket retention).
