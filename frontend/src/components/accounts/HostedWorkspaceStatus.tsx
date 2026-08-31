"use client";

import React from "react";
import Link from "next/link";
import { authorizeExecution, describeJourney, type HostedJourney } from "@/lib/hosted-journey";
import { useLang } from "@/components/AppShell";
import { t, type Lang } from "@/lib/i18n";

/**
 * Hosted Workspace status panel (Product Consistency Pass — P0.1 / P1.3).
 *
 * A Hosted Workspace customer NEVER enters a broker server / login / password by hand — GuvFX runs a
 * managed MetaTrader terminal and the customer logs in INSIDE it (deferred identity bind). So on the
 * Accounts page a hosted customer must see a read-only STATUS experience, not the legacy "Add Trading
 * Account" form. This panel is pure presentation: it derives every row from the already-loaded hosted
 * journey (workspace/terminal/broker/readiness) plus the accounts list (active account, demo/live) — no
 * new backend, no credential entry, and it always leads to ONE next action (never a dead end).
 */

type Acct = {
  id: number;
  name?: string | null;
  account_number?: string | null;
  is_active?: boolean;
  is_demo?: boolean;
};

const glass: React.CSSProperties = {
  borderRadius: 12,
  border: "1px solid rgba(74, 179, 255, 0.18)",
  background: "linear-gradient(135deg, rgba(10,15,40,0.95) 0%, rgba(5,8,22,0.98) 100%)",
  boxShadow: "0 8px 32px rgba(0,0,0,0.4)",
  padding: "1.25rem 1.35rem",
};

const primaryBtn: React.CSSProperties = {
  display: "inline-block",
  padding: "0.55rem 1.35rem",
  borderRadius: 999,
  background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 50%, #2979ff 100%)",
  color: "#ffffff",
  fontSize: "0.88rem",
  fontWeight: 600,
  textDecoration: "none",
  boxShadow: "0 10px 30px rgba(37, 99, 235, 0.45)",
};

// Customer-safe workspace status label per phase (no internal identifiers ever reach the customer).
const WORKSPACE_STATUS: Record<string, { key: string; color: string }> = {
  NO_WORKSPACE: { key: "hostedStatus.notSetUp", color: "#94a3b8" },
  WORKSPACE_REQUESTED: { key: "hostedStatus.preparing", color: "#38bdf8" },
  WORKSPACE_PREPARING: { key: "hostedStatus.preparing", color: "#38bdf8" },
  AWAITING_BROKER_LOGIN: { key: "hostedStatus.waitingLogin", color: "#f59e0b" },
  BROKER_CONNECTED: { key: "hostedStatus.waitingLogin", color: "#f59e0b" },
  ACCOUNT_CONFIRMATION_REQUIRED: { key: "hostedStatus.confirmAccount", color: "#f59e0b" },
  ACCOUNT_BOUND: { key: "hostedStatus.finishing", color: "#38bdf8" },
  WORKSPACE_READY: { key: "hostedStatus.ready", color: "#22c55e" },
  WORKSPACE_UNAVAILABLE: { key: "hostedStatus.needsAttention", color: "#f87171" },
};

const DELIVERY_STATUS: Record<string, { key: string; color: string }> = {
  DELIVERY_READY: { key: "hostedStatus.readyToOpen", color: "#22c55e" },
  DELIVERY_PREPARING: { key: "hostedStatus.preparing", color: "#38bdf8" },
  DELIVERY_NOT_AVAILABLE: { key: "hostedStatus.notReady", color: "#94a3b8" },
  DELIVERY_EXTERNAL_GATE: { key: "hostedStatus.actionNeeded", color: "#f59e0b" },
};

// Status-oriented copy for THIS read-only page. The interactive journey's own description is imperative
// ("Enter your broker account number…", "confirm it…"), which would tell the customer to do something they
// cannot do here — the actual controls live in the hosted flow the primary CTA opens. So we describe the
// state and point at that CTA, never instruct an action this page has no field/button for.
const STATUS_DESC: Record<string, string> = {
  NO_WORKSPACE: "hostedStatus.desc.noWorkspace",
  WORKSPACE_REQUESTED: "hostedStatus.desc.requested",
  WORKSPACE_PREPARING: "hostedStatus.desc.preparing",
  AWAITING_BROKER_LOGIN: "hostedStatus.desc.awaitingLogin",
  BROKER_CONNECTED: "hostedStatus.desc.connected",
  ACCOUNT_CONFIRMATION_REQUIRED: "hostedStatus.desc.confirm",
  ACCOUNT_BOUND: "hostedStatus.desc.bound",
  WORKSPACE_READY: "hostedStatus.desc.ready",
  WORKSPACE_UNAVAILABLE: "hostedStatus.desc.unavailable",
};

function maskNumber(n?: string | null): string {
  const s = (n || "").trim();
  if (!s) return "";
  return s.length <= 3 ? s : "•••" + s.slice(-3);
}

export function HostedWorkspaceStatus({ journey, accounts, onAuthorized }: {
  journey: HostedJourney;
  accounts: Acct[];
  /** Called after the customer successfully enables automated trading, so the page re-reads authoritative
   *  state. Defaults to a full reload (the accounts page re-fetches the journey). */
  onAuthorized?: () => void;
}) {
  const lang = useLang();
  const view = describeJourney(journey);
  const desc = STATUS_DESC[journey.phase]
    ? t(lang, STATUS_DESC[journey.phase])
    : t(lang, view.descriptionKey, view.descriptionParams);
  const ws = WORKSPACE_STATUS[journey.phase] ?? { key: "hostedStatus.inProgress", color: "#38bdf8" };
  const del = DELIVERY_STATUS[journey.delivery] ?? { key: "hostedStatus.preparing", color: "#38bdf8" };
  const active = accounts.find((a) => a.is_active) ?? accounts[0];
  // Broker account: show "<server> · <masked login>" once the account is matched (both fields come from the
  // authoritative read model, non-empty only when proj_account_match=True), else "Not yet".
  const brokerLogin = (journey.active_login_masked || "").trim();
  const brokerServer = (journey.active_server || "").trim();
  const brokerDetected = brokerLogin
    ? (brokerServer ? `${brokerServer} · ${brokerLogin}` : brokerLogin)
    : "";
  const ready = journey.phase === "WORKSPACE_READY";

  // "Trading readiness" is reported at the ASSIGNMENT tier (strategy_eligible), strictly below arming/order
  // authority — never implying it is safe to send an order.
  const readinessValue = journey.strategy_eligible
    ? t(lang, "hostedStatus.readyChoose")
    : ready
      ? t(lang, "hostedStatus.ready")
      : t(lang, "hostedStatus.settingUp");

  const rows: Array<{ label: string; value: string; color: string }> = [
    { label: t(lang, "hostedStatus.workspaceLabel"), value: t(lang, ws.key), color: ws.color },
    { label: t(lang, "hostedStatus.terminalLabel"), value: t(lang, del.key), color: del.color },
    { label: t(lang, "hostedStatus.brokerLabel"), value: brokerDetected || t(lang, "hostedStatus.notYet"), color: brokerDetected ? "#e5f4ff" : "#94a3b8" },
    { label: t(lang, "hostedStatus.accountType"), value: active ? (active.is_demo === false ? t(lang, "hostedStatus.live") : t(lang, "hostedStatus.demo")) : "—", color: "#e5f4ff" },
    {
      label: t(lang, "hostedStatus.activeAccount"),
      value: active
        ? (active.name || `#${active.id}`) + (maskNumber(active.account_number) ? ` · ${maskNumber(active.account_number)}` : "")
        : t(lang, "hostedStatus.notConnected"),
      color: active ? "#e5f4ff" : "#94a3b8",
    },
    { label: t(lang, "hostedStatus.readiness"), value: readinessValue, color: journey.strategy_eligible ? "#22c55e" : "#94a3b8" },
  ];

  // ADR-0047 — the AUTOMATED-TRADING (authorization) tier, shown only once the workspace is ready. Capability
  // (the workspace being ready) is NEVER presented as consent: "Ready — not yet enabled" is the honest resting
  // state, and automated trading only begins after the customer explicitly clicks Enable.
  const armed = journey.execution_armed === true;
  const canEnable = journey.can_enable_automated_trading === true;
  if (ready) {
    rows.push({
      label: t(lang, "hostedStatus.automated"),
      value: armed ? t(lang, "hostedStatus.enabled") : canEnable ? t(lang, "hostedStatus.readyNotEnabled") : t(lang, "hostedStatus.preparing"),
      color: armed ? "#22c55e" : canEnable ? "#f59e0b" : "#38bdf8",
    });
  }

  // ONE clear next action, always. Ready → open the terminal; otherwise → continue the hosted journey.
  const primaryHref = ready ? "/trading/terminal-access" : "/onboarding/hosted";
  const primaryLabel = ready ? t(lang, "hostedStatus.openMetaTrader") : t(lang, "hostedStatus.continueSetup");

  return (
    <div style={glass}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <h2 style={{ fontSize: "1.15rem", fontWeight: 600, color: "#e9f4ff", margin: 0 }}>{t(lang, "hostedStatus.title")}</h2>
        <span style={{ fontSize: "0.8rem", fontWeight: 700, color: ws.color }}>{t(lang, ws.key)}</span>
      </div>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", lineHeight: 1.6, margin: "0.5rem 0 1.1rem" }}>{desc}</p>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(230px, 1fr))", gap: "0 1.5rem", marginBottom: "1.1rem" }}>
        {rows.map((r) => (
          <div
            key={r.label}
            style={{ display: "flex", justifyContent: "space-between", gap: 10, padding: "0.5rem 0", borderBottom: "1px solid rgba(148,163,184,0.10)" }}
          >
            <span style={{ fontSize: "0.82rem", color: "#94a3b8" }}>{r.label}</span>
            <span style={{ fontSize: "0.82rem", fontWeight: 600, color: r.color, textAlign: "right" }}>{r.value}</span>
          </div>
        ))}
      </div>

      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", alignItems: "center" }}>
        <Link href={primaryHref} style={primaryBtn}>{primaryLabel}</Link>
        {ready && (
          <Link href="/strategies/marketplace" style={{ color: "#4ab3ff", fontSize: "0.85rem", fontWeight: 600, textDecoration: "none" }}>
            {t(lang, "hostedStatus.chooseStrategy")}
          </Link>
        )}
      </div>

      {(canEnable || armed) && (
        <AutomatedTradingControl armed={armed} canEnable={canEnable} onAuthorized={onAuthorized} lang={lang} />
      )}

      <p style={{ margin: "1rem 0 0", fontSize: "0.78rem", color: "#64748b", lineHeight: 1.5 }}>
        {t(lang, "hostedStatus.privacy")}
      </p>
    </div>
  );
}

/**
 * ADR-0047 — the customer's EXPLICIT "Enable automated trading" control. This is the ONLY thing that permits
 * arming: a ready workspace is a CAPABILITY, never consent. The copy deliberately states the distinction and
 * uses NO internal terminology (no "EXECUTION_READY", "AutoTrading", "trade_allowed", "arming"). On success we
 * re-read authoritative state (default: reload) rather than trust the optimistic click.
 */
function AutomatedTradingControl({ armed, canEnable, onAuthorized, lang }: {
  armed: boolean;
  canEnable: boolean;
  onAuthorized?: () => void;
  lang: Lang;
}) {
  const [pending, setPending] = React.useState(false);
  const [error, setError] = React.useState("");

  const enable = React.useCallback(async () => {
    setPending(true);
    setError("");
    try {
      await authorizeExecution();
      // Never trust the click — re-read the server's authoritative state.
      if (onAuthorized) onAuthorized();
      else if (typeof window !== "undefined") window.location.reload();
    } catch {
      setError(t(lang, "hostedStatus.enableError"));
      setPending(false);
    }
  }, [lang, onAuthorized]);

  const box: React.CSSProperties = {
    marginTop: "1.1rem",
    borderRadius: 10,
    border: "1px solid rgba(74, 179, 255, 0.2)",
    background: "rgba(37, 99, 235, 0.06)",
    padding: "1rem 1.1rem",
  };

  if (armed) {
    return (
      <div style={box}>
        <div style={{ fontSize: "0.9rem", fontWeight: 600, color: "#22c55e" }}>{t(lang, "hostedStatus.automatedEnabled")}</div>
        <p style={{ margin: "0.35rem 0 0", fontSize: "0.85rem", color: "#b7c5dd", lineHeight: 1.6 }}>
          {t(lang, "hostedStatus.automatedEnabledBody")}
        </p>
      </div>
    );
  }

  if (!canEnable) return null;

  return (
    <div style={box}>
      <div style={{ fontSize: "0.9rem", fontWeight: 600, color: "#e9f4ff" }}>{t(lang, "hostedStatus.automated")}</div>
      <p style={{ margin: "0.35rem 0 0.9rem", fontSize: "0.85rem", color: "#b7c5dd", lineHeight: 1.6 }}>
        {t(lang, "hostedStatus.enableBody")}
      </p>
      <button
        type="button"
        onClick={enable}
        disabled={pending}
        style={{ ...primaryBtn, border: "none", cursor: pending ? "default" : "pointer", opacity: pending ? 0.7 : 1 }}
      >
        {pending ? t(lang, "common.enabling") : t(lang, "hostedStatus.enable")}
      </button>
      {error && (
        <p role="alert" style={{ margin: "0.6rem 0 0", fontSize: "0.8rem", color: "#f87171" }}>{error}</p>
      )}
    </div>
  );
}
