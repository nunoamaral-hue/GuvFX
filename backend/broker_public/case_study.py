"""Genuine TradersWay withdrawal — PRIVATE evidence case study payload (WV1 §2). DARK.

Returns ONLY the verified, REDACTED facts of the single genuine pilot withdrawal (the real evidence lives immutably
in the production evidence store; this module does not read it — it reproduces reviewed, redacted facts). It NEVER
contains a raw email, account number, confirmation link, wallet address or evidence pointer, and NEVER a member
identity. Served only behind ``broker_public_case_study_enabled`` (default OFF) so the facts are not even shipped in
the client bundle until armed in an isolated demo. Pure constant; no DB, no I/O, no member data.
"""
from __future__ import annotations


def case_study_payload() -> dict:
    return {
        "real": True,
        "authenticated_on_production": True,
        "title": {"en": "Genuine Withdrawal — Private Case Study", "ja": "実際の出金 — 非公開ケーススタディ"},
        "badge": {"en": "REAL · AUTHENTICATED ON PRODUCTION", "ja": "実データ · 本番で認証済み"},
        "lede": {
            "en": ("This is the single genuine pilot withdrawal GuvFX observed from authenticated broker emails. It "
                   "is real evidence held immutably on production — not synthetic demo data, and not part of the "
                   "dashboard or WTM aggregate figures. All identifying details are redacted."),
            "ja": ("これは、GuvFXが認証済みブローカーメールから観測した唯一の実際のパイロット出金です。本番に不変で保持される実データで"
                   "あり、合成デモデータでも、ダッシュボードやWTM集計の数値の一部でもありません。識別情報はすべて秘匿されています。"),
        },
        "facts": [
            {"k": {"en": "Broker", "ja": "ブローカー"}, "v": "TradersWay"},
            {"k": {"en": "Type", "ja": "種別"}, "v": "External withdrawal"},
            {"k": {"en": "External method", "ja": "外部手段"}, "v": "Ethereum"},
            {"k": {"en": "Amount", "ja": "金額"}, "v": "24.12 USD"},
            {"k": {"en": "Request observed", "ja": "申請の観測"}, "v": "2026-10-19 07:55:04 UTC"},
            {"k": {"en": "Completion observed", "ja": "完了の観測"}, "v": "2026-10-19 08:28:05 UTC"},
            {"k": {"en": "Email-to-email interval", "ja": "メール間隔"}, "v": "33m01s (1981s)"},
            {"k": {"en": "Notifications", "ja": "通知"},
             "v": "Request + completion emails, linked by one broker reference"},
        ],
        "authentication": {
            "en": ["Sender authenticated: DKIM pass + SPF pass, from the broker's registrable domain.",
                   "Content-hash verified on both messages (immutable evidence; re-read reproduces the same bytes).",
                   "Broker-reported completion only — wallet/funds receipt is NOT independently certified by GuvFX."],
            "ja": ["送信者認証: DKIM合格 + SPF合格、ブローカーの登録ドメインから。",
                   "両メッセージで内容ハッシュを検証（不変の証跡。再読込で同一バイト列を再現）。",
                   "ブローカー報告の完了のみ — ウォレット/着金はGuvFXが独立に証明していません。"],
        },
        "attribution": {
            "en": ["Owner-attributed to the member whose connected mailbox received the broker emails.",
                   "Account UNRESOLVED: the TradersWay account the withdrawal was made from is not yet registered in "
                   "GuvFX — so it is surfaced as owner-attributed / account-unresolved, never bound to an unrelated "
                   "account."],
            "ja": ["ブローカーメールを受信した接続メールボックスの会員に帰属。",
                   "口座は未特定: 出金元のTradersWay口座はGuvFXに未登録のため、所有者帰属/口座未特定として表示し、無関係な口座に"
                   "は決して紐付けません。"],
        },
        "limits": {
            "en": ["No claim that GuvFX initiated, confirmed, or cancelled the withdrawal — GuvFX never does.",
                   "No claim of independent wallet/funds receipt.",
                   "This single real case is not used in any public broker ranking or aggregate."],
            "ja": ["GuvFXが出金を開始・承認・取消したという主張はありません — GuvFXは決して行いません。",
                   "ウォレット/着金の独立確認の主張はありません。",
                   "この唯一の実ケースは公開ブローカー評価や集計には一切使用しません。"],
        },
        "redaction": {
            "en": "No raw email, account number, confirmation link, wallet address or evidence pointer is shown.",
            "ja": "生メール、口座番号、確認リンク、ウォレットアドレス、証跡ポインタは一切表示していません。",
        },
    }
