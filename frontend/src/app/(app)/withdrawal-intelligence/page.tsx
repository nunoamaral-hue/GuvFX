"use client";

/**
 * Withdrawal Intelligence — permanent authenticated member page (production general release).
 *
 * This is the genuine, owner-scoped customer feature: it observes the member's OWN broker withdrawal notifications
 * from an authorized READ-ONLY email source. GuvFX never initiates, confirms, cancels or moves a withdrawal.
 *
 * The page composes the already-owner-scoped, DARK-safe sections — each self-hides (renders null) while its backend
 * flag is OFF (the endpoint returns 404), so no half-built UI ever appears before staged activation:
 *   - Connection state:   <ConnectedEmailAccounts/>  (BROKER_MAILBOX_STATUS_ENABLED)
 *   - Withdrawal overview: <WithdrawalsPanel/>        (BROKER_WITHDRAWAL_UX_ENABLED)
 *   - Withdrawal history:  <WithdrawalsHistory/>      (BROKER_WITHDRAWAL_UX_ENABLED)
 * Plus a persistent intro, empty/onboarding prompt and an evidence explanation that are always shown, so the page is
 * coherent before any data exists and before the feature is armed.
 *
 * Genuine-only: the member APIs return REAL rows by default (synthetic requires the separate OFF demo flag), so the
 * production experience carries no demonstration banner and no synthetic customer-history rows.
 */
import { useLang } from "@/components/AppShell";
import { ConnectedEmailAccounts } from "@/components/accounts/ConnectedEmailAccounts";
import { WithdrawalsPanel } from "@/components/dashboard/WithdrawalsPanel";
import { WithdrawalsHistory } from "@/components/dashboard/WithdrawalsHistory";

type Lang = "en" | "ja";

const S = {
  title: { en: "Withdrawal Intelligence", ja: "出金インテリジェンス" },
  lede: {
    en: "GuvFX observes your broker withdrawal notifications from an authorized read-only email source — so you can see request and completion timing in one place.",
    ja: "GuvFXは、許可された読み取り専用のメールソースからブローカーの出金通知を観測し、申請と完了のタイミングを一か所で確認できるようにします。",
  },
  neverTitle: { en: "What GuvFX does", ja: "GuvFXの役割" },
  never: {
    en: "GuvFX never initiates, confirms, cancels or moves a withdrawal. It only reads notification emails you authorize and shows you what it observed.",
    ja: "GuvFXが出金を開始・確認・取消・送金することはありません。許可された通知メールを読み取り、観測した内容を表示するだけです。",
  },
  connectionH: { en: "Email connection", ja: "メール接続" },
  overviewH: { en: "Overview", ja: "概要" },
  historyH: { en: "Withdrawal history", ja: "出金履歴" },
  emptyTitle: { en: "No withdrawal activity observed yet", ja: "出金活動はまだ観測されていません" },
  empty: {
    en: "No withdrawal activity has been observed yet. Connect your supported email account and GuvFX will monitor eligible broker withdrawal notifications using read-only access.",
    ja: "出金活動はまだ観測されていません。対応するメールアカウントを接続すると、GuvFXは読み取り専用アクセスで対象のブローカー出金通知を監視します。",
  },
  evidenceH: { en: "How this works & its limits", ja: "仕組みと制約" },
  evidence: {
    en: [
      "What GuvFX reads: only broker withdrawal-notification emails (request and completion) from the authorized mailbox, using read-only Gmail access.",
      "What GuvFX does NOT read: it cannot send, delete or modify email, cannot move funds, and does not read unrelated messages for this feature.",
      "Timestamps: “request observed” and “completion observed” are when each notification email arrived — the observable interval between them, not an internal system latency.",
      "Correlation: a request and its completion are linked using the broker’s own reference in the emails plus the authenticated evidence; unrelated events are never merged.",
      "Account unresolved: when the withdrawal is attributed to you but the originating trading account is not yet registered in GuvFX, it is shown as “Account unresolved” and never bound to an unrelated account.",
      "Broker-reported completion is not independent confirmation of fund receipt: a “completed” notification is what the broker reported; GuvFX does not independently verify that funds arrived in your wallet or bank.",
    ],
    ja: [
      "GuvFXが読み取る情報：許可されたメールボックス内のブローカー出金通知メール（申請と完了）のみを、読み取り専用のGmailアクセスで取得します。",
      "GuvFXが読み取らない情報：メールの送信・削除・変更はできず、資金の移動もできません。この機能のために無関係なメッセージを読むことはありません。",
      "タイムスタンプ：「申請を観測」「完了を観測」は各通知メールが届いた時刻であり、両者の観測可能な間隔を示します（システム内部の遅延ではありません）。",
      "相関付け：申請とその完了は、メール内のブローカー独自の参照番号と認証済みの証跡を用いて関連付けます。無関係なイベントが統合されることはありません。",
      "口座未解決：出金があなたに帰属するものの、発生元の取引口座がまだGuvFXに登録されていない場合、「口座未解決」として表示し、無関係な口座に結び付けることはありません。",
      "ブローカー報告の完了は資金受領の独立した確認ではありません：「完了」通知はブローカーが報告した内容であり、GuvFXは資金がウォレットや銀行に到着したことを独立して検証しません。",
    ],
  },
} as const;

const wrap: React.CSSProperties = {
  maxWidth: 980, margin: "0 auto", padding: "1.5rem 1rem 3rem", display: "flex", flexDirection: "column", gap: "1.1rem",
};
const card: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem", background: "rgba(12,18,40,0.6)",
};
const h2: React.CSSProperties = { margin: "0 0 0.4rem", fontSize: "0.95rem", color: "#dbe6f5" };
const sectionLabel: React.CSSProperties = {
  fontSize: "0.72rem", color: "#8fa0b7", textTransform: "uppercase", letterSpacing: "0.05em", margin: "0.3rem 0 0.2rem",
};

export default function WithdrawalIntelligencePage() {
  const langRaw = useLang();
  const L: Lang = langRaw === "ja" ? "ja" : "en";

  return (
    <div style={wrap} data-testid="withdrawal-intelligence">
      <header>
        <h1 style={{ margin: 0, fontSize: "1.5rem", color: "#eaf2ff" }}>{S.title[L]}</h1>
        <p style={{ color: "#9db0c7", fontSize: "0.9rem", lineHeight: 1.6, margin: "0.5rem 0 0" }}>{S.lede[L]}</p>
      </header>

      <div style={{ ...card, borderColor: "rgba(90,209,154,0.3)", background: "rgba(90,209,154,0.06)" }}>
        <div style={{ ...sectionLabel, color: "#7ee3b0", marginTop: 0 }}>{S.neverTitle[L]}</div>
        <p style={{ margin: 0, fontSize: "0.86rem", color: "#c7e8d6", lineHeight: 1.6 }}>{S.never[L]}</p>
      </div>

      {/* Connection state — self-hides (null) until BROKER_MAILBOX_STATUS_ENABLED is armed */}
      <section>
        <div style={sectionLabel}>{S.connectionH[L]}</div>
        <ConnectedEmailAccounts lang={L} />
      </section>

      {/* Overview + history — self-hide (null) until BROKER_WITHDRAWAL_UX_ENABLED is armed; genuine-only in prod */}
      <section>
        <div style={sectionLabel}>{S.overviewH[L]}</div>
        <WithdrawalsPanel lang={L} />
      </section>
      <section>
        <div style={sectionLabel}>{S.historyH[L]}</div>
        <WithdrawalsHistory lang={L} />
      </section>

      {/* Persistent onboarding / empty prompt — always shown so the page is coherent before data/activation. */}
      <div style={card} data-testid="wi-empty-hint">
        <h2 style={h2}>{S.emptyTitle[L]}</h2>
        <p style={{ margin: 0, fontSize: "0.86rem", color: "#9db0c7", lineHeight: 1.6 }}>{S.empty[L]}</p>
      </div>

      {/* Evidence explanation + limitations (§5) */}
      <div style={card} data-testid="wi-evidence">
        <h2 style={h2}>{S.evidenceH[L]}</h2>
        <ul style={{ margin: "0.4rem 0 0", paddingLeft: 18, color: "#b8c7db", fontSize: "0.82rem", lineHeight: 1.7 }}>
          {S.evidence[L].map((t, i) => <li key={i}>{t}</li>)}
        </ul>
      </div>
    </div>
  );
}
