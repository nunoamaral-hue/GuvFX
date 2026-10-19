"""PR-B — IS6 (IS6FX) parser #2: deposit/withdrawal classification, EN + JP, and the deposit-never-withdrawal invariant.

The genuine captured IS6 deposit email (blob 3, 2026-10-12) is the RULE-11 positive control and is certified against
the IMMUTABLE evidence on production (read-only) — it is NEVER committed here. These fixtures REPRODUCE blob 3's
grammar (English label/value table; the Japanese variant is a faithful synthetic reconstruction of the same layout)
with PLACEHOLDER account/amount values, so the test suite carries no genuine account number or figure.

Headline safety tests:
  * a DEPOSIT email classifies as category DEPOSIT and NEVER EXTERNAL_WITHDRAWAL, and never creates a Withdrawal /
    enters withdrawal metrics (deposits can't contaminate withdrawal statistics — structural, not a filter);
  * completion is never inferred from a request (a deposit REQUEST → DEPOSIT_REQUESTED, not DEPOSIT_COMPLETED);
  * a withdrawal email is never read as EXTERNAL from prose (no prose→external path);
  * no confirmation URL / token ever reaches an emitted field;
  * the IS6 and TradersWay parsers never claim each other's mail (both share one mailbox).
"""
from __future__ import annotations

import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from broker_intelligence import ingestion, parsers, redaction
from broker_intelligence.evidence import EvidenceStore
from broker_intelligence.gmail_source import _html_to_text, parse_rfc822
from broker_intelligence.mail_source import MailMessage
from broker_intelligence.models import (BrokerEmailIdentity, BrokerEvent, EvidenceBlob, Provenance,
                                        TransactionCategory, Withdrawal)
from broker_intelligence.parsers_is6 import IS6Parser
from broker_intelligence.parsers_tradersway import TradersWayParser, register_default_parsers
from trading.models import TradingAccount

User = get_user_model()
_n = 0


def _uniq():
    global _n
    _n += 1
    return "61%04d" % _n


def _acct(user, account_number, broker="IS6FX"):
    return TradingAccount.objects.create(user=user, name="A", broker_name=broker, account_number=account_number,
                                         is_demo=False, is_active=True)


# PLACEHOLDER account/amount — NOT the genuine values (certified separately against blob 3 on production).
PLACEHOLDER_ACCT = "99887766"


def _en_deposit_body(acct=PLACEHOLDER_ACCT, amount="150 (USD)", fee="0 (USD)"):
    """Reproduce blob 3's English label/value table (HTML)."""
    return ("<html><body><p>Your payment procedure for your bank transfer has been completed.</p>"
            "<table><tr><td>Deposit details</td></tr>"
            "<tr><td>Date and time</td><td>2026/10/12 15:13</td></tr>"
            "<tr><td>Deposit method</td><td>Bank remittance</td></tr>"
            f"<tr><td>Account number</td><td>{acct}</td></tr>"
            f"<tr><td>Deposit amount</td><td>{amount}</td></tr>"
            f"<tr><td>Fee</td><td>{fee}</td></tr></table></body></html>")


def _jp_deposit_body(acct=PLACEHOLDER_ACCT, amount="150 (USD)", fee="0 (USD)"):
    """Faithful synthetic reconstruction of the same layout in Japanese (入金 = deposit, 手数料 = fee, 口座番号 = account)."""
    return ("<html><body><p>お客様の銀行送金のお手続きが完了しました。</p>"
            "<table><tr><td>入金詳細</td></tr>"
            "<tr><td>日時</td><td>2026/10/12 15:13</td></tr>"
            "<tr><td>入金方法</td><td>銀行送金</td></tr>"
            f"<tr><td>口座番号</td><td>{acct}</td></tr>"
            f"<tr><td>入金額</td><td>{amount}</td></tr>"
            f"<tr><td>手数料</td><td>{fee}</td></tr></table></body></html>")


class IS6ParserUnitTests(TestCase):
    """Pure parser behaviour (no DB), on the tag-stripped body a real IS6 HTML email produces."""

    def setUp(self):
        self.p = IS6Parser()

    def _parse_en(self, subject="[IS6FX] Notification of completed deposit processing (bank transfer)", **kw):
        return self.p.parse(subject=subject, body=_html_to_text(_en_deposit_body(**kw)), from_address="noreply@is6.com")

    # ---- positive: the certified blob-3 shape (placeholder values) ----
    def test_english_deposit_completed_full_extraction(self):
        r = self._parse_en()
        self.assertEqual(r.event_type, "DEPOSIT_COMPLETED")
        self.assertEqual(r.transaction_category, TransactionCategory.DEPOSIT)
        self.assertEqual(r.broker, "IS6")
        self.assertEqual(r.amount, Decimal("150"))
        self.assertEqual(r.currency, "USD")
        self.assertEqual(r.fee, Decimal("0"))               # a 0 fee is an explicit value, preserved (not None)
        self.assertEqual(r.fee_currency, "USD")
        self.assertEqual(r.destination_account, PLACEHOLDER_ACCT)  # money ENTERS this account → destination
        self.assertEqual(r.source_account, "")
        self.assertIsNone(r.occurred_at)                    # broker-stated local time has no tz → never fabricated

    def test_japanese_unrelated_verb_is_not_this_completion(self):
        # JP KYC/receipt boilerplate: identity verification (本人確認) / authentication (認証) completing or being
        # rejected in a deposit/withdrawal REQUEST email must NOT be read as the transaction itself (invariant C).
        cases = [
            ("【IS6FX】入金リクエスト受付のお知らせ",
             "<p>入金リクエストを受け付けました。</p><p>ご本人確認が完了しました。</p><p>入金額</p><p>200 (USD)</p>",
             "DEPOSIT_REQUESTED"),
            ("【IS6FX】出金リクエスト",
             "<p>出金リクエストを受け付けました。ご本人確認が完了しましたので、通常1営業日以内に処理されます。</p>",
             "WITHDRAWAL_REQUESTED"),
            ("【IS6FX】入金",
             "<p>入金のお申し込みを受け付けました。</p><p>認証が拒否されました。</p>", "DEPOSIT_REQUESTED"),
        ]
        for subj, html, expected in cases:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, expected, subj)

    def test_japanese_kyc_procedure_is_not_this_completion(self):
        # KYC/auth "procedure" (本人確認手続き / 認証手続き) completing or being rejected is NOT the money movement.
        cases = [
            ("【IS6FX】出金リクエスト",
             "<p>出金リクエストを受け付けました。ご本人確認のお手続きが完了しましたので、通常1営業日以内に処理されます。</p>", "WITHDRAWAL_REQUESTED"),
            ("【IS6FX】入金リクエスト受付",
             "<p>入金リクエストを受け付けました。</p><p>本人確認手続きが完了しました。</p><p>入金額</p><p>200 (USD)</p>", "DEPOSIT_REQUESTED"),
            ("【IS6FX】入金", "<p>入金のお申し込みを受け付けました。</p><p>認証手続きが拒否されました。</p>", "DEPOSIT_REQUESTED"),
        ]
        for subj, html, expected in cases:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, expected, subj)

    def test_completion_naming_destination_or_method_still_completes(self):
        # Capturing completed withdrawals is the stream's core signal — a completion that names the destination
        # account / method before the verb must still classify as completed (EN + JP kanji form).
        cases = [
            ("[IS6FX] Withdrawal notification", "<p>Your withdrawal to your bank account has been completed.</p>",
             "WITHDRAWAL_COMPLETED"),
            ("[IS6FX] Withdrawal notification",
             "<p>Your withdrawal of 500 USD to your account 99887766 has been completed.</p>", "WITHDRAWAL_COMPLETED"),
            ("[IS6FX] notification", "<p>Your deposit via this payment method has been completed.</p>",
             "DEPOSIT_COMPLETED"),
            ("【IS6FX】入金処理完了のお知らせ", "<p>お客様の入金手続きが完了致しました。</p>", "DEPOSIT_COMPLETED"),  # kanji 致し form
            ("【IS6FX】出金", "<p>お客様の出金手続きが完了致しました。</p>", "WITHDRAWAL_COMPLETED"),
        ]
        for subj, html, expected in cases:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, expected, html)

    def test_account_registration_or_method_change_is_not_a_money_event(self):
        # A money word used only as a MODIFIER of a non-money noun (bank-account registration, deposit-method change,
        # account verification) completing is NOT a deposit/withdrawal — declined (None).
        for subj, html in [
            ("【IS6FX】出金先口座登録完了", "<p>出金先口座の登録が完了しました。</p>"),
            ("【IS6FX】入金方法変更", "<p>入金方法の変更が完了しました。</p>"),
            ("[IS6FX] Withdrawal bank account registered",
             "<p>Registration of your withdrawal bank account has been completed.</p>"),
            ("[IS6FX] verification", "<p>Your deposit account verification has been completed.</p>"),
        ]:
            self.assertIsNone(self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com"),
                              subj)

    def test_daily_confirmation_statement_is_not_a_money_event(self):
        # Genuine LIVE-OBSERVED IS6 non-transactional template: "Daily Confirmation" (a daily account/position
        # statement). Captured 2026-10-13 as evidence blobs 4 & 5 during the SUPERVISED ingestion pass (the subsequent
        # scheduled timer cycles ingested zero) — authenticated is6.com mail that the parser correctly DECLINED
        # (retained as quarantined evidence, no event). This regression PINS these observed/minimized cases; it does
        # NOT guarantee every future template variant is rejected. Bodies are MINIMIZED, PII-redacted SYNTHETIC
        # renderings of the genuine non-transactional structure (genuine subject only; no genuine amounts / accounts /
        # recipients committed).
        for subj, html in [
            ("Daily Confirmation",
             "<p>Your daily account summary. Open positions and balance as of today. No action is required.</p>"),
            ("[IS6FX] Daily Confirmation",
             "<p>Daily statement: an informational summary of account activity for the period.</p>"),
            ("【IS6FX】デイリーコンファメーション",
             "<p>本日の口座サマリーのご案内です。お取引状況をご確認ください。</p>"),
        ]:
            self.assertIsNone(self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com"),
                              subj)

    def test_completed_substep_is_not_the_money_event(self):
        # A completed SUB-STEP (review/confirmation/verification/fee/card-auth) of a deposit/withdrawal is NOT the
        # money movement completing (invariant C). EN analogue of the JP subject anchor.
        for subj, html in [
            ("[IS6FX] Withdrawal request update",
             "<p>The review of your withdrawal has been completed. Your funds will be sent within 1-3 days.</p>"
             "<p>Withdrawal amount</p><p>150 (USD)</p>"),
            ("[IS6FX] Withdrawal", "<p>Confirmation of your withdrawal has been completed. Funds transfer to follow.</p>"),
            ("[IS6FX] Your deposit request has been received",
             "<p>Verification of your deposit has been completed. Funds credited after the bank confirms.</p>"
             "<p>Deposit amount</p><p>200 (USD)</p>"),
            ("[IS6FX] Deposit authentication",
             "<p>Your card authentication for this deposit has been completed. The deposit is now pending.</p>"),
        ]:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertNotIn(getattr(r, "event_type", ""), ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED"), html)
        # JP sub-steps (出金の審査/入金の確認 completed) are rejected by the JP subject anchor
        for subj, html in [
            ("【IS6FX】出金リクエスト", "<p>出金リクエストを受け付けました。出金の審査が完了しました。</p>"),
            ("【IS6FX】入金リクエスト", "<p>入金リクエストを受け付けました。入金の確認が完了しました。</p>"),
        ]:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertIn(r.event_type, ("DEPOSIT_REQUESTED", "WITHDRAWAL_REQUESTED"), html)

    def test_substep_via_any_preposition_is_not_the_money_event(self):
        # The EN subject anchor must reject a sub-step named via ANY governing preposition (the lookbehind list is
        # derived from _EN_PREP so it can't drift) — not just of/for.
        for prep in ("regarding", "concerning", "from", "into", "under", "onto", "of", "for"):
            r = self.p.parse(subject="[IS6FX] Withdrawal request update", from_address="noreply@is6.com",
                             body=_html_to_text(f"<p>The review {prep} your withdrawal has been completed. Funds will "
                                                f"be sent within 1-3 days.</p><p>Withdrawal amount</p><p>150 (USD)</p>"))
            self.assertNotEqual(r.event_type, "WITHDRAWAL_COMPLETED", prep)

    def test_japanese_cancellation_is_not_completion(self):
        # A withdrawal CANCELLATION confirmation (取消/キャンセル手続きが完了) must NOT be read as the withdrawal completing
        # — the money never left (semantic inversion).
        for html in ("<p>お客様よりご依頼いただきました出金を取り消す手続きが完了いたしました。</p>",
                     "<p>出金をキャンセルする手続きが完了いたしました。</p>",
                     "<p>出金は取消処理が完了いたしました。</p>"):
            r = self.p.parse(subject="【IS6FX】出金取消完了のお知らせ", body=_html_to_text(html),
                             from_address="noreply@is6.com")
            self.assertNotEqual(getattr(r, "event_type", None), "WITHDRAWAL_COMPLETED", html)

    def test_other_direction_completion_does_not_advance_this_kind(self):
        # A completed clause about the OTHER direction's transaction must not advance a request to *_COMPLETED
        # (kind-specific anchors). A withdrawal-request email that notes the funding deposit cleared stays REQUESTED.
        cases = [
            ("[IS6FX] Withdrawal request received",
             "<p>We have received your withdrawal request. Your deposit has been completed, so these funds are now "
             "eligible for withdrawal.</p><p>Withdrawal amount</p><p>500 (USD)</p>", "WITHDRAWAL_REQUESTED"),
            ("[IS6FX] Your withdrawal request has been received",
             "<p>We have received your withdrawal request. As your deposit has been completed, your funds will be "
             "sent within 1-3 business days.</p>", "WITHDRAWAL_REQUESTED"),
            ("[IS6FX] Your deposit request has been received",
             "<p>We have received your deposit request. Your withdrawal has been completed.</p>", "DEPOSIT_REQUESTED"),
            ("出金リクエスト受付のお知らせ",
             "<p>出金のリクエストを受け付けました。なお、ご入金が完了しました。</p><p>出金額</p><p>500 (USD)</p>", "WITHDRAWAL_REQUESTED"),
            ("【IS6FX】出金リクエストを受け付けました",
             "<p>出金リクエストを受け付けました。ご入金が完了しておりますので、通常1〜3営業日以内に処理されます。</p>", "WITHDRAWAL_REQUESTED"),
        ]
        for subj, html, expected in cases:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, expected, subj)

    def test_ambiguous_token_other_direction_completion_does_not_advance_kind(self):
        # An ambiguous token (payment/remittance/送金) carrying the OTHER direction's keyword as a modifier must not
        # flip the kind: "your DEPOSIT payment has been completed" in a withdrawal email stays REQUESTED.
        cases = [
            ("[IS6FX] Withdrawal request received",
             "<p>We have received your withdrawal request. Your deposit payment has been completed, so these funds "
             "are now eligible for withdrawal.</p><p>Withdrawal amount</p><p>500 (USD)</p>", "WITHDRAWAL_REQUESTED"),
            ("[IS6FX] Withdrawal request received",
             "<p>We received your withdrawal request. Your deposit payment has been rejected by our bank.</p>",
             "WITHDRAWAL_REQUESTED"),
            ("[IS6FX] Your deposit request has been received",
             "<p>We received your deposit request. Your withdrawal remittance has been completed yesterday.</p>",
             "DEPOSIT_REQUESTED"),
            ("出金リクエスト受付のお知らせ", "<p>出金のリクエストを受け付けました。入金送金が完了しました。</p>", "WITHDRAWAL_REQUESTED"),
        ]
        for subj, html, expected in cases:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, expected, subj)

    def test_japanese_negated_completion_is_not_completed(self):
        # 未完了 (incomplete) / 完了済みではありません / 完了しておりません are NOT completions.
        for html in ("<p>ご入金が未完了です。</p>", "<p>入金が未完了となっております。</p>",
                     "<p>ご入金はまだ完了済みではありません。</p>", "<p>ご入金はまだ完了しておりません。</p>"):
            r = self.p.parse(subject="【IS6FX】入金状況", body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertNotEqual(getattr(r, "event_type", None), "DEPOSIT_COMPLETED", html)
        # the affirmative 完了です / 完了しました still complete
        for html in ("<p>ご入金が完了です。</p>", "<p>入金が完了しました。</p>"):
            r = self.p.parse(subject="【IS6FX】入金", body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, "DEPOSIT_COMPLETED", html)

    def test_genuine_completion_with_confirm_verb_still_completes(self):
        # "We are pleased to confirm your deposit has been completed" — 'your deposit' is the subject of 'completed'
        # (NOT the object of a preposition), so it still classifies as completed.
        r = self.p.parse(subject="[IS6FX] deposit", from_address="noreply@is6.com",
                         body=_html_to_text("<p>We are pleased to confirm your deposit has been completed.</p>"))
        self.assertEqual(r.event_type, "DEPOSIT_COMPLETED")

    def test_japanese_genuine_completion_forms(self):
        # The transaction's OWN completion in several polite JP forms → DEPOSIT_COMPLETED.
        for body in ("<p>お客様の入金手続きが完了となりました。</p>", "<p>お客様の入金手続きが完了しております。</p>",
                     "<p>入金が完了しました。</p>"):
            r = self.p.parse(subject="【IS6FX】入金処理", body=_html_to_text(body), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, "DEPOSIT_COMPLETED", body)

    def test_english_second_subject_in_one_clause_is_not_completion(self):
        # "Your deposit request is pending and verification has been completed" — the completion belongs to
        # 'verification', not the deposit; the gap must not bridge the 'and' to a second subject.
        r = self.p.parse(subject="[IS6FX] deposit request received", from_address="noreply@is6.com",
                         body=_html_to_text("<p>Your deposit request is pending and verification has been "
                                            "completed</p>"))
        self.assertEqual(r.event_type, "DEPOSIT_REQUESTED")

    def test_japanese_deposit_completed_full_extraction(self):
        r = self.p.parse(subject="【IS6FX】入金処理完了のお知らせ（銀行送金）",
                         body=_html_to_text(_jp_deposit_body()), from_address="noreply@is6.com")
        self.assertEqual(r.event_type, "DEPOSIT_COMPLETED")
        self.assertEqual(r.transaction_category, TransactionCategory.DEPOSIT)
        self.assertEqual(r.amount, Decimal("150"))
        self.assertEqual(r.currency, "USD")
        self.assertEqual(r.fee, Decimal("0"))
        self.assertEqual(r.destination_account, PLACEHOLDER_ACCT)

    def test_thousands_grouped_amount(self):
        r = self._parse_en(amount="1,250.00 (USD)")
        self.assertEqual(r.amount, Decimal("1250.00"))
        self.assertEqual(r.currency, "USD")

    def test_absent_fee_is_none_not_zero(self):
        # No fee label at all → fee None (never fabricated); distinct from an explicit "0".
        body = ("<html><body><p>Your deposit has been completed.</p>"
                "<p>Deposit amount</p><p>150 (USD)</p>"
                f"<p>Account number</p><p>{PLACEHOLDER_ACCT}</p></body></html>")
        r = self.p.parse(subject="[IS6FX] deposit completed", body=_html_to_text(body), from_address="noreply@is6.com")
        self.assertIsNone(r.fee)
        self.assertEqual(r.fee_currency, "")

    # ---- lifecycle: completion is NEVER inferred from a request / CTA / future / negation (invariant C) ----
    def test_deposit_request_is_not_completed(self):
        r = self.p.parse(subject="[IS6FX] Your deposit request has been received",
                         body=_html_to_text("<p>We have received your deposit request.</p>"
                                            "<p>Deposit amount</p><p>150 (USD)</p>"),
                         from_address="noreply@is6.com")
        self.assertEqual(r.event_type, "DEPOSIT_REQUESTED")       # requested, NOT completed
        self.assertEqual(r.transaction_category, TransactionCategory.DEPOSIT)

    def test_call_to_action_to_complete_is_not_completed(self):
        # "To complete your deposit, please remit…" is a REQUEST/instruction, not a completion.
        r = self.p.parse(subject="[IS6FX] Your deposit request has been received",
                         body=_html_to_text("<p>We have received your deposit request. To complete your deposit, "
                                            "please remit the funds to the bank account below.</p>"),
                         from_address="noreply@is6.com")
        self.assertEqual(r.event_type, "DEPOSIT_REQUESTED")

    def test_future_tense_will_be_completed_is_not_completed(self):
        r = self.p.parse(subject="[IS6FX] Withdrawal request received",
                         body=_html_to_text("<p>Your withdrawal request has been received and will be completed "
                                            "within 24 hours.</p>"),
                         from_address="noreply@is6.com")
        self.assertEqual(r.event_type, "WITHDRAWAL_REQUESTED")    # future promise is NOT a completion

    def test_japanese_until_completion_is_not_completed(self):
        # 完了まで = "until completion" — a pending request, NOT a completed deposit.
        r = self.p.parse(subject="【IS6FX】入金リクエスト受付",
                         body=_html_to_text("<p>入金リクエストを受け付けました。入金完了までお待ちください。</p>"),
                         from_address="noreply@is6.com")
        self.assertEqual(r.event_type, "DEPOSIT_REQUESTED")

    def test_negation_not_completed_is_declined(self):
        r = self.p.parse(subject="[IS6FX] deposit", from_address="noreply@is6.com",
                         body=_html_to_text("<p>Your deposit has not been completed yet.</p>"))
        self.assertIsNone(r)                                      # "has not been completed" is never a completion

    def test_completed_email_with_reject_boilerplate_footer_stays_completed(self):
        # A genuinely completed deposit whose support footer mentions failure/"could not be" must NOT flip to REJECTED.
        for footer in ("If the transfer could not be credited, please contact support.",
                       "If your deposit failed, please contact us."):
            r = self.p.parse(subject="[IS6FX] Notification of completed deposit processing (bank transfer)",
                             body=_html_to_text(f"<p>Your payment procedure for your bank transfer has been "
                                                f"completed. {footer}</p>"),
                             from_address="noreply@is6.com")
            self.assertEqual(r.event_type, "DEPOSIT_COMPLETED", footer)

    def test_present_perfect_is_completed_still_parses(self):
        r = self.p.parse(subject="[IS6FX] deposit completed", from_address="noreply@is6.com",
                         body=_html_to_text("<p>Your deposit is completed.</p>"
                                            "<p>Deposit amount</p><p>150 (USD)</p>"))
        self.assertEqual(r.event_type, "DEPOSIT_COMPLETED")

    def test_habitual_rule_is_not_completion(self):
        # "Deposits are completed within 24 hours" is a general rule in a request email, NOT this deposit completing.
        r = self.p.parse(subject="[IS6FX] Your deposit request has been received", from_address="noreply@is6.com",
                         body=_html_to_text("<p>We have received your deposit request. Deposits are completed within "
                                            "24 hours.</p><p>Deposit amount</p><p>200 (USD)</p>"))
        self.assertEqual(r.event_type, "DEPOSIT_REQUESTED")

    def test_conditional_is_completed_once_is_not_completion(self):
        r = self.p.parse(subject="[IS6FX] Your deposit request has been received", from_address="noreply@is6.com",
                         body=_html_to_text("<p>Your deposit is completed once we receive the funds.</p>"))
        self.assertEqual(r.event_type, "DEPOSIT_REQUESTED")

    def test_generic_third_person_rule_is_not_this_completion(self):
        # Generic/habitual statements about deposits in general — singular OR plural subject — are NOT this deposit
        # completing. Only "your <txn> … completed" counts.
        for sentence in ("Deposits are completed within 24 hours.",
                         "Each deposit is completed after our bank confirms receipt of your funds.",
                         "Normally a deposit is completed in minutes.",
                         "This deposit is completed only after funds clear."):
            r = self.p.parse(subject="[IS6FX] Your deposit request has been received", from_address="noreply@is6.com",
                             body=_html_to_text(f"<p>We have received your deposit request. {sentence}</p>"
                                                "<p>Deposit amount</p><p>200 (USD)</p>"))
            self.assertEqual(r.event_type, "DEPOSIT_REQUESTED", sentence)

    def test_completion_with_trailing_timing_clause_still_completes(self):
        # A genuine completion whose own sentence also states a delivery time must stay COMPLETED (the perfect-tense
        # anchor excludes the future "will be completed", so "within" is safe to allow here).
        r = self.p.parse(subject="[IS6FX] Withdrawal completed", from_address="noreply@is6.com",
                         body=_html_to_text("<p>Your withdrawal has been completed and the funds will arrive within "
                                            "1 business day.</p><p>Withdrawal amount</p><p>150 (USD)</p>"))
        self.assertEqual(r.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(r.amount, Decimal("150"))

    def test_temporal_subordinator_is_not_completion(self):
        # "After/Before/Until your <txn> is completed, …" is a not-yet-happened condition, not a completion.
        cases = [
            ("[IS6FX] Your deposit request has been received",
             "<p>We have received your deposit request. After your deposit is completed, we will notify you.</p>"
             "<p>Deposit amount</p><p>200 (USD)</p>", "DEPOSIT_REQUESTED"),
            ("[IS6FX] Withdrawal request received",
             "<p>Your withdrawal request has been received. Before your withdrawal is completed, verify your identity.</p>",
             "WITHDRAWAL_REQUESTED"),
        ]
        for subj, html, expected in cases:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, expected, subj)
        self.assertIsNone(self.p.parse(subject="[IS6FX] deposit pending", from_address="noreply@is6.com",
                                       body=_html_to_text("<p>Until your deposit is completed, trading remains "
                                                          "disabled.</p>")))

    def test_subject_completion_phrase_is_not_a_sole_decider(self):
        # A "completed <txn> processing" phrase persists verbatim in a "Re:" reply / reversal subject while the body
        # says the opposite — it must NOT by itself force COMPLETED (completion is read from the body).
        for subj, html in [
            ("Re: [IS6FX] Notification of completed deposit processing (bank transfer)",
             "<p>We are sorry to inform you that this deposit was reversed by the bank and did not complete.</p>"),
            ("[IS6FX] Regarding your completed deposit processing - reversal notice",
             "<p>This deposit was reversed and the funds were returned. It did not complete.</p>"),
            ("[IS6FX] deposit status", "<p>We have not yet completed deposit processing for your request.</p>"
                                       "<p>Deposit amount</p><p>200 (USD)</p>"),
            ("[IS6FX] info", "<p>Completed deposit processing usually takes 24 hours.</p>"),
        ]:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertNotEqual(getattr(r, "event_type", None), "DEPOSIT_COMPLETED", html)
        # the genuine blob-3 completion is still read from its BODY (and kind=DEPOSIT from the subject keyword)
        r = self.p.parse(subject="[IS6FX] Notification of completed deposit processing (bank transfer)",
                         body=_html_to_text("<p>Your payment procedure for your bank transfer has been completed.</p>"),
                         from_address="noreply@is6.com")
        self.assertEqual(r.event_type, "DEPOSIT_COMPLETED")

    def test_quoted_reply_history_is_not_classified_as_live(self):
        # A support-thread reply whose LIVE body is pending must not be classified from a quoted earlier completion.
        for html in [
            "<p>We are investigating why your deposit has not yet reached your account.</p>"
            "<blockquote><p>&gt; Your deposit has been completed.</p></blockquote>",
            "<p>Your deposit is still pending.</p><p>On Mon, IS6 Support wrote:</p>"
            "<p>Your deposit has been completed.</p>",
        ]:
            r = self.p.parse(subject="Re: [IS6FX] deposit issue", body=_html_to_text(html),
                             from_address="noreply@is6.com")
            self.assertNotEqual(getattr(r, "event_type", None), "DEPOSIT_COMPLETED", html)

    def test_conditional_reject_footer_does_not_flip_completion(self):
        # A hypothetical "if ... was declined/rejected" support footer must NOT flip a real completion to rejected.
        for footer in ("If your deposit was declined for any reason, contact support.",
                       "If a previous withdrawal was rejected, please contact us."):
            r = self.p.parse(subject="[IS6FX] Notification of completed deposit processing",
                             body=_html_to_text(f"<p>Your deposit has been completed. {footer}</p>"),
                             from_address="noreply@is6.com")
            self.assertEqual(r.event_type, "DEPOSIT_COMPLETED", footer)
        # and a genuine, asserted rejection still classifies as rejected
        r = self.p.parse(subject="[IS6FX] deposit rejected", from_address="noreply@is6.com",
                         body=_html_to_text("<p>Your deposit has been rejected.</p>"))
        self.assertEqual(r.event_type, "DEPOSIT_REJECTED")

    # ---- extraction: prose never shadows the labelled detail row (invariant E) ----
    def test_prose_account_does_not_shadow_labelled_row(self):
        body = ("<html><body><p>Your withdrawal has been completed and sent to your bank account 12345678.</p>"
                "<p>Withdrawal amount</p><p>150 (USD)</p>"
                f"<p>Account number</p><p>{PLACEHOLDER_ACCT}</p><p>Fee</p><p>5 (USD)</p></body></html>")
        r = self.p.parse(subject="[IS6FX] Withdrawal completed", body=_html_to_text(body),
                         from_address="noreply@is6.com")
        self.assertEqual(r.source_account, PLACEHOLDER_ACCT)      # the LABELLED GuvFX account, NOT the prose "12345678"
        self.assertEqual(r.amount, Decimal("150"))

    def test_prose_amount_does_not_shadow_labelled_row(self):
        body = ("<html><body><p>Your withdrawal has been completed. Any bonus amount 500 is non-withdrawable.</p>"
                "<table><tr><td>Withdrawal amount</td><td>150 (USD)</td></tr></table></body></html>")
        r = self.p.parse(subject="[IS6FX] Withdrawal completed", body=_html_to_text(body),
                         from_address="noreply@is6.com")
        self.assertEqual(r.amount, Decimal("150"))               # the labelled 150, NOT the prose "500"

    def test_two_labelled_accounts_are_ambiguous_unresolved(self):
        body = ("<html><body><p>Your withdrawal has been completed.</p>"
                "<p>Account number</p><p>11112222</p><p>Account number</p><p>33334444</p>"
                "<p>Withdrawal amount</p><p>150 (USD)</p></body></html>")
        r = self.p.parse(subject="[IS6FX] Withdrawal completed", body=_html_to_text(body),
                         from_address="noreply@is6.com")
        self.assertEqual(r.source_account, "")                   # >1 distinct labelled account → '' (never guess)

    def test_european_decimal_comma(self):
        r = self._parse_en(amount="12,34 (EUR)")
        self.assertEqual(r.amount, Decimal("12.34"))
        self.assertEqual(r.currency, "EUR")

    def test_ambiguous_multi_separator_amount_fails_closed(self):
        # A European thousands+decimal "1.234,56" is not a cleanly supported shape → amount None (never a wrong value).
        r = self._parse_en(amount="1.234,56 (EUR)")
        self.assertIsNone(r.amount)
        self.assertEqual(r.currency, "")

    # ---- the deposit-never-withdrawal invariant ----
    def test_deposit_never_external_withdrawal(self):
        r = self._parse_en()
        self.assertNotEqual(r.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)
        self.assertEqual(r.transaction_category, TransactionCategory.DEPOSIT)

    def test_withdrawal_prose_never_classifies_external(self):
        # A genuine-looking withdrawal with NO internal evidence: lifecycle parses, category stays UNKNOWN — the
        # parser has no prose→EXTERNAL path, so "withdrawal" alone can never inflate the external-withdrawal metric.
        body = ("<html><body><p>Your withdrawal has been completed and sent to your bank account.</p>"
                "<p>Withdrawal amount</p><p>150 (USD)</p>"
                f"<p>Account number</p><p>{PLACEHOLDER_ACCT}</p><p>Fee</p><p>5 (USD)</p></body></html>")
        r = self.p.parse(subject="[IS6FX] Withdrawal completed", body=_html_to_text(body),
                         from_address="noreply@is6.com")
        self.assertEqual(r.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(r.transaction_category, TransactionCategory.UNKNOWN)   # NEVER external from prose
        self.assertEqual(r.source_account, PLACEHOLDER_ACCT)    # money LEAVES this account → source
        self.assertEqual(r.destination_account, "")
        self.assertEqual(r.fee, Decimal("5"))

    def test_internal_transfer_classification(self):
        # A withdrawal-context message carrying explicit internal-transfer evidence classifies INTERNAL_TRANSFER
        # (reachable only via a withdrawal/deposit lifecycle) — NEVER external.
        body = ("<html><body><p>Your withdrawal has been completed as an internal transfer between your accounts.</p>"
                "<p>Withdrawal amount</p><p>150 (USD)</p></body></html>")
        r = self.p.parse(subject="[IS6FX] Withdrawal completed", body=_html_to_text(body),
                         from_address="noreply@is6.com")
        self.assertEqual(r.event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(r.transaction_category, TransactionCategory.INTERNAL_TRANSFER)
        self.assertNotEqual(r.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)

    def test_bare_transfer_without_deposit_or_withdrawal_context_is_declined(self):
        # A pure "transfer" notice with NO deposit/withdrawal word carries no supported lifecycle → declined
        # (fail-closed): the parser never invents a lifecycle the vocabulary does not support.
        body = "<html><body><p>Your transfer has been completed.</p><p>Amount</p><p>150 (USD)</p></body></html>"
        self.assertIsNone(self.p.parse(subject="[IS6FX] Transfer completed", body=_html_to_text(body),
                                       from_address="noreply@is6.com"))

    # ---- fail-closed on ambiguous / unsupported / malformed ----
    def test_ambiguous_deposit_and_withdrawal_is_declined(self):
        r = self.p.parse(subject="[IS6FX] deposit and withdrawal summary",
                         body=_html_to_text("<p>Your deposit and withdrawal have been completed.</p>"),
                         from_address="noreply@is6.com")
        self.assertIsNone(r)                                 # ambiguous dominant context → no event (fail-closed)

    def test_empty_body_is_declined(self):
        self.assertIsNone(self.p.parse(subject="", body="", from_address="noreply@is6.com"))

    def test_deposit_word_without_lifecycle_verb_is_declined(self):
        r = self.p.parse(subject="[IS6FX] deposit information",
                         body=_html_to_text("<p>Information about deposits is available in your cabinet.</p>"),
                         from_address="noreply@is6.com")
        self.assertIsNone(r)                                 # a topic word but no lifecycle verb → not resolvable

    def test_non_positive_amount_is_dropped(self):
        r = self._parse_en(amount="0 (USD)")
        self.assertIsNone(r.amount)                          # a non-positive "amount" is not a real amount
        self.assertEqual(r.currency, "")

    # ---- sensitive material never emitted ----
    def test_no_url_or_token_in_any_emitted_field(self):
        body = ("<html><body><p>Your deposit has been completed. "
                '<a href="https://is6.com/c?token=SECRETTOKEN">details</a></p>'
                "<p>Deposit amount</p><p>150 (USD)</p>"
                f"<p>Account number</p><p>{PLACEHOLDER_ACCT}</p></body></html>")
        r = self.p.parse(subject="[IS6FX] deposit completed", body=_html_to_text(body), from_address="noreply@is6.com")
        for field in (r.broker_reference_id, r.currency, r.fee_currency, r.broker, r.event_type,
                      r.transaction_category, r.source_account, r.destination_account):
            self.assertFalse(redaction.contains_sensitive(field or ""),
                             f"emitted field leaked sensitive material: {field!r}")

    # ---- can_parse domain exactness + cross-broker isolation ----
    def test_can_parse_domain_exactness(self):
        self.assertTrue(self.p.can_parse(subject="deposit", body="x", from_address="x@is6.com"))
        self.assertTrue(self.p.can_parse(subject="deposit", body="x", from_address="x@mail.is6.com"))   # subdomain OK
        self.assertFalse(self.p.can_parse(subject="deposit", body="x", from_address="x@evilis6.com"))
        self.assertFalse(self.p.can_parse(subject="deposit", body="x", from_address="x@is6.com.evil.com"))
        self.assertFalse(self.p.can_parse(subject="hello", body="nothing relevant", from_address="x@is6.com"))  # no kw

    def test_can_parse_japanese_keyword(self):
        self.assertTrue(self.p.can_parse(subject="【IS6FX】入金処理完了", body="x", from_address="x@is6.com"))

    def test_parsers_do_not_claim_each_others_mail(self):
        tw = TradersWayParser()
        self.assertFalse(self.p.can_parse(subject="withdrawal", body="x", from_address="x@tradersway.com"))
        self.assertFalse(tw.can_parse(subject="deposit", body="x", from_address="x@is6.com"))


# The IS6 BrokerProfile (seeded by migration 0007) authenticates via DMARC pass; override settings so the test is
# self-contained and does not depend on seed/profile state.
@override_settings(BROKER_INTELLIGENCE_SENDER_ALLOWLIST="is6.com",
                   BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS="mx.google.com")
class IS6IngestionPipelineTests(TestCase):
    """End-to-end: authenticated IS6 HTML email → stored evidence → parsed append-only BrokerEvent → attribution,
    and the structural guarantee that a deposit never becomes a Withdrawal."""

    _AR_PASS = ("mx.google.com; dkim=pass header.i=@is6.com header.d=is6.com; "
                "spf=pass smtp.mailfrom=is6.com; dmarc=pass (p=REJECT) header.from=is6.com")

    def setUp(self):
        self._root = tempfile.mkdtemp(prefix="bi-is6-")
        self._ctx = override_settings(BROKER_INTELLIGENCE_EVIDENCE_ROOT=self._root)
        self._ctx.enable()
        self.store = EvidenceStore()
        parsers.clear_registry()
        register_default_parsers()                          # registers BOTH TradersWay and IS6
        self.addCleanup(parsers.clear_registry)
        self.u = User.objects.create_user(username="is6%s" % _uniq(), email="is6@x.invalid", password="x")
        # The identity's "home" account — the owner-scoped recipient lookup matches identities bound to the owner's
        # own accounts. Distinct from the credited account in the deposit email (PLACEHOLDER_ACCT).
        self.home = _acct(self.u, "70000001")
        BrokerEmailIdentity.objects.create(email="nrfda1111@googlemail.com", broker_name="IS6",
                                           user=self.u, trading_account=self.home,
                                           status=BrokerEmailIdentity.Status.VERIFIED)

    def tearDown(self):
        self._ctx.disable()
        shutil.rmtree(self._root, ignore_errors=True)

    def _raw(self, to_addr="nrfda1111@googlemail.com", frm="IS6FX <noreply@is6.com>",
             subject="[IS6FX] Notification of completed deposit processing (bank transfer)",
             html=None, ar=None):
        html = html if html is not None else _en_deposit_body()
        ar = ar if ar is not None else self._AR_PASS
        return (
            f"From: {frm}\r\nTo: {to_addr}\r\nSubject: {subject}\r\n"
            f"Authentication-Results: {ar}\r\n"
            "Date: Sun, 12 Oct 2026 06:13:00 +0000\r\n"
            'Content-Type: multipart/related; boundary="BX"\r\n\r\n'
            "--BX\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + html + "\r\n--BX--\r\n"
        ).encode("utf-8")

    def _mailmessage(self, raw: bytes) -> MailMessage:
        f = parse_rfc822(raw)
        return MailMessage(raw_bytes=raw, to_addresses=f["to_addresses"], from_address=f["from_address"],
                           subject=f["subject"], body=f["body"], received_at=f["received_at"],
                           auth_verdict=f["auth_verdict"])

    def _ingest(self, raw=None):
        return ingestion.ingest_message(self._mailmessage(raw or self._raw()), store=self.store,
                                        provenance=Provenance.REAL, owner_user=self.u)

    def test_authenticated_deposit_is_parsed_and_stored(self):
        ev = self._ingest()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.provenance, Provenance.REAL)
        self.assertEqual(ev.event_type, "DEPOSIT_COMPLETED")
        self.assertEqual(ev.transaction_category, TransactionCategory.DEPOSIT)
        self.assertEqual(ev.broker, "IS6")
        self.assertEqual(ev.amount, Decimal("150"))
        self.assertEqual(ev.currency, "USD")
        self.assertEqual(ev.fee, Decimal("0"))
        self.assertEqual(ev.fee_currency, "USD")
        self.assertEqual(ev.destination_account, PLACEHOLDER_ACCT)
        self.assertEqual(ev.owner_id, self.u.id)

    def test_deposit_never_creates_a_withdrawal(self):
        from broker_intelligence.correlation import run_correlation
        self._ingest()
        run_correlation()
        self.assertEqual(Withdrawal.objects.count(), 0)     # a deposit never enters withdrawal metrics

    def test_attribution_resolves_when_deposit_account_matches(self):
        matched = _acct(self.u, PLACEHOLDER_ACCT)           # the member holds the credited account in GuvFX
        ev = self._ingest()
        self.assertEqual(ev.trading_account_id, matched.id)
        self.assertEqual(ev.owner_id, self.u.id)

    def test_attribution_unresolved_when_account_does_not_match(self):
        _acct(self.u, "10101010")                           # a different GuvFX account — not the credited one
        ev = self._ingest()
        self.assertIsNone(ev.trading_account_id)             # never bound via the recipient email alone
        self.assertEqual(ev.owner_id, self.u.id)

    def test_unauthenticated_deposit_is_quarantined_no_event(self):
        raw = self._raw(ar="mx.google.com; dkim=fail; spf=pass; dmarc=fail")   # spoofed broker domain
        ev = self._ingest(raw)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 1)    # evidence retained (quarantine)
        self.assertEqual(BrokerEvent.objects.count(), 0)

    def test_idempotent_reingest(self):
        e1 = self._ingest()
        e2 = self._ingest()
        self.assertEqual(e1.id, e2.id)
        self.assertEqual(BrokerEvent.objects.count(), 1)

    def test_is6_parser_selected_for_is6_mail(self):
        ev = self._ingest()
        self.assertEqual(ev.parser_name, "is6")
        self.assertEqual(ev.parser_version, "v1")

    def test_japanese_deposit_pipeline(self):
        raw = self._raw(subject="【IS6FX】入金処理完了のお知らせ（銀行送金）", html=_jp_deposit_body())
        ev = self._ingest(raw)
        self.assertEqual(ev.event_type, "DEPOSIT_COMPLETED")
        self.assertEqual(ev.transaction_category, TransactionCategory.DEPOSIT)
        self.assertEqual(ev.amount, Decimal("150"))

    # --- §5 adversarial authentication regression (PR3): aligned DKIM / DMARC / forged headers / wrong domains /
    #     cross-user / ambiguous accounts. Exercises the REAL verdict path (gmail_source._auth_verdict, RFC 8601
    #     authserv-id trust) + the exact-domain allowlist + owner-scoped attribution. ---
    def test_aligned_dkim_pass_without_dmarc_is_accepted(self):
        # DMARC_OR_ALIGNED_DKIM: a From-aligned DKIM pass authenticates even with NO dmarc result present.
        raw = self._raw(ar="mx.google.com; dkim=pass header.d=is6.com; spf=pass smtp.mailfrom=is6.com")
        ev = self._ingest(raw)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "DEPOSIT_COMPLETED")

    def test_dmarc_pass_without_dkim_is_accepted(self):
        raw = self._raw(ar="mx.google.com; spf=pass smtp.mailfrom=is6.com; dmarc=pass (p=REJECT) header.from=is6.com")
        self.assertIsNotNone(self._ingest(raw))

    def test_forged_foreign_authserv_id_is_ignored_no_event(self):
        # An attacker-injected A-R bearing a FOREIGN authserv-id (not the trusted receiver mx.google.com) is ignored
        # (RFC 8601 — the receiver strips forgeries of its own id); no trusted pass ⇒ no event, evidence quarantined.
        raw = self._raw(ar="evil.attacker.example; dkim=pass header.d=is6.com; dmarc=pass (p=REJECT) header.from=is6.com")
        ev = self._ingest(raw)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 1)

    def test_unaligned_dkim_without_dmarc_is_not_accepted(self):
        # DKIM passes but signs a DIFFERENT domain than From, and no DMARC ⇒ not From-aligned ⇒ no pass ⇒ no event.
        raw = self._raw(ar="mx.google.com; dkim=pass header.d=mailer.example; spf=pass smtp.mailfrom=bounce.example")
        self.assertIsNone(self._ingest(raw))

    def test_incorrect_broker_from_domain_is_dropped_before_storage(self):
        # A look-alike/phish From domain is NOT on the exact-domain allowlist ⇒ the retention gate drops REAL mail
        # BEFORE any storage (no evidence, no event), regardless of a well-formed A-R. Never a substring match.
        raw = self._raw(frm="IS6FX <noreply@is6-phish.com>")
        ev = self._ingest(raw)
        self.assertIsNone(ev)
        self.assertEqual(EvidenceBlob.objects.count(), 0)

    def test_cross_user_recipient_is_not_attributed(self):
        # The SAME authenticated IS6 mail ingested through a DIFFERENT member's mailbox (owner_user=other). The event
        # is correctly attributed to the MAILBOX owner who actually received it (other) — that is faithful, not a leak:
        # the bytes are in other's own inbox. The cross-user FIREWALL is preserved where it matters: the resolution is
        # scoped to `other` (never self.u, the identity's real owner), so NO trading account is cross-attributed and
        # the event never enters self.u's withdrawal projection (correlation skips a null trading_account).
        other = User.objects.create_user(username="other%s" % _uniq(), email="other@x.invalid", password="x")
        ev = ingestion.ingest_message(self._mailmessage(self._raw()), store=self.store,
                                      provenance=Provenance.REAL, owner_user=other)
        self.assertIsNotNone(ev)                            # authenticated + parseable ⇒ event created (evidence)
        self.assertEqual(ev.owner_id, other.id)             # attributed to the MAILBOX owner (owner_key), per #511
        self.assertIsNone(ev.trading_account_id)            # the firewall: NO cross-user account attribution

    def test_ambiguous_credited_account_is_unresolved(self):
        # Owner holds TWO is6-broker accounts sharing the credited number ⇒ ambiguous ⇒ fail closed (no binding),
        # though the owner is still known via the verified identity.
        _acct(self.u, PLACEHOLDER_ACCT, broker="IS6FX")
        _acct(self.u, PLACEHOLDER_ACCT, broker="IS6 Technologies")
        ev = self._ingest()
        self.assertEqual(ev.owner_id, self.u.id)
        self.assertIsNone(ev.trading_account_id)


class IS6LifecycleCorrectionTests(TestCase):
    """PR-B.1 — corrects the lifecycle defect the genuine blob-3 certification caught: the real completion is a
    DEFINITE money-operation subject with prepositional ownership ("the payment procedure for your bank transfer has
    been completed"), and routine footer wording ("if you received this email by mistake") must never be read as a
    request. Uses PLACEHOLDER account/amount — the genuine values stay in the immutable production evidence."""

    def setUp(self):
        self.p = IS6Parser()

    def _parse(self, subject, html):
        return self.p.parse(subject=subject, body=_html_to_text(html), from_address="noreply@is6.com")

    # ---- the genuine blob-3 construction (definite subject + "for your" ownership) ----
    def test_genuine_definite_subject_completion(self):
        # Full genuine-shaped deposit (placeholder account/amount) incl. the real footer — the completion is read
        # from "the payment procedure for your bank transfer has been completed".
        r = self._parse(
            "[IS6FX] Notification of completed deposit processing (bank transfer)",
            "<p>Hi Nuno,</p><p>Thank you for your continued loyalty to IS6FX. We would like to inform you that the "
            "payment procedure for your bank transfer has been completed. To check the reflection of your deposit, "
            "please log in to your MT5 account.</p>"
            "<table><tr><td>Account number</td><td>99887766</td></tr>"
            "<tr><td>Deposit amount</td><td>200 (USD)</td></tr><tr><td>Fee</td><td>0 (USD)</td></tr></table>"
            "<p>If you received this email by mistake, please immediately notify the sender and delete the email.</p>")
        self.assertEqual(r.event_type, "DEPOSIT_COMPLETED")
        self.assertEqual(r.transaction_category, TransactionCategory.DEPOSIT)
        self.assertEqual(r.amount, Decimal("200"))
        self.assertEqual(r.currency, "USD")
        self.assertEqual(r.fee, Decimal("0"))
        self.assertEqual(r.fee_currency, "USD")
        self.assertEqual(r.destination_account, "99887766")
        self.assertEqual(r.source_account, "")
        self.assertEqual(r.broker, "IS6")
        self.assertIsNone(r.occurred_at)

    def test_genuine_definite_completion_sentence_alone(self):
        r = self._parse("[IS6FX] deposit", "<p>The payment procedure for your bank transfer has been completed.</p>")
        self.assertEqual(r.event_type, "DEPOSIT_COMPLETED")

    # ---- footer boilerplate must never be a request (anchored to a real transaction request) ----
    def test_footer_received_is_not_a_request(self):
        # Footer alone, no transaction request -> declined, NOT *_REQUESTED.
        r = self._parse("[IS6FX] notice",
                        "<p>Your deposit is pending.</p>"
                        "<p>If you received this email by mistake, please delete it.</p>")
        self.assertNotIn(getattr(r, "event_type", ""), ("DEPOSIT_REQUESTED", "WITHDRAWAL_REQUESTED"))

    def test_genuine_completion_with_footer_stays_completed(self):
        r = self._parse("[IS6FX] deposit",
                        "<p>The payment procedure for your bank transfer has been completed.</p>"
                        "<p>If you received this email by mistake, please delete it.</p>")
        self.assertEqual(r.event_type, "DEPOSIT_COMPLETED")

    def test_genuine_transaction_request_still_requested(self):
        for subj, html, expect in [
            ("[IS6FX] Your deposit request has been received", "<p>We have received your deposit request.</p>",
             "DEPOSIT_REQUESTED"),
            ("[IS6FX] Withdrawal request received", "<p>Your withdrawal request has been received.</p>",
             "WITHDRAWAL_REQUESTED"),
            ("【IS6FX】入金リクエスト受付", "<p>入金リクエストを受け付けました。</p>", "DEPOSIT_REQUESTED"),
            ("【IS6FX】出金", "<p>出金のお申し込みを受け付けました。</p>", "WITHDRAWAL_REQUESTED"),
        ]:
            r = self.p.parse(subject=subj, body=_html_to_text(html), from_address="noreply@is6.com")
            self.assertEqual(r.event_type, expect, subj)

    # ---- administrative sub-step completion is NOT the money event (preserved) ----
    def test_administrative_substep_completion_not_money(self):
        for html in [
            "<p>The review of your withdrawal has been completed.</p>",
            "<p>Verification of your deposit has been completed.</p>",
            "<p>Confirmation of your withdrawal has been completed.</p>",
            "<p>Registration of your withdrawal bank account has been completed.</p>",
            "<p>Your card authentication for this deposit has been completed.</p>",
            "<p>Your fee payment has been completed.</p>",
        ]:
            r = self._parse("[IS6FX] update", html)
            self.assertNotIn(getattr(r, "event_type", ""), ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED"), html)

    # ---- a generic rule with no member ownership is not a completion (preserved) ----
    def test_generic_rule_without_your_ownership_not_completed(self):
        for html in ["<p>A deposit is completed within 24 hours.</p>",
                     "<p>Each deposit is completed after our bank confirms.</p>",
                     "<p>This deposit is completed only once funds clear.</p>",
                     "<p>Deposits are completed within 24 hours.</p>"]:
            r = self._parse("[IS6FX] info", html)
            self.assertNotIn(getattr(r, "event_type", ""), ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED"), html)

    def test_definite_completion_requires_member_ownership(self):
        # Subject carries the deposit keyword so direction resolves (as in the genuine blob 3). With NO "your"
        # anywhere, the definite subject is not a member completion -> fail-closed (not *_COMPLETED).
        self.assertNotIn(getattr(self._parse("[IS6FX] deposit processing",
                                             "<p>The payment procedure has been completed.</p>"), "event_type", ""),
                         ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED"))
        # the same sentence WITH member ownership -> completed
        self.assertEqual(self._parse("[IS6FX] deposit processing",
                                     "<p>The payment procedure for your bank transfer has been completed.</p>").event_type,
                         "DEPOSIT_COMPLETED")

    # ---- deposit never becomes an external withdrawal (safety invariant) ----
    def test_deposit_never_external_withdrawal(self):
        r = self._parse("[IS6FX] Notification of completed deposit processing (bank transfer)",
                        "<p>The payment procedure for your bank transfer has been completed.</p>"
                        "<p>Deposit amount</p><p>200 (USD)</p>")
        self.assertEqual(r.transaction_category, TransactionCategory.DEPOSIT)
        self.assertNotEqual(r.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)

    def test_no_prose_external_withdrawal(self):
        # Even strongly "external"-sounding withdrawal prose never yields EXTERNAL_WITHDRAWAL (no prose->EXTERNAL path).
        r = self._parse("[IS6FX] Withdrawal completed",
                        "<p>Your withdrawal has been completed and the funds were sent out to your external bank "
                        "account.</p><p>Withdrawal amount</p><p>150 (USD)</p>")
        self.assertEqual(r.event_type, "WITHDRAWAL_COMPLETED")
        self.assertNotEqual(r.transaction_category, TransactionCategory.EXTERNAL_WITHDRAWAL)

    # ---- malformed / ambiguous HTML -> fail-closed ----
    def test_malformed_and_ambiguous(self):
        self.assertIsNone(self._parse("[IS6FX]", "<p><<</p>&nbsp;<div>"))
        self.assertIsNone(self._parse("[IS6FX] deposit and withdrawal",
                                      "<p>Your deposit and withdrawal have been completed.</p>"))
        self.assertIsNone(self._parse("[IS6FX] deposit", ""))


class IS6LifecycleReviewHardeningTests(TestCase):
    """PR-B.1 adversarial-review hardening: the definite-subject completion branch must accept the genuine
    construction but NEVER a present-tense SLA/policy rule, a cross-kind funding reassurance, or a fee-payment
    sub-step, even when an incidental "your" appears in the clause."""

    def setUp(self):
        self.p = IS6Parser()

    def _parse(self, subject, html):
        return self.p.parse(subject=subject, body=_html_to_text(html), from_address="noreply@is6.com")

    def _not_completed(self, subject, html):
        r = self._parse(subject, html)
        self.assertNotIn(getattr(r, "event_type", ""), ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED"), html)

    def test_present_tense_sla_line_is_not_completion(self):
        # "A deposit IS completed … to your account within 24 hours" in a request ack — present-tense habitual rule,
        # money not moved. The clause-local "your account" must NOT make it a completion.
        self._not_completed(
            "[IS6FX] We have received your deposit request",
            "<p>We have received your deposit request and it is now pending bank confirmation.</p>"
            "<p>A deposit is completed and credited to your account within 24 hours.</p>"
            "<p>Deposit amount</p><p>200 (USD)</p>")
        self._not_completed(
            "[IS6FX] Your withdrawal request has been received",
            "<p>We have received your withdrawal request. A transfer of funds to your bank is completed within "
            "3 business days.</p><p>Withdrawal amount</p><p>500 (USD)</p>")

    def test_cross_kind_funding_reassurance_is_not_completion(self):
        # A withdrawal-request email noting the funding deposit completed must not become WITHDRAWAL_COMPLETED.
        self._not_completed(
            "[IS6FX] Your withdrawal request has been received",
            "<p>We have received your withdrawal request. The deposit payment for your account has been completed.</p>")

    def test_fee_payment_substep_is_not_completion(self):
        # "The payment OF your deposit fee has been completed" is a fee charge, not the deposit completing.
        self._not_completed("[IS6FX] deposit", "<p>The payment of your deposit fee has been completed.</p>")

    def test_genuine_definite_completion_accepted(self):
        # The genuine blob-3 construction (and a close structural variant) must classify DEPOSIT_COMPLETED.
        for html in [
            "<p>We would like to inform you that the payment procedure for your bank transfer has been completed.</p>",
            "<p>The payment procedure for your bank transfer has been completed.</p>",
        ]:
            r = self._parse("[IS6FX] Notification of completed deposit processing (bank transfer)", html)
            self.assertEqual(r.event_type, "DEPOSIT_COMPLETED", html)

    def test_request_for_a_transaction_is_requested(self):
        r = self._parse("[IS6FX] withdrawal", "<p>We have received a request for a withdrawal.</p>")
        self.assertEqual(r.event_type, "WITHDRAWAL_REQUESTED")


class IS6DefiniteBranchHardeningTests(TestCase):
    """PR-B.1 re-review: the definite-completion branch must have EN/JP parity on semantic inversion, tolerate an
    affirmative adverb in the verb phrase, and request-detection must ignore imperative/marketing CTAs."""

    def setUp(self):
        self.p = IS6Parser()

    def _parse(self, subject, html):
        return self.p.parse(subject=subject, body=_html_to_text(html), from_address="noreply@is6.com")

    def test_inversion_of_money_op_is_not_completion(self):
        # A cancellation/reversal/refund/chargeback/failure of a money op => money did NOT move (EN/JP parity).
        for html in [
            "<p>The payment cancellation for your withdrawal has been completed; no funds have left your account.</p>",
            "<p>The payment reversal for your deposit has been completed and the funds were returned to your card.</p>",
            "<p>The payment chargeback for your deposit has been completed.</p>",
            "<p>The payment refund for your deposit has been completed.</p>",
            "<p>The transfer reversal for your deposit has been completed.</p>",
            "<p>The payment failure for your deposit has been completed and will be retried.</p>",
        ]:
            r = self._parse("[IS6FX] notice", html)
            self.assertNotIn(getattr(r, "event_type", ""), ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED"), html)

    def test_affirmative_adverb_completion_still_completes(self):
        self.assertEqual(self._parse("[IS6FX] Notification of completed deposit processing (bank transfer)",
                                     "<p>The payment procedure for your bank transfer has been successfully "
                                     "completed.</p>").event_type, "DEPOSIT_COMPLETED")
        self.assertEqual(self._parse("[IS6FX] Withdrawal completed",
                                     "<p>Your withdrawal has been successfully completed.</p>").event_type,
                         "WITHDRAWAL_COMPLETED")
        self.assertEqual(self._parse("[IS6FX] deposit",
                                     "<p>Your deposit has been fully completed.</p>").event_type, "DEPOSIT_COMPLETED")

    def test_imperative_cta_is_not_a_request(self):
        for subj, html in [
            ("[IS6FX] Promotion", "<p>Deposit bonus! Request a deposit of 1000 USD or more to qualify.</p>"),
            ("[IS6FX] October account statement",
             "<p>Your monthly statement is ready. You can request a withdrawal at any time from your dashboard.</p>"),
        ]:
            r = self._parse(subj, html)
            self.assertNotIn(getattr(r, "event_type", ""), ("DEPOSIT_REQUESTED", "WITHDRAWAL_REQUESTED"), html)

    def test_member_request_forms_still_requested(self):
        self.assertEqual(self._parse("[IS6FX] Your deposit request has been received",
                                     "<p>We have received your deposit request.</p>").event_type, "DEPOSIT_REQUESTED")
        self.assertEqual(self._parse("[IS6FX] withdrawal",
                                     "<p>We have received a request for a withdrawal.</p>").event_type,
                         "WITHDRAWAL_REQUESTED")


class IS6GapNegationHardeningTests(TestCase):
    """PR-B.1 round-3: the possessive-branch gap must not bridge a negating/diminishing adverb before the two-token
    'has been' verb; the definite branch must tolerate an affirmative adverb before 'been'; a member past-indicative
    request is recognised while an imperative CTA is not."""

    def setUp(self):
        self.p = IS6Parser()

    def _parse(self, subject, html):
        return self.p.parse(subject=subject, body=_html_to_text(html), from_address="noreply@is6.com")

    def test_negating_diminishing_adverb_is_not_completion_or_rejection(self):
        for html in [
            "<p>Your deposit has never been completed.</p>",
            "<p>Our records show your withdrawal has never been completed. Kindly contact support.</p>",
            "<p>Your deposit has rarely been completed.</p>",
            "<p>Your deposit has nearly been completed.</p>",
            "<p>Your deposit has almost been completed.</p>",
            "<p>Your withdrawal has partially been completed.</p>",
            "<p>Your withdrawal has almost been completed and the remaining amount will follow shortly.</p>"
            "<p>Withdrawal amount</p><p>500 (USD)</p>",
            "<p>Your deposit has never been rejected.</p>",
            "<p>Your withdrawal has never been declined.</p>",
        ]:
            r = self._parse("[IS6FX] status", html)
            self.assertNotIn(getattr(r, "event_type", ""),
                             ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED", "DEPOSIT_REJECTED", "WITHDRAWAL_REJECTED"),
                             html)

    def test_definite_affirmative_adverb_before_been_completes(self):
        for html in ["<p>The payment procedure for your bank transfer has now been completed.</p>",
                     "<p>The payment procedure for your bank transfer has already been completed.</p>"]:
            self.assertEqual(self._parse("[IS6FX] Notification of completed deposit processing (bank transfer)",
                                         html).event_type, "DEPOSIT_COMPLETED", html)

    def test_member_past_indicative_request_vs_imperative_cta(self):
        self.assertEqual(self._parse("[IS6FX] withdrawal",
                                     "<p>You have requested a withdrawal of funds.</p>").event_type,
                         "WITHDRAWAL_REQUESTED")
        r = self._parse("[IS6FX] Promotion", "<p>Request a deposit of 1000 USD to qualify.</p>")
        self.assertNotIn(getattr(r, "event_type", ""), ("DEPOSIT_REQUESTED", "WITHDRAWAL_REQUESTED"))


class IS6PaymentObjectHardeningTests(TestCase):
    """PR-B.1 round-4: the possessive gap must not bridge a payment OF/FOR a sub-step/inversion object, and must
    exclude diminisher synonyms — both pre-existing possessive-branch holes."""

    def setUp(self):
        self.p = IS6Parser()

    def _parse(self, subject, html):
        return self.p.parse(subject=subject, body=_html_to_text(html), from_address="noreply@is6.com")

    def _not_fin(self, subject, html):
        r = self._parse(subject, html)
        self.assertNotIn(getattr(r, "event_type", ""),
                         ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED", "DEPOSIT_REJECTED", "WITHDRAWAL_REJECTED"), html)

    def test_payment_of_substep_or_inversion_is_not_the_transaction(self):
        self._not_fin("[IS6FX] Withdrawal fee settled",
                      "<p>Your payment of the withdrawal fee has been completed.</p>"
                      "<p>Your withdrawal is now being processed.</p><p>Withdrawal amount</p><p>500 (USD)</p>")
        self._not_fin("[IS6FX] deposit", "<p>Your payment of the fee has been completed.</p>")
        self._not_fin("[IS6FX] withdrawal", "<p>Your payment of the withdrawal fee has been rejected.</p>")
        self._not_fin("[IS6FX] withdrawal", "<p>Your payment of the withdrawal cancellation has been completed.</p>")
        self._not_fin("[IS6FX] deposit", "<p>Your transfer of the deposit fee has been completed.</p>")
        self._not_fin("[IS6FX] deposit", "<p>Your payment of the identity verification has been completed.</p>")

    def test_diminisher_synonyms_not_completion(self):
        for html in ["<p>Our records show your withdrawal has virtually been completed; the final step is in "
                     "progress.</p>",
                     "<p>Your deposit has practically been completed.</p>",
                     "<p>Your deposit has essentially been completed.</p>"]:
            self._not_fin("[IS6FX] status update", html)

    def test_genuine_of_amount_and_destination_still_complete(self):
        # "of 500 USD" (amount) and "to your account" (destination) are NOT sub-step objects — must stay COMPLETED.
        self.assertEqual(self._parse("[IS6FX] Withdrawal",
                                     "<p>Your withdrawal of 500 USD to your account 99887766 has been completed.</p>"
                                     ).event_type, "WITHDRAWAL_COMPLETED")
        self.assertEqual(self._parse("[IS6FX] Withdrawal",
                                     "<p>Your withdrawal to your bank account has been completed.</p>").event_type,
                         "WITHDRAWAL_COMPLETED")
        self.assertEqual(self._parse("[IS6FX] deposit",
                                     "<p>Your deposit via this method has been completed.</p>").event_type,
                         "DEPOSIT_COMPLETED")


class IS6ObjectGuardGeneralisedTests(TestCase):
    """PR-B.1 round-5: the possessive object guard must reject a sub-step/charge/inversion or OTHER-direction object
    of the money head under ANY preposition and a wider window, while keeping instrument/destination/amount objects
    genuine."""

    def setUp(self):
        self.p = IS6Parser()

    def _parse(self, subject, html):
        return self.p.parse(subject=subject, body=_html_to_text(html), from_address="noreply@is6.com")

    def _not_fin(self, subject, html):
        r = self._parse(subject, html)
        self.assertNotIn(getattr(r, "event_type", ""),
                         ("DEPOSIT_COMPLETED", "WITHDRAWAL_COMPLETED", "DEPOSIT_REJECTED", "WITHDRAWAL_REJECTED"), html)

    def test_substep_object_under_any_preposition_or_window_is_not_completion(self):
        self._not_fin("[IS6FX] Your withdrawal request has been received",
                      "<p>We have received your withdrawal request. Your payment of the overseas withdrawal "
                      "processing fee has been completed, and your withdrawal is now being processed.</p>"
                      "<p>Withdrawal amount</p><p>500 (USD)</p>")
        self._not_fin("[IS6FX] Your deposit request has been received",
                      "<p>Your payment via bank card for the fee has been completed; your deposit will reflect "
                      "shortly.</p>")
        self._not_fin("[IS6FX] deposit", "<p>Your payment via the identity verification step has been completed.</p>")
        self._not_fin("[IS6FX] deposit", "<p>Your deposit by the service fee transaction has been completed.</p>")

    def test_other_direction_object_after_head_is_not_this_completion(self):
        self._not_fin("[IS6FX] Your withdrawal request has been received",
                      "<p>We have received your withdrawal request. Your payment for the originating deposit has "
                      "been completed.</p>")

    def test_instrument_destination_amount_objects_still_complete(self):
        for subj, html, expect in [
            ("[IS6FX] Withdrawal", "<p>Your withdrawal of 500 USD to your account 99887766 has been completed.</p>",
             "WITHDRAWAL_COMPLETED"),
            ("[IS6FX] Withdrawal", "<p>Your withdrawal to your bank account has been completed.</p>",
             "WITHDRAWAL_COMPLETED"),
            ("[IS6FX] deposit", "<p>Your deposit via this payment method has been completed.</p>", "DEPOSIT_COMPLETED"),
        ]:
            self.assertEqual(self._parse(subj, html).event_type, expect, html)
