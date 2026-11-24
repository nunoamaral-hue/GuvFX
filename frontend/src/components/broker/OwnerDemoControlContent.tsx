"use client";

import React, { useCallback, useEffect, useState } from "react";

import {
  getOwnerDemoReadiness, ownerDemoConfirm, ownerDemoPrepare, ownerDemoStop,
} from "@/lib/copy-trading-api";
import type { Lang } from "@/lib/i18n";
import type {
  OwnerDemoConfirmResult, OwnerDemoPrepareResult, OwnerDemoReadiness, OwnerDemoRoute, OwnerDemoStopResult,
} from "@/types/copy-trading";

// Inline EN/JA table (Mechanism B — matches CopyTradingConfigContent). Value-blind strings only.
const T = {
  title: { en: "Copy Trading demonstration — #46 → #48", ja: "コピートレードのデモ — #46 → #48" },
  subtitle: {
    en: "Owner-operated, bounded live demonstration. No staff access, no shell commands, no secrets in the browser.",
    ja: "オーナー操作による限定的なライブデモ。スタッフ権限・シェルコマンド・ブラウザ上の秘密情報は不要です。",
  },
  loading: { en: "Loading…", ja: "読み込み中…" },
  notAvailable: { en: "Not available", ja: "利用できません" },
  darkNote: {
    en: "This control is DARK in production until a Sponsor arms it. The real demonstration still needs a separate, explicit Sponsor real-money approval.",
    ja: "本番ではスポンサーが有効化するまでこの操作は無効（DARK）です。実際のデモには別途、明示的なスポンサーの承認が必要です。",
  },
  notOwner: {
    en: "This demonstration can only be operated by the verified owner of the #46 → #48 route.",
    ja: "このデモは #46 → #48 ルートの確認済みオーナーのみが操作できます。",
  },
  stageReadiness: { en: "1 · Check readiness", ja: "1 · 準備状況の確認" },
  stagePrepare: { en: "2 · Prepare demonstration", ja: "2 · デモの準備" },
  stageConfirm: { en: "3 · Confirm arm", ja: "3 · アームの確認" },
  stageStop: { en: "4 · Stop / return DARK", ja: "4 · 停止 / DARK へ戻す" },
  ready: { en: "Ready to prepare", ja: "準備できました" },
  notReady: { en: "Not ready — resolve the blockers below", ja: "準備未完了 — 下記のブロッカーを解消してください" },
  blockers: { en: "Blockers", ja: "ブロッカー" },
  route: { en: "Governed route", ja: "対象ルート" },
  caps: { en: "Caps", ja: "上限" },
  authorities: { en: "Authorities", ja: "権限" },
  expiry: { en: "Expiry window", ja: "有効期限" },
  minutes: { en: "minutes", ja: "分" },
  prepare: { en: "Prepare demonstration", ja: "デモを準備" },
  prepared: { en: "Prepared — confirm or stop", ja: "準備完了 — 確認または停止" },
  confirmHeading: { en: "Deliberate confirmation", ja: "確定的な確認" },
  confirmRestate: {
    en: "You are about to arm a LIVE, one-order (max 0.01 lot) demonstration on the route below. Re-enter your password to confirm.",
    ja: "以下のルートでライブの1注文（最大0.01ロット）デモをアームしようとしています。確認のためパスワードを再入力してください。",
  },
  password: { en: "Your password", ja: "パスワード" },
  confirmArm: { en: "Confirm arm", ja: "アームを確認" },
  cancel: { en: "Cancel", ja: "キャンセル" },
  stop: { en: "Stop / return DARK", ja: "停止 / DARK へ戻す" },
  armed: { en: "Armed", ja: "アーム済み" },
  armPending: {
    en: "Confirmed. Execution stays DARK until the signed host actuators are provisioned and a Sponsor gives the real-money GO.",
    ja: "確認済み。署名済みホストアクチュエータの準備とスポンサーの承認があるまで実行はDARKのままです。",
  },
  darkRestored: { en: "Backend returned to DARK.", ja: "バックエンドはDARKに戻りました。" },
  teardownLabel: { en: "Teardown", ja: "ティアダウン" },
  partialExcluded: { en: "Partial close excluded", ja: "部分決済は対象外" },
  attestNote: {
    en: "Your “trade closed” statement is historical. The arm re-proves #46 clean, #48 flat and identity/route/symbol at the confirm instant and rolls back to DARK on any failure.",
    ja: "「取引を閉じた」という申告は過去の情報です。アームは確認の瞬間に #46 のクリーン状態・#48 の無取引・識別情報/ルート/シンボルを再検証し、失敗時はDARKへロールバックします。",
  },
} as const;

function Reasons({ codes }: { codes: string[] }) {
  return (
    <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
      {codes.map((c) => (
        <li key={c} style={{ fontFamily: "ui-monospace, monospace", fontSize: 13 }}>{c}</li>
      ))}
    </ul>
  );
}

function RouteCard({ route, lang }: { route: OwnerDemoRoute; lang: Lang }) {
  const L: Lang = lang === "ja" ? "ja" : "en";
  return (
    <div style={{ border: "1px solid var(--border, #e2e2e2)", borderRadius: 8, padding: 12, fontSize: 14 }}>
      <div><strong>{T.route[L]}:</strong> #{route.master_account_id} → #{route.follower_account_id}
        {" "}({route.source_symbol} → {route.follower_symbol})</div>
      <div><strong>{T.caps[L]}:</strong> {route.max_volume} lot · {route.max_orders} order ·
        {" "}{route.max_concurrent} concurrent · {T.partialExcluded[L]}</div>
      <div><strong>{T.authorities[L]}:</strong> {route.authorities.join(", ")}</div>
      <div><strong>{T.expiry[L]}:</strong> {route.valid_minutes} {T.minutes[L]}
        {route.expires_at ? ` · ${new Date(route.expires_at).toLocaleTimeString()}` : ""}</div>
      <div style={{ marginTop: 6, color: "var(--muted, #666)", fontSize: 12 }}>
        <strong>{T.teardownLabel[L]}:</strong> {route.teardown}</div>
    </div>
  );
}

export const OwnerDemoControlContent: React.FC<{ lang?: Lang }> = ({ lang = "en" }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [loaded, setLoaded] = useState(false);
  const [readiness, setReadiness] = useState<OwnerDemoReadiness | null>(null);
  const [prepared, setPrepared] = useState<OwnerDemoPrepareResult | null>(null);
  const [confirmResult, setConfirmResult] = useState<OwnerDemoConfirmResult | null>(null);
  const [stopResult, setStopResult] = useState<OwnerDemoStopResult | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    getOwnerDemoReadiness()
      .then((r) => { setReadiness(r); setLoaded(true); })
      .catch((e: unknown) => {
        const err = e as { status?: number; body?: { reason?: string } };
        setReadiness({ enabled: err.status === 503 ? false : true, stage: "readiness",
          authorized: err.status === 403 ? false : undefined, reason: err.body?.reason });
        setLoaded(true);
      });
  }, []);

  useEffect(() => { load(); }, [load]);

  const doPrepare = async () => {
    setBusy(true); setError(null); setConfirmResult(null); setStopResult(null);
    try {
      const res = await ownerDemoPrepare();
      setPrepared(res);
      if (!res.prepared) setError(res.reason || "prepare_failed");
    } catch (e) { setError((e as Error)?.message || "prepare_failed"); }
    finally { setBusy(false); }
  };

  const doConfirm = async () => {
    if (!prepared?.confirm_token) return;
    setBusy(true); setError(null);
    try {
      const res = await ownerDemoConfirm({ confirm_token: prepared.confirm_token, password });
      setConfirmResult(res);
      setConfirmOpen(false);
      setPassword("");
      if (!res.armed && res.reason && res.reason !== "EXECUTION_ACTUATORS_UNPROVISIONED") {
        setError(res.reason);
      }
      load();
    } catch (e) {
      const err = e as { body?: { reason?: string }; message?: string };
      setError(err.body?.reason || err.message || "confirm_failed");
    } finally { setBusy(false); }
  };

  const doStop = async () => {
    setBusy(true); setError(null);
    try {
      const res = await ownerDemoStop();
      setStopResult(res);
      setPrepared(null); setConfirmResult(null);
      load();
    } catch (e) { setError((e as Error)?.message || "stop_failed"); }
    finally { setBusy(false); }
  };

  if (!loaded) return <div style={{ padding: 16 }}>{T.loading[L]}</div>;

  if (readiness && readiness.enabled === false) {
    return (
      <div style={{ padding: 16, maxWidth: 680 }}>
        <h2>{T.title[L]}</h2>
        <p style={{ color: "var(--muted, #666)" }}>{T.notAvailable[L]}</p>
        <p style={{ fontSize: 13 }}>{T.darkNote[L]}</p>
      </div>
    );
  }
  if (readiness && readiness.authorized === false) {
    return (
      <div style={{ padding: 16, maxWidth: 680 }}>
        <h2>{T.title[L]}</h2>
        <p style={{ color: "var(--muted, #666)" }}>{T.notOwner[L]}</p>
      </div>
    );
  }

  const route = prepared?.route || readiness?.route;
  const canPrepare = !!readiness?.ready_to_prepare && !prepared?.prepared;

  return (
    <div style={{ padding: 16, maxWidth: 720, margin: "0 auto" }}>
      <h2 style={{ marginBottom: 2 }}>{T.title[L]}</h2>
      <p style={{ color: "var(--muted, #666)", marginTop: 0, fontSize: 14 }}>{T.subtitle[L]}</p>

      {/* Stage 1 — readiness */}
      <section style={{ marginTop: 16 }}>
        <h3 style={{ fontSize: 15 }}>{T.stageReadiness[L]}</h3>
        {readiness?.ready_to_prepare
          ? <p style={{ color: "var(--ok, #157a3d)" }}>✓ {T.ready[L]}</p>
          : <>
              <p style={{ color: "var(--warn, #9a6a00)" }}>{T.notReady[L]}</p>
              {readiness?.blockers && readiness.blockers.length > 0 &&
                <><div style={{ fontSize: 13 }}>{T.blockers[L]}:</div>
                  <Reasons codes={readiness.blockers} /></>}
            </>}
        {route && <div style={{ marginTop: 10 }}><RouteCard route={route} lang={lang} /></div>}
        <p style={{ fontSize: 12, color: "var(--muted, #666)", marginTop: 8 }}>{T.attestNote[L]}</p>
      </section>

      {/* Stage 2 — prepare */}
      <section style={{ marginTop: 16 }}>
        <h3 style={{ fontSize: 15 }}>{T.stagePrepare[L]}</h3>
        <button type="button" data-testid="owner-demo-prepare" disabled={!canPrepare || busy}
                onClick={() => void doPrepare()}
                style={{ padding: "8px 14px", borderRadius: 6 }}>
          {T.prepare[L]}
        </button>
        {prepared?.prepared && <p style={{ color: "var(--ok, #157a3d)", marginTop: 8 }}>✓ {T.prepared[L]}</p>}
      </section>

      {/* Stage 3 — confirm */}
      {prepared?.prepared && !confirmResult?.armed && (
        <section style={{ marginTop: 16 }}>
          <h3 style={{ fontSize: 15 }}>{T.stageConfirm[L]}</h3>
          <button type="button" data-testid="owner-demo-confirm-open" disabled={busy}
                  onClick={() => setConfirmOpen(true)}
                  style={{ padding: "8px 14px", borderRadius: 6, background: "#b23b3b", color: "#fff" }}>
            {T.confirmArm[L]}
          </button>
        </section>
      )}

      {confirmResult && (
        <p style={{ marginTop: 12, color: confirmResult.armed ? "var(--ok, #157a3d)" : "var(--muted, #666)" }}>
          {confirmResult.armed ? `✓ ${T.armed[L]}` : T.armPending[L]}
        </p>
      )}

      {/* Stage 4 — stop */}
      {(prepared?.prepared || confirmResult) && (
        <section style={{ marginTop: 16 }}>
          <h3 style={{ fontSize: 15 }}>{T.stageStop[L]}</h3>
          <button type="button" data-testid="owner-demo-stop" disabled={busy} onClick={() => void doStop()}
                  style={{ padding: "8px 14px", borderRadius: 6 }}>
            {T.stop[L]}
          </button>
          {stopResult?.backend_dark && <p style={{ color: "var(--ok, #157a3d)", marginTop: 8 }}>✓ {T.darkRestored[L]}</p>}
        </section>
      )}

      {error && <p role="alert" style={{ color: "var(--err, #b23b3b)", marginTop: 12,
        fontFamily: "ui-monospace, monospace", fontSize: 13 }}>{error}</p>}

      {/* Confirm modal — deliberate, re-authentication (password re-entry) */}
      {confirmOpen && (
        <div role="dialog" aria-modal="true"
             style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.4)", display: "flex",
               alignItems: "center", justifyContent: "center", padding: 16 }}>
          <div style={{ background: "var(--bg, #fff)", borderRadius: 10, padding: 20, maxWidth: 480, width: "100%" }}>
            <h3 style={{ marginTop: 0 }}>{T.confirmHeading[L]}</h3>
            <p style={{ fontSize: 14 }}>{T.confirmRestate[L]}</p>
            {route && <RouteCard route={route} lang={lang} />}
            <label style={{ display: "block", marginTop: 12, fontSize: 14 }}>{T.password[L]}
              <input type="password" data-testid="owner-demo-password" autoComplete="current-password"
                     value={password} onChange={(e) => setPassword(e.target.value)}
                     style={{ display: "block", width: "100%", marginTop: 4, padding: "8px",
                       borderRadius: 6, border: "1px solid var(--border, #ccc)" }} />
            </label>
            <div style={{ display: "flex", gap: 8, marginTop: 16, justifyContent: "flex-end" }}>
              <button type="button" onClick={() => { setConfirmOpen(false); setPassword(""); }}
                      style={{ padding: "8px 14px", borderRadius: 6 }}>{T.cancel[L]}</button>
              <button type="button" data-testid="owner-demo-confirm" disabled={busy || !password}
                      onClick={() => void doConfirm()}
                      style={{ padding: "8px 14px", borderRadius: 6, background: "#b23b3b", color: "#fff" }}>
                {T.confirmArm[L]}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
