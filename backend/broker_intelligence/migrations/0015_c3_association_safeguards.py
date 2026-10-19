# C3 — association safeguards (PD review of #511/#513), DARK & additive. NOT applied to prod.
#
# 1. trading_account FK CASCADE -> PROTECT (history-safe; AlterField, no DDL).
# 2. Append-only AUDIT immutability — SELECTIVE BEFORE UPDATE (rejects evidential-column changes but ALLOWS the four
#    SET_NULL FK cascade updates, so deleting a referenced user/account/identity/mailbox still succeeds) + BEFORE
#    DELETE. Mirrors the bi_brokerevent_block_evidence_update pattern (migration 0002), never the blanket EvidenceBlob
#    one (which would abort every parent deletion via the cascade SET_NULL updates).
# 3. FAIL-CLOSED VERIFIED — a BEFORE INSERT OR UPDATE trigger refusing status='VERIFIED' unless the mailbox owner and
#    the broker-email-identity owner are both the trading account's owner. Ownership is the ACCOUNT owner, never the
#    shared email address. Fail-closed: a null mailbox / unowned identity / owner mismatch RAISEs.
#
# All reversible. Postgres (prod + CI test DB). ASCII-only SQL.

import django.db.models.deletion
from django.db import migrations, models


AUDIT_IMMUTABLE_FORWARD = r"""
CREATE OR REPLACE FUNCTION bi_tabc_audit_block_update() RETURNS trigger AS $$
BEGIN
    -- Recorded FACTS are immutable (corrections are new rows).
    IF ROW(NEW.action, NEW.outcome, NEW.reason, NEW.status_before, NEW.status_after, NEW.proofs, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.action, OLD.outcome, OLD.reason, OLD.status_before, OLD.status_after, OLD.proofs, OLD.created_at) THEN
        RAISE EXCEPTION 'TradingAccountBrokerContactAudit is append-only; recorded facts are immutable (corrections are new rows)';
    END IF;
    -- Back-reference FKs (all ON DELETE SET_NULL) may ONLY decay to NULL on a parent cascade — never be re-pointed to
    -- a different value or un-nulled. This keeps parent deletions (incl. the operator purge) working WITHOUT allowing
    -- history to be silently re-linked (the refutation-11 lesson: a blanket update-block aborts every cascade).
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

DROP TRIGGER IF EXISTS bi_tabc_audit_no_update ON broker_intelligence_tradingaccountbrokercontactaudit;
CREATE TRIGGER bi_tabc_audit_no_update
    BEFORE UPDATE ON broker_intelligence_tradingaccountbrokercontactaudit
    FOR EACH ROW EXECUTE FUNCTION bi_tabc_audit_block_update();

CREATE OR REPLACE FUNCTION bi_tabc_audit_block_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'TradingAccountBrokerContactAudit is append-only; DELETE is not allowed';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bi_tabc_audit_no_delete ON broker_intelligence_tradingaccountbrokercontactaudit;
CREATE TRIGGER bi_tabc_audit_no_delete
    BEFORE DELETE ON broker_intelligence_tradingaccountbrokercontactaudit
    FOR EACH ROW EXECUTE FUNCTION bi_tabc_audit_block_delete();
"""

AUDIT_IMMUTABLE_REVERSE = r"""
DROP TRIGGER IF EXISTS bi_tabc_audit_no_delete ON broker_intelligence_tradingaccountbrokercontactaudit;
DROP FUNCTION IF EXISTS bi_tabc_audit_block_delete();
DROP TRIGGER IF EXISTS bi_tabc_audit_no_update ON broker_intelligence_tradingaccountbrokercontactaudit;
DROP FUNCTION IF EXISTS bi_tabc_audit_block_update();
"""

VERIFIED_CONSISTENCY_FORWARD = r"""
CREATE OR REPLACE FUNCTION bi_tabc_require_verified_consistency() RETURNS trigger AS $$
DECLARE
    acct_owner integer;
    mbx_owner integer;
    idn_owner integer;
    idn_acct_owner integer;
BEGIN
    IF NEW.status <> 'VERIFIED' THEN
        RETURN NEW;   -- only the VERIFIED transition is constrained; every other status is unconstrained here
    END IF;
    -- (1) a VERIFIED binding needs a mailbox whose owner is the account owner (mailbox control alone is not proof).
    IF NEW.connected_mailbox_id IS NULL THEN
        RAISE EXCEPTION 'TradingAccountBrokerContact cannot be VERIFIED without a connected mailbox';
    END IF;
    SELECT user_id INTO acct_owner FROM trading_tradingaccount WHERE id = NEW.trading_account_id;
    SELECT user_id INTO mbx_owner FROM broker_intelligence_connectedmailbox WHERE id = NEW.connected_mailbox_id;
    IF acct_owner IS NULL OR mbx_owner IS NULL OR mbx_owner <> acct_owner THEN
        RAISE EXCEPTION 'TradingAccountBrokerContact VERIFIED requires mailbox owner to equal the account owner';
    END IF;
    -- (2) the broker email identity must belong to the account owner (directly, or via its own trading account).
    SELECT user_id INTO idn_owner FROM broker_intelligence_brokeremailidentity WHERE id = NEW.broker_email_identity_id;
    SELECT ta.user_id INTO idn_acct_owner
        FROM broker_intelligence_brokeremailidentity i
        LEFT JOIN trading_tradingaccount ta ON ta.id = i.trading_account_id
        WHERE i.id = NEW.broker_email_identity_id;
    IF NOT ( (idn_owner IS NOT NULL AND idn_owner = acct_owner)
          OR (idn_acct_owner IS NOT NULL AND idn_acct_owner = acct_owner) ) THEN
        RAISE EXCEPTION 'TradingAccountBrokerContact VERIFIED requires the broker email identity to belong to the account owner';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS bi_tabc_verified_consistency ON broker_intelligence_tradingaccountbrokercontact;
CREATE TRIGGER bi_tabc_verified_consistency
    BEFORE INSERT OR UPDATE ON broker_intelligence_tradingaccountbrokercontact
    FOR EACH ROW EXECUTE FUNCTION bi_tabc_require_verified_consistency();
"""

VERIFIED_CONSISTENCY_REVERSE = r"""
DROP TRIGGER IF EXISTS bi_tabc_verified_consistency ON broker_intelligence_tradingaccountbrokercontact;
DROP FUNCTION IF EXISTS bi_tabc_require_verified_consistency();
"""


class Migration(migrations.Migration):

    dependencies = [
        ('broker_intelligence', '0014_c2_owner_scoped_evidence_delivery_dedup'),
        ('trading', '0020_broker_server_environment_governance'),
    ]

    operations = [
        migrations.AlterField(
            model_name='tradingaccountbrokercontact',
            name='trading_account',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='broker_contacts',
                                    to='trading.tradingaccount'),
        ),
        migrations.AlterField(
            model_name='tradingaccountbrokercontactaudit',
            name='contact',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='audit', to='broker_intelligence.tradingaccountbrokercontact'),
        ),
        migrations.RunSQL(sql=AUDIT_IMMUTABLE_FORWARD, reverse_sql=AUDIT_IMMUTABLE_REVERSE),
        migrations.RunSQL(sql=VERIFIED_CONSISTENCY_FORWARD, reverse_sql=VERIFIED_CONSISTENCY_REVERSE),
    ]
