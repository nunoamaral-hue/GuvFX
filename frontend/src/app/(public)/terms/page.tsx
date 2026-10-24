"use client";

import Link from "next/link";
import { useState } from "react";
import { detectLang, type Lang } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";

/**
 * Public Terms of Service (/terms) — accessible without authentication.
 *
 * Grounded in the real service (a strategy-management and broker/withdrawal analytics platform with an
 * optional read-only email connection). Operator identity (Virtus Logic UK Ltd, company no. 13318959,
 * registered office), governing law and jurisdiction (England and Wales), effective date (24 October
 * 2026) and conservative standard liability/warranty wording are all Sponsor-confirmed for this
 * founder-controlled controlled launch. No contractual facts are invented and no placeholders remain.
 */
type Section = { h: string; p: string[] };

const copy: Record<Lang, {
  eyebrow: string;
  title: string;
  updated: string;
  intro: string;
  never: string;
  sections: Section[];
  home: string;
}> = {
  en: {
    eyebrow: "Terms",
    title: "Terms of Service",
    updated: "Last updated: 24 October 2026",
    intro:
      "These terms govern your use of the GuvFX platform at guvfx.com. By using GuvFX you agree to them.",
    never: "GuvFX never initiates, confirms, cancels or moves a withdrawal. You remain solely responsible for your own trading and withdrawal decisions.",
    sections: [
      {
        h: "1. The service",
        p: [
          "GuvFX is a strategy-management and analytics platform. It includes Withdrawal Intelligence, an optional feature that reads your email read-only to detect your broker funding and withdrawal events and summarise your withdrawal activity.",
          "GuvFX is operated by Virtus Logic UK Ltd (trading as GuvFX), a company registered in England and Wales under company number 13318959, whose registered office is Airport Business Centre, 10 Thornbury Road, Plymouth, England, PL6 7PP. The service is currently offered on a limited/beta basis.",
        ],
      },
      {
        h: "2. Eligibility and your account",
        p: [
          "You must be able to form a binding contract to use GuvFX. You are responsible for keeping your credentials secure and for activity under your account. Do not share your password or verification codes.",
        ],
      },
      {
        h: "3. The optional email connection",
        p: [
          "Withdrawal Intelligence operates only if you explicitly connect a supported email account, and requests read-only Gmail access only. You confirm you are authorised to connect that mailbox. How we handle this data is described in the Privacy Policy.",
        ],
      },
      {
        h: "4. No financial advice; no withdrawal actions",
        p: [
          "GuvFX provides software tools and analytics only. It does not provide investment, financial, legal or tax advice, and nothing in the service is a recommendation to trade or to move funds.",
          "GuvFX never initiates, confirms, cancels or moves a withdrawal on your behalf. A broker-reported completion shown in GuvFX is not independent confirmation that funds have been received. You remain responsible for your own decisions.",
          "Trading in financial instruments carries risk, including loss of capital. Past performance does not guarantee future results.",
        ],
      },
      {
        h: "5. Acceptable use",
        p: [
          "You agree not to misuse the service: no attempting to access other users' data, no interfering with or probing the platform's security, no reverse engineering except as permitted by law, and no use that breaches applicable law or your broker's terms.",
        ],
      },
      {
        h: "6. Data and privacy",
        p: [
          "Your use of GuvFX is also governed by our Privacy Policy, which explains what data we process and your rights.",
        ],
      },
      {
        h: "7. Availability and changes to the service",
        p: [
          "The service is provided on an \"as is\" and \"as available\" basis. Features may change, and the service may be suspended or withdrawn, particularly during the beta period.",
        ],
      },
      {
        h: "8. Disclaimers and limitation of liability",
        p: [
          "To the extent permitted by law, GuvFX is provided \"as is\" and \"as available\", without warranties of any kind, whether express or implied, including any implied warranties of merchantability, fitness for a particular purpose and non-infringement. To the maximum extent permitted by law, neither GuvFX nor Virtus Logic UK Ltd will be liable for any indirect, incidental, special or consequential loss, or for any loss of profits, trading or investment losses, or loss of data, arising out of or in connection with your use of the service, and our total liability for any claim is limited to the amount (if any) you have paid to use the service. Nothing in these terms excludes or limits any liability that cannot lawfully be excluded or limited, including liability for death or personal injury caused by negligence, or for fraud or fraudulent misrepresentation.",
        ],
      },
      {
        h: "9. Termination",
        p: [
          "You may stop using GuvFX at any time and disconnect any connected email account. We may suspend or terminate access for breach of these terms or where required by law. On termination your access ends and connected credentials are removed.",
        ],
      },
      {
        h: "10. Changes to these terms",
        p: [
          "We may update these terms and will post changes here, updating the date above. Continued use after a change means you accept the updated terms.",
        ],
      },
      {
        h: "11. Governing law and contact",
        p: [
          "These terms are governed by the laws of England and Wales, with disputes subject to the exclusive jurisdiction of the courts of England and Wales. Questions: support@guvfx.com.",
        ],
      },
    ],
    home: "Back to GuvFX",
  },
  ja: {
    eyebrow: "規約",
    title: "利用規約",
    updated: "最終更新日：2026年10月24日",
    intro: "本規約は、guvfx.comで提供されるGuvFXプラットフォームの利用に適用されます。GuvFXを利用することで、本規約に同意したものとみなされます。",
    never: "GuvFXが出金を開始・確認・取消・送金することはありません。ご自身の取引および出金の判断については、お客様が単独で責任を負います。",
    sections: [
      {
        h: "1. サービスについて",
        p: [
          "GuvFXは戦略管理・分析プラットフォームです。これには、メールを読み取り専用で参照し、ブローカーの入金・出金イベントを検出して出金アクティビティを要約する任意機能「出金インテリジェンス」が含まれます。",
          "GuvFXは、イングランド・ウェールズで登録された会社番号13318959のVirtus Logic UK Ltd（「GuvFX」の屋号で事業を行う）が運営します。登記上の事務所は Airport Business Centre, 10 Thornbury Road, Plymouth, England, PL6 7PP です。本サービスは現在、限定／ベータとして提供されています。",
        ],
      },
      {
        h: "2. 利用資格とアカウント",
        p: [
          "GuvFXの利用には、拘束力のある契約を締結できることが必要です。資格情報の安全な管理およびアカウント下の活動については、お客様が責任を負います。パスワードや確認コードを共有しないでください。",
        ],
      },
      {
        h: "3. 任意のメール連携",
        p: [
          "出金インテリジェンスは、対応するメールアカウントをご自身で明示的に連携した場合にのみ動作し、読み取り専用のGmailアクセスのみを要求します。お客様は、当該メールボックスを連携する権限を有することを表明するものとします。データの取扱いはプライバシーポリシーに記載しています。",
        ],
      },
      {
        h: "4. 投資助言の不提供・出金操作の不実施",
        p: [
          "GuvFXはソフトウェアツールおよび分析のみを提供します。投資・金融・法務・税務に関する助言は提供せず、本サービスのいかなる内容も取引や資金移動の推奨ではありません。",
          "GuvFXがお客様に代わって出金を開始・確認・取消・送金することはありません。GuvFXに表示されるブローカー報告の「完了」は、資金の受領を独立に確認するものではありません。判断の責任はお客様にあります。",
          "金融商品の取引には、元本の損失を含むリスクが伴います。過去の実績は将来の結果を保証しません。",
        ],
      },
      {
        h: "5. 許容される利用",
        p: [
          "本サービスを濫用しないことに同意するものとします。すなわち、他の利用者のデータへのアクセスの試み、プラットフォームのセキュリティへの妨害・探索、法が認める範囲を超えるリバースエンジニアリング、適用法令やブローカー規約に違反する利用を行わないものとします。",
        ],
      },
      {
        h: "6. データとプライバシー",
        p: [
          "GuvFXの利用には、取り扱うデータとお客様の権利を説明するプライバシーポリシーも適用されます。",
        ],
      },
      {
        h: "7. 提供状況と変更",
        p: [
          "本サービスは「現状有姿」かつ「提供可能な範囲」で提供されます。特にベータ期間中は、機能が変更され、またサービスが停止・終了する場合があります。",
        ],
      },
      {
        h: "8. 免責と責任の制限",
        p: [
          "法が認める範囲で、GuvFXは「現状有姿」かつ「提供可能な範囲」で、明示・黙示を問わずいかなる保証（商品性、特定目的への適合性、非侵害の黙示の保証を含みます）もなく提供されます。法が認める最大限の範囲で、GuvFXおよびVirtus Logic UK Ltdは、間接的・付随的・特別または結果的損害、ならびに逸失利益、取引・投資上の損失、データの損失について責任を負わず、当社の総責任は、お客様が本サービスの利用のために支払った金額（ある場合）を上限とします。法律上除外または制限できない責任（過失による死亡・人身傷害、詐欺または不実表示に関する責任を含みます）を本規約が除外・制限することはありません。",
        ],
      },
      {
        h: "9. 終了",
        p: [
          "お客様はいつでもGuvFXの利用を停止し、連携済みのメールアカウントを解除できます。当社は、本規約違反の場合または法令上必要な場合に、アクセスを停止・終了することがあります。終了に伴い、アクセスは終了し、連携された資格情報は削除されます。",
        ],
      },
      {
        h: "10. 本規約の変更",
        p: [
          "当社は本規約を更新することがあり、変更は本ページに掲示して上記の日付を更新します。変更後の継続利用は、更新後の規約への同意を意味します。",
        ],
      },
      {
        h: "11. 準拠法と連絡先",
        p: [
          "本規約はイングランド及びウェールズの法律に準拠し、紛争はイングランド及びウェールズの裁判所の専属管轄に服します。お問い合わせ：support@guvfx.com。",
        ],
      },
    ],
    home: "GuvFXに戻る",
  },
};

export default function TermsPage() {
  const [lang] = useState<Lang>(() => (typeof window === "undefined" ? "en" : detectLang()));
  const c = copy[lang];
  return (
    <main
      data-testid="terms-page"
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
            data-testid="terms-never-statement"
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
