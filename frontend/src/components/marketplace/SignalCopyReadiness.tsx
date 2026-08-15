"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { t, type Lang } from "@/lib/i18n";
import { Button } from "@/components/ui/Button";

/** WS-D (packet: Customer Journey Consolidation) — the customer readiness panel that replaces the opaque
 * "Not armed" hint on the signal-copy marketplace card. It renders a ✓/✕ checklist and ONE customer-safe
 * next action for the selected demo account, backed by the read-only /signal-copy/readiness endpoint
 * (which mirrors the arm gates, so the panel can never claim readiness the backend would refuse). The
 * backend returns only machine keys/codes; every visible string is chosen here from i18n — no runtime,
 * model, or backend terminology reaches the customer. The Enable-Trading button appears only when the
 * broker-connectivity journey is built (armUiEnabled) AND the backend says the account can_arm. */

type Check = { key: string; ok: boolean };
type Readiness = {
  state: string;
  armed: boolean;
  enabled: boolean;
  can_arm: boolean;
  checklist: Check[];
  next_action: string;
  account_id: number;
};
export type ReadinessAccount = { id: number; name?: string | null; is_demo?: boolean };

const CHECK_LABEL: Record<string, string> = {
  demo: "marketplace.readinessCheckDemo",
  active: "marketplace.readinessCheckActive",
  credentials: "marketplace.readinessCheckCredentials",
  runtime_ready: "marketplace.readinessCheckRuntime",
  pilot_access: "marketplace.readinessCheckAccess",
};

// next-action code → i18n key. Reuses the arm-error keys where the wording is identical, so a permanent
// deny (e.g. request_access) reads the same whether it is pre-empted here or surfaced by the arm toast.
const NEXT_KEY: Record<string, string> = {
  closed: "marketplace.readinessNextClosed",
  attention_validation: "marketplace.armValidationUnhealthy",
  attention_paused: "marketplace.armPaused",
  attention_duplicate: "marketplace.armDuplicate",
  add_demo_account: "marketplace.readinessNextAddDemo",
  activate_account: "marketplace.readinessNextActivate",
  add_credentials: "marketplace.armCredentialsMissing",
  preparing: "marketplace.armRuntimeNotReady",
  connecting: "marketplace.armBrokerNotConnected",
  single_tenant: "marketplace.armSingleTenant",
  trading_on: "marketplace.readinessNextTradingOn",
  request_access: "marketplace.armNotPilotApproved",
  enable_to_resume: "marketplace.readinessNextResume",
  ready_enable: "marketplace.readinessNextReady",
};

// When prerequisites are incomplete, each next-action maps to a navigation button that takes the customer
// to where they fix it — so a card is never a status-only dead end (states with no customer-navigable fix,
// e.g. closed / pilot-access, keep the explanatory line only).
const NEXT_NAV: Record<string, { href: string; labelKey: string }> = {
  add_demo_account: { href: "/accounts", labelKey: "marketplace.navGoAccounts" },
  activate_account: { href: "/accounts", labelKey: "marketplace.navActivateAccount" },
  add_credentials: { href: "/onboarding/hosted", labelKey: "marketplace.navFinishWorkspace" },
  preparing: { href: "/onboarding/hosted", labelKey: "marketplace.navFinishWorkspace" },
  connecting: { href: "/onboarding/hosted", labelKey: "marketplace.navFinishWorkspace" },
  attention_validation: { href: "/accounts", labelKey: "marketplace.navGoAccounts" },
  attention_paused: { href: "/accounts", labelKey: "marketplace.navGoAccounts" },
  attention_duplicate: { href: "/accounts", labelKey: "marketplace.navGoAccounts" },
  // P0.2 — no readiness state may be status-only: give the remaining navigable denials a destination too.
  single_tenant: { href: "/accounts", labelKey: "marketplace.navGoAccounts" },
  trading_on: { href: "/strategies", labelKey: "marketplace.navViewStrategies" },
  enable_to_resume: { href: "/strategies", labelKey: "marketplace.navViewStrategies" },
};

// Shared pill style for a navigation next-action (a Link that moves the customer to where they fix things).
const navBtnStyle: React.CSSProperties = {
  display: "inline-block",
  alignSelf: "flex-start",
  padding: "0.5rem 1.1rem",
  borderRadius: 999,
  background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 50%, #2979ff 100%)",
  color: "#ffffff",
  fontSize: "0.8rem",
  fontWeight: 600,
  textDecoration: "none",
  boxShadow: "0 8px 24px rgba(37,99,235,0.4)",
};

const STATE_COLOR: Record<string, string> = {
  READY: "#22c55e",
  TRADING_ON: "#22c55e",
  PREPARING: "#38bdf8",
  CONNECTING: "#38bdf8",
  SETUP_INCOMPLETE: "#f59e0b",
  NEEDS_ATTENTION: "#f59e0b",
  CLOSED: "#94a3b8",
};

export function SignalCopyReadiness({
  lang,
  marketplaceStrategyId,
  accounts,
  selectedAccountId,
  onSelectAccount,
  armUiEnabled,
  isAuthed,
  arming,
  onArm,
}: {
  lang: Lang;
  marketplaceStrategyId: string;
  accounts: ReadinessAccount[];
  selectedAccountId: number | "" | undefined;
  onSelectAccount: (v: number | "") => void;
  armUiEnabled: boolean;
  isAuthed: boolean;
  arming: boolean;
  onArm: (accountId: number) => void;
}) {
  // Only demo accounts are eligible for the copy path (matches the backend's demo-only rule), so we never
  // offer an account the arm endpoint would reject.
  const demoAccounts = accounts.filter((a) => a.is_demo !== false);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  const selId = typeof selectedAccountId === "number" ? selectedAccountId : Number(selectedAccountId);

  // Auto-select the first demo account so the panel always has something to describe.
  useEffect(() => {
    if ((selectedAccountId === "" || selectedAccountId == null) && demoAccounts.length > 0) {
      onSelectAccount(demoAccounts[0].id);
    }
  }, [selectedAccountId, demoAccounts, onSelectAccount]);

  const load = useCallback(async (accountId: number) => {
    setLoading(true);
    setFailed(false);
    try {
      const data = await apiFetch<Readiness>(
        "/api/strategies/strategies/signal-copy/readiness/?marketplace_strategy_id=" +
          encodeURIComponent(marketplaceStrategyId) + "&account_id=" + accountId,
      );
      setReadiness(data);
    } catch {
      // Read-only status fetch: on failure show a neutral "unavailable", never a false "not ready".
      setReadiness(null);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, [marketplaceStrategyId]);

  useEffect(() => {
    if (Number.isFinite(selId) && selId > 0) void load(selId);
  }, [selId, load]);

  if (demoAccounts.length === 0) {
    return (
      <div style={{ fontSize: "0.72rem", color: "#94a3b8" }}>
        <p style={{ margin: "0 0 6px" }}>{t(lang, "marketplace.readinessNoDemo")}</p>
        <Link href="/accounts" style={{ color: "#93c5fd", textDecoration: "none" }}>
          {t(lang, "marketplace.readinessAddAccount")} →
        </Link>
      </div>
    );
  }

  const accentColor = readiness ? STATE_COLOR[readiness.state] || "#94a3b8" : "#94a3b8";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
      {demoAccounts.length > 1 && (
        <select
          value={selId > 0 ? selId : ""}
          onChange={(e) => onSelectAccount(e.target.value ? Number(e.target.value) : "")}
          aria-label={t(lang, "marketplace.selectAccount")}
          style={{
            width: "100%", padding: "0.5rem", borderRadius: 8,
            border: "1px solid rgba(255,255,255,0.15)", background: "rgba(10,16,35,0.6)",
            color: "#e2e8f0", fontSize: "0.85rem",
          }}
        >
          {demoAccounts.map((a) => (
            <option key={a.id} value={a.id}>{a.name || `#${a.id}`}</option>
          ))}
        </select>
      )}

      {loading && !readiness ? (
        <p style={{ fontSize: "0.72rem", color: "#64748b", margin: 0 }}>{t(lang, "marketplace.readinessLoading")}</p>
      ) : failed ? (
        // P0.2: a failed status fetch is not a dead end — offer a retry so the customer has a next action.
        <div style={{ display: "flex", flexDirection: "column", gap: "0.45rem" }}>
          <p style={{ fontSize: "0.72rem", color: "#94a3b8", margin: 0 }}>{t(lang, "marketplace.readinessUnavailable")}</p>
          <button
            type="button"
            onClick={() => { if (selId > 0) void load(selId); }}
            style={{
              alignSelf: "flex-start", padding: "0.35rem 0.9rem", borderRadius: 999,
              border: "1px solid rgba(148,163,184,0.35)", background: "rgba(148,163,184,0.12)",
              color: "#cbd5e1", fontSize: "0.75rem", fontWeight: 600, cursor: "pointer",
            }}
          >
            {t(lang, "marketplace.readinessRetry")}
          </button>
        </div>
      ) : readiness ? (
        <>
          <div style={{ background: "rgba(0,0,0,0.2)", borderRadius: 8, padding: "0.5rem 0.65rem" }}>
            <div style={{ fontSize: "0.72rem", textTransform: "uppercase", letterSpacing: "0.04em", color: "#94a3b8", marginBottom: 6 }}>
              {t(lang, "marketplace.readinessTitle")}
            </div>
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: 3 }}>
              {readiness.checklist.map((c) => (
                <li key={c.key} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: "0.74rem", color: c.ok ? "#e2e8f0" : "#94a3b8" }}>
                  <span aria-hidden style={{ color: c.ok ? "#22c55e" : "#f59e0b", fontWeight: 700 }}>{c.ok ? "✓" : "✕"}</span>
                  <span>{t(lang, CHECK_LABEL[c.key] || c.key)}</span>
                </li>
              ))}
            </ul>
          </div>
          <p style={{ fontSize: "0.72rem", color: accentColor, margin: 0 }}>
            {t(lang, NEXT_KEY[readiness.next_action] || "marketplace.readinessNextReady")}
          </p>
          {/* Item 5: the card always points at ONE next action, never status-only.
              - can_arm + arm UI built → the operational Enable button.
              - can_arm + arm UI dark → no control (the next-action line reads "…ready. Enable trading…");
                a DARK build never surfaces a live arm path.
              - incomplete with a customer-navigable fix → a navigation button to where they fix it.
              - incomplete with no in-app fix (e.g. pilot access) → the goal button, disabled, under the
                explanatory next-action line above. */}
          {readiness.can_arm ? (
            armUiEnabled ? (
              // Complete + arm UI built → the operational Enable button.
              <Button
                variant="primary"
                onClick={() => (selId > 0 ? onArm(selId) : undefined)}
                disabled={!isAuthed || arming}
              >
                {arming ? t(lang, "marketplace.armWorking") : t(lang, "marketplace.armEnableTrading")}
              </Button>
            ) : (
              // Complete + arm UI DARK → the self-serve arm control isn't built yet, but the card must still
              // offer ONE next action (P0.2). Send the ready customer to their hosted workspace — a navigation
              // Link, never a live arm control, so a DARK build never surfaces an arm path.
              <Link href="/onboarding/hosted" style={navBtnStyle}>
                {t(lang, "marketplace.navOpenWorkspace")}
              </Link>
            )
          ) : NEXT_NAV[readiness.next_action] ? (
            // Incomplete but customer-navigable → a navigation button to where they fix it.
            <Link href={NEXT_NAV[readiness.next_action].href} style={navBtnStyle}>
              {t(lang, NEXT_NAV[readiness.next_action].labelKey)}
            </Link>
          ) : (
            // Incomplete with no in-app destination (e.g. pilot access / closed) → the goal button, disabled
            // (never live — no onArm), under the explanatory next-action line above. Shown in BOTH flag states
            // so the card is never status-only.
            <Button variant="primary" onClick={() => undefined} disabled>
              {t(lang, "marketplace.armEnableTrading")}
            </Button>
          )}
        </>
      ) : null}
    </div>
  );
}
