"use client";

import Link from "next/link";
import { useState } from "react";
import { detectLang, type Lang } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";

/**
 * Public Privacy Policy (/privacy) — accessible without authentication.
 *
 * Content is grounded in the actual Withdrawal Intelligence implementation:
 *   - Gmail OAuth requests ONLY https://www.googleapis.com/auth/gmail.readonly (gmail_oauth.py)
 *   - OAuth tokens are Fernet-encrypted off-DB; the application DB holds only an opaque reference
 *     (credential_store.py); tokens are never logged.
 *   - Broker/withdrawal events are extracted deterministically (no human reads email, no LLM).
 *   - Owner-scoped: a member only ever sees their own data.
 *
 * DRAFT: the operator / data-controller identity (Virtus Logic UK Ltd, company no. 13318959, its
 * registered office, and the data controller) is the Sponsor-confirmed company information, applied on
 * the Sponsor's explicit instruction; independent Companies House confirmation is recommended before
 * publication. All OTHER legal/business facts remain bracketed [ ... — Sponsor to confirm] and are not
 * invented. Do not merge/deploy until those bracketed fields are resolved and legal review is complete.
 * A residential address is never published here.
 */
type Section = { h: string; p: string[] };

const copy: Record<Lang, {
  draft: string;
  eyebrow: string;
  title: string;
  updated: string;
  intro: string;
  never: string;
  limitedUseHeading: string;
  limitedUse: string;
  sections: Section[];
  home: string;
}> = {
  en: {
    draft:
      "Draft pending legal review. Fields shown in [brackets] are unresolved and require Sponsor/legal confirmation before publication.",
    eyebrow: "Privacy",
    title: "Privacy Policy",
    updated: "Last updated: [EFFECTIVE DATE — Sponsor to confirm]",
    intro:
      "This policy explains what personal data GuvFX processes and why, with particular detail on the optional email connection that powers Withdrawal Intelligence.",
    never: "GuvFX never initiates, confirms, cancels or moves a withdrawal. Withdrawal Intelligence is observation only.",
    limitedUseHeading: "Google API Services User Data Policy — Limited Use",
    limitedUse:
      "GuvFX's use and transfer of information received from Google APIs will adhere to the Google API Services User Data Policy, including the Limited Use requirements. Information obtained via Gmail APIs is used only to provide and improve the user-facing Withdrawal Intelligence feature; is not transferred to others except as necessary to provide the feature, to comply with applicable law, or as part of a merger or acquisition; is not used for advertising; and is not read by humans unless we have your explicit consent for specific messages, it is necessary for security or to comply with law, or the data is aggregated and anonymised for internal operations.",
    sections: [
      {
        h: "1. Who we are",
        p: [
          "GuvFX is operated by Virtus Logic UK Ltd (trading as GuvFX), a company registered in England and Wales under company number 13318959, whose registered office is Airport Business Centre, 10 Thornbury Road, Plymouth, England, PL6 7PP. GuvFX is available at guvfx.com. The data controller for personal data processed through GuvFX is Virtus Logic UK Ltd; privacy and data-protection enquiries may be sent to support@guvfx.com.",
        ],
      },
      {
        h: "2. The optional email connection",
        p: [
          "Withdrawal Intelligence is optional and operates only if you explicitly connect a supported email account.",
          "Scope requested: read-only Gmail access only — https://www.googleapis.com/auth/gmail.readonly. We never request permission to send, delete or modify your email, and we do not request any other Google data (no Drive, Contacts or Calendar).",
          "What we read: we read emails from recognised brokers (a sender allowlist). We derive funding and withdrawal events only from messages that also pass email authentication; a message from a recognised broker that fails authentication is retained as evidence but produces no event. We use the derived events to summarise your withdrawal activity.",
          "How we process it: processing is deterministic and automated. As part of this feature's automated processing, no GuvFX employee reads your email, and no large language model or other AI processes your email content (the limited exceptions in which a human may access data are described below under Limited Use). We derive specific, structured facts from each message; the original broker email is retained as described in section 3 (we do not discard it).",
        ],
      },
      {
        h: "3. What we store",
        p: [
          "The full raw broker email, retained write-once in a separate, access-controlled evidence store that is held apart from our application database (and outside our code repository); an integrity hash and a pointer to that email in the database; the structured events we derive (such as a withdrawal request or completion, amount, broker and timestamp); and an opaque reference to your encrypted access credential.",
          "Your Google OAuth tokens are encrypted and stored separately from our application database; the database holds only an opaque reference. Tokens are never written to logs.",
          "We never send, forward, delete, label or modify your email.",
        ],
      },
      {
        h: "4. How your data is protected",
        p: [
          "Credentials are encrypted at rest and held separately from the application database. Access follows least privilege, your data is isolated per account (you only ever see your own data), and administrative interfaces are restricted to authorised operators and are not publicly accessible.",
        ],
      },
      {
        h: "5. Other data we process",
        p: [
          "Account data you provide (email and authentication), the trading-account and performance data you connect to the platform, and standard operational and security logs used to run and secure the service.",
        ],
      },
      {
        h: "6. Legal bases (where GDPR / UK GDPR applies)",
        p: [
          "Consent for the optional email connection (withdrawable at any time by disconnecting); performance of a contract to provide the platform; and legitimate interests in security, fraud prevention and service improvement, balanced against your rights.",
        ],
      },
      {
        h: "7. Retention and deletion",
        p: [
          "Disconnecting your email in GuvFX removes GuvFX's stored access to that mailbox and stops further reading. You can also remove GuvFX's access directly in your Google Account (see section 8).",
          "You may request deletion of your derived withdrawal and broker records and stored evidence by contacting support@guvfx.com; we delete within [RETENTION PERIOD — Sponsor to confirm] unless retention is legally required.",
          "Operational and security logs are retained for [LOG RETENTION PERIOD — Sponsor to confirm].",
        ],
      },
      {
        h: "8. How to disconnect and delete access",
        p: [
          "In GuvFX: open Withdrawal Intelligence and use Disconnect on the connected email account.",
          "In Google: go to myaccount.google.com, then Security, then your connections to third-party apps and services, select GuvFX, and remove access.",
        ],
      },
      {
        h: "9. Sharing and international transfers",
        p: [
          "We do not sell or rent your data. We share only with the service providers necessary to run the platform, under contract, and as required by law. Sub-processors: [SUB-PROCESSOR LIST — Sponsor to confirm]. Where data is transferred internationally, the transfer mechanism is [TRANSFER MECHANISM — Sponsor to confirm].",
        ],
      },
      {
        h: "10. Your rights",
        p: [
          "Depending on your jurisdiction you may have rights to access, rectification, erasure, restriction, portability, objection and withdrawal of consent. Contact support@guvfx.com. You may also complain to your data-protection authority [SUPERVISORY AUTHORITY — Sponsor to confirm].",
        ],
      },
      {
        h: "11. Changes and contact",
        p: [
          "We will post changes here and update the date above. Questions: support@guvfx.com. Data protection contact: [DPO / DATA-PROTECTION CONTACT — Sponsor to confirm].",
        ],
      },
    ],
    home: "Back to GuvFX",
  },
  ja: {
    draft:
      "本ポリシーは法務レビュー前のドラフトです。[角括弧]で示した項目は未確定であり、公開前にスポンサー／法務の確認が必要です。",
    eyebrow: "プライバシー",
    title: "プライバシーポリシー",
    updated: "最終更新日：[発効日 — スポンサー確認待ち]",
    intro:
      "本ポリシーは、GuvFXが取り扱う個人データとその目的について、特に出金インテリジェンスを支える任意のメール連携に焦点を当てて説明します。",
    never: "GuvFXが出金を開始・確認・取消・送金することはありません。出金インテリジェンスは観測のみを行います。",
    limitedUseHeading: "Google API Services ユーザーデータポリシー — 限定使用（Limited Use）",
    limitedUse:
      "GuvFXがGoogle APIから受け取った情報の使用および転送は、限定使用（Limited Use）要件を含むGoogle API Servicesユーザーデータポリシーに準拠します。Gmail APIを通じて取得した情報は、ユーザー向けの出金インテリジェンス機能の提供・改善のためにのみ使用し、機能提供・法令遵守・合併／買収に必要な場合を除き第三者に転送せず、広告には使用せず、特定メッセージについての明示的な同意がある場合、セキュリティ上または法令遵守上必要な場合、または内部運用のために集計・匿名化された場合を除き、人間が閲覧することはありません。",
    sections: [
      {
        h: "1. 事業者について",
        p: [
          "GuvFXは、イングランド・ウェールズで登録された会社番号13318959のVirtus Logic UK Ltd（「GuvFX」の屋号で事業を行う）が運営します。登記上の事務所は Airport Business Centre, 10 Thornbury Road, Plymouth, England, PL6 7PP です。GuvFXはguvfx.comで提供されます。GuvFXで処理される個人データの管理者はVirtus Logic UK Ltdであり、プライバシーおよびデータ保護に関するお問い合わせは support@guvfx.com までお送りください。",
        ],
      },
      {
        h: "2. 任意のメール連携",
        p: [
          "出金インテリジェンスは任意であり、対応するメールアカウントをご自身で明示的に連携した場合にのみ動作します。",
          "要求する権限：読み取り専用のGmailアクセスのみ（https://www.googleapis.com/auth/gmail.readonly）。メールの送信・削除・変更の権限を求めることは一切なく、その他のGoogleデータ（Drive、連絡先、カレンダー等）も要求しません。",
          "読み取る対象：認識済みブローカー（送信元許可リスト）からのメールです。入金・出金イベントは、メール認証にも合格したメッセージからのみ導出します。認識済みブローカーからのメールであっても認証に失敗したものは、証跡として保持しますがイベントは生成しません。導出したイベントを用いて、お客様の出金アクティビティを要約します。",
          "処理方法：処理は決定論的かつ自動的です。本機能の自動処理においては、GuvFXの従業員がメールを読むことはなく、大規模言語モデルその他のAIがメール本文を処理することもありません（人間がデータにアクセスし得る限定的な例外は、下記の「限定使用」に記載します）。各メッセージから特定の構造化された事実を抽出します。元のブローカーメールは第3項に記載のとおり保持します（破棄しません）。",
        ],
      },
      {
        h: "3. 保存する情報",
        p: [
          "ブローカーの生メール全体（アプリケーションのデータベースとは別に、アクセス制御された証跡ストアにwrite-onceで保持します。コードリポジトリの外にあります）、当該メールの整合性ハッシュとそのポインタ（データベース内）、抽出した構造化イベント（出金の要求・完了、金額、ブローカー、タイムスタンプ等）、および暗号化されたアクセス資格情報への不透明な参照です。",
          "お客様のGoogle OAuthトークンは暗号化され、アプリケーションのデータベースとは別に保管されます。データベースには不透明な参照のみが保持されます。トークンがログに書き込まれることはありません。",
          "お客様のメールを送信・転送・削除・ラベル付け・変更することはありません。",
        ],
      },
      {
        h: "4. データの保護",
        p: [
          "資格情報は保存時に暗号化され、アプリケーションのデータベースとは別に保持されます。アクセスは最小権限に従い、データはアカウントごとに分離され（お客様はご自身のデータのみを参照できます）、管理用インターフェースは権限を有する運用担当者に限定され、一般には公開されません。",
        ],
      },
      {
        h: "5. その他に取り扱うデータ",
        p: [
          "お客様が提供するアカウント情報（メールおよび認証）、プラットフォームに連携する取引口座・パフォーマンスデータ、ならびにサービスの運用・保護に用いる標準的な運用・セキュリティログです。",
        ],
      },
      {
        h: "6. 法的根拠（GDPR／UK GDPRが適用される場合）",
        p: [
          "任意のメール連携についての同意（連携解除によりいつでも撤回可能）、プラットフォーム提供のための契約の履行、ならびにセキュリティ・不正防止・サービス改善に関する正当な利益（お客様の権利とのバランスを考慮）です。",
        ],
      },
      {
        h: "7. 保持と削除",
        p: [
          "GuvFXでメール連携を解除すると、GuvFXが保管する当該メールボックスへのアクセスが削除され、以降の読み取りは停止します。Googleアカウントから直接GuvFXのアクセスを削除することもできます（第8項参照）。",
          "抽出された出金・ブローカー記録および保存された証跡の削除は、support@guvfx.com までご請求いただけます。法令上の保持義務がない限り[保持期間 — スポンサー確認待ち]以内に削除します。",
          "運用・セキュリティログは[ログ保持期間 — スポンサー確認待ち]保持します。",
        ],
      },
      {
        h: "8. 連携解除とアクセス削除の方法",
        p: [
          "GuvFX内：出金インテリジェンスを開き、連携済みメールアカウントの「解除」を使用します。",
          "Google：myaccount.google.com → セキュリティ → サードパーティ製アプリ／サービスとの連携 → GuvFX → アクセスを削除。",
        ],
      },
      {
        h: "9. 共有と国際移転",
        p: [
          "お客様のデータを販売・貸与することはありません。プラットフォーム運用に必要なサービス提供者との間で、契約に基づき、また法令上必要な範囲でのみ共有します。サブプロセッサー：[サブプロセッサー一覧 — スポンサー確認待ち]。データを国際的に移転する場合の移転メカニズムは[移転メカニズム — スポンサー確認待ち]です。",
        ],
      },
      {
        h: "10. お客様の権利",
        p: [
          "お住まいの法域に応じて、アクセス・訂正・消去・制限・ポータビリティ・異議・同意撤回の権利を有する場合があります。support@guvfx.com までご連絡ください。データ保護監督機関[監督機関 — スポンサー確認待ち]への申立ても可能です。",
        ],
      },
      {
        h: "11. 変更と連絡先",
        p: [
          "変更は本ページに掲示し、上記の日付を更新します。お問い合わせ：support@guvfx.com。データ保護に関する連絡先：[DPO／データ保護担当 — スポンサー確認待ち]。",
        ],
      },
    ],
    home: "GuvFXに戻る",
  },
};

export default function PrivacyPage() {
  const [lang] = useState<Lang>(() => (typeof window === "undefined" ? "en" : detectLang()));
  const c = copy[lang];
  return (
    <main
      data-testid="privacy-page"
      style={{
        minHeight: "100vh",
        padding: "clamp(1rem, 5vw, 4rem)",
        boxSizing: "border-box",
        background: "radial-gradient(circle at top left, #0b1930, #050816 58%, #030610)",
        color: "#e9f4ff",
      }}
    >
      <div style={{ width: "100%", maxWidth: 760, margin: "0 auto" }}>
        <Link href="/" style={{ color: "#7dd3fc", textDecoration: "none", fontWeight: 700 }}>
          GuvFX
        </Link>

        <p
          data-testid="privacy-draft-notice"
          style={{
            marginTop: "1.25rem",
            padding: "0.75rem 0.9rem",
            borderRadius: 10,
            background: "rgba(251,191,36,0.07)",
            border: "1px solid rgba(251,191,36,0.2)",
            color: "#fde68a",
            lineHeight: 1.55,
            fontSize: "0.82rem",
          }}
        >
          {c.draft}
        </p>

        <section
          style={{
            marginTop: "clamp(1.5rem, 6vw, 3rem)",
            padding: "clamp(1.25rem, 5vw, 2.5rem)",
            borderRadius: 18,
            border: "1px solid rgba(74,179,255,0.18)",
            background: "rgba(6,11,29,0.92)",
            boxShadow: "0 20px 60px rgba(0,0,0,0.45)",
          }}
        >
          <p style={{ margin: 0, color: "#4ab3ff", fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: "0.12em" }}>
            {c.eyebrow}
          </p>
          <h1 style={{ margin: "0.6rem 0 0", fontSize: "clamp(1.7rem, 6vw, 2.5rem)" }}>{c.title}</h1>
          <p style={{ margin: "0.4rem 0 0", color: "#8aa0bd", fontSize: "0.85rem" }}>{c.updated}</p>
          <p style={{ color: "#b7c5dd", lineHeight: 1.7 }}>{c.intro}</p>

          <p
            data-testid="privacy-never-statement"
            style={{
              margin: "1.25rem 0",
              padding: "0.85rem 1rem",
              borderRadius: 12,
              background: "rgba(34,197,94,0.08)",
              border: "1px solid rgba(34,197,94,0.28)",
              color: "#bbf7d0",
              lineHeight: 1.6,
              fontWeight: 600,
            }}
          >
            {c.never}
          </p>

          {c.sections.map((s) => (
            <div key={s.h}>
              <h2 style={{ marginTop: "1.6rem", fontSize: "1.05rem" }}>{s.h}</h2>
              {s.p.map((para, i) => (
                <p key={i} style={{ color: "#b7c5dd", lineHeight: 1.7 }}>
                  {para}
                </p>
              ))}
            </div>
          ))}

          <h2 data-testid="privacy-limited-use-heading" style={{ marginTop: "1.6rem", fontSize: "1.05rem" }}>
            {c.limitedUseHeading}
          </h2>
          <p style={{ color: "#b7c5dd", lineHeight: 1.7 }}>{c.limitedUse}</p>

          <nav style={{ marginTop: "2rem" }}>
            <Link href="/" style={{ color: "#93c5fd" }}>
              {c.home}
            </Link>
          </nav>
        </section>
      </div>

      <LegalFooter lang={lang} />
    </main>
  );
}
