"""ADR-0054 — permit the governed erasure path to ANONYMISE the append-only contact-audit rows.

Migration 0015 made ``TradingAccountBrokerContactAudit`` append-only via ``bi_tabc_audit_block_update()`` (it
treats ``proofs`` as an immutable recorded fact and forbids re-pointing/un-nulling the back-reference FKs). The
audit-PII inventory requires erasure to ANONYMISE these rows (clear ``proofs``, null the subject/account links)
while KEEPING the accountability facts. This migration mirrors the delete-gate pattern (migration 0017): it adds
a tightly-scoped carve-out so that ONLY the privileged ``guvfx_erasure`` role may perform an anonymisation —
``proofs`` may only be cleared to ``{}`` and the back-references may only be nulled; the accountability facts
(``action``/``outcome``/``reason``/``status_before``/``status_after``/``created_at``) remain immutable for EVERY
path, including the governed one. Ordinary code is unchanged (its UPDATE is still fully blocked). Additive and
reversible; the reverse restores the exact 0015 body.
"""
from django.db import migrations


FORWARD = r"""
CREATE OR REPLACE FUNCTION bi_tabc_audit_block_update() RETURNS trigger AS $$
BEGIN
    -- Accountability facts are immutable on EVERY path (including the governed erasure path).
    IF ROW(NEW.action, NEW.outcome, NEW.reason, NEW.status_before, NEW.status_after, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.action, OLD.outcome, OLD.reason, OLD.status_before, OLD.status_after, OLD.created_at) THEN
        RAISE EXCEPTION 'TradingAccountBrokerContactAudit is append-only; accountability facts are immutable';
    END IF;

    IF current_user = 'guvfx_erasure' THEN
        -- Governed anonymisation (ADR-0054): proofs may ONLY be cleared to '{}'; back-references + subject may
        -- ONLY be nulled. Nothing else. This keeps the row for accountability while removing the PII.
        IF NEW.proofs IS DISTINCT FROM OLD.proofs AND NEW.proofs <> '{}'::jsonb THEN
            RAISE EXCEPTION 'governed anonymisation may only clear proofs to {}';
        END IF;
        IF (NEW.contact_id IS NOT NULL AND NEW.contact_id IS DISTINCT FROM OLD.contact_id)
           OR (NEW.subject_user_id IS NOT NULL AND NEW.subject_user_id IS DISTINCT FROM OLD.subject_user_id)
           OR (NEW.trading_account_id IS NOT NULL AND NEW.trading_account_id IS DISTINCT FROM OLD.trading_account_id)
           OR (NEW.broker_email_identity_id IS NOT NULL AND NEW.broker_email_identity_id IS DISTINCT FROM OLD.broker_email_identity_id)
           OR (NEW.connected_mailbox_id IS NOT NULL AND NEW.connected_mailbox_id IS DISTINCT FROM OLD.connected_mailbox_id) THEN
            RAISE EXCEPTION 'governed anonymisation may only null back-references';
        END IF;
        RETURN NEW;
    END IF;

    -- Ordinary path (unchanged from migration 0015): proofs is an immutable recorded fact.
    IF NEW.proofs IS DISTINCT FROM OLD.proofs THEN
        RAISE EXCEPTION 'TradingAccountBrokerContactAudit is append-only; recorded facts are immutable (corrections are new rows)';
    END IF;
    IF (NEW.contact_id IS NOT NULL AND NEW.contact_id IS DISTINCT FROM OLD.contact_id)
       OR (NEW.subject_user_id IS NOT NULL AND NEW.subject_user_id IS DISTINCT FROM OLD.subject_user_id)
       OR (NEW.trading_account_id IS NOT NULL AND NEW.trading_account_id IS DISTINCT FROM OLD.trading_account_id)
       OR (NEW.broker_email_identity_id IS NOT NULL AND NEW.broker_email_identity_id IS DISTINCT FROM OLD.broker_email_identity_id)
       OR (NEW.connected_mailbox_id IS NOT NULL AND NEW.connected_mailbox_id IS DISTINCT FROM OLD.connected_mailbox_id) THEN
        RAISE EXCEPTION 'TradingAccountBrokerContactAudit back-references are immutable except SET NULL on cascade';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

REVERSE = r"""
CREATE OR REPLACE FUNCTION bi_tabc_audit_block_update() RETURNS trigger AS $$
BEGIN
    IF ROW(NEW.action, NEW.outcome, NEW.reason, NEW.status_before, NEW.status_after, NEW.proofs, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.action, OLD.outcome, OLD.reason, OLD.status_before, OLD.status_after, OLD.proofs, OLD.created_at) THEN
        RAISE EXCEPTION 'TradingAccountBrokerContactAudit is append-only; recorded facts are immutable (corrections are new rows)';
    END IF;
    IF (NEW.contact_id IS NOT NULL AND NEW.contact_id IS DISTINCT FROM OLD.contact_id)
       OR (NEW.subject_user_id IS NOT NULL AND NEW.subject_user_id IS DISTINCT FROM OLD.subject_user_id)
       OR (NEW.trading_account_id IS NOT NULL AND NEW.trading_account_id IS DISTINCT FROM OLD.trading_account_id)
       OR (NEW.broker_email_identity_id IS NOT NULL AND NEW.broker_email_identity_id IS DISTINCT FROM OLD.broker_email_identity_id)
       OR (NEW.connected_mailbox_id IS NOT NULL AND NEW.connected_mailbox_id IS DISTINCT FROM OLD.connected_mailbox_id) THEN
        RAISE EXCEPTION 'TradingAccountBrokerContactAudit back-references are immutable except SET NULL on cascade';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


class Migration(migrations.Migration):
    dependencies = [("broker_intelligence", "0019_erasurerequest_consistency_checks")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
