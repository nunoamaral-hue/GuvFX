"use client";

import React from "react";
import Link from "next/link";
import { describeJourney, type HostedJourney } from "@/lib/hosted-journey";

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
const WORKSPACE_STATUS: Record<string, { label: string; color: string }> = {
  NO_WORKSPACE: { label: "Not set up yet", color: "#94a3b8" },
  WORKSPACE_REQUESTED: { label: "Preparing", color: "#38bdf8" },
  WORKSPACE_PREPARING: { label: "Preparing", color: "#38bdf8" },
  AWAITING_BROKER_LOGIN: { label: "Waiting for you to log in", color: "#f59e0b" },
  BROKER_CONNECTED: { label: "Waiting for you to log in", color: "#f59e0b" },
  ACCOUNT_CONFIRMATION_REQUIRED: { label: "Confirm your account", color: "#f59e0b" },
  ACCOUNT_BOUND: { label: "Finishing up", color: "#38bdf8" },
  WORKSPACE_READY: { label: "Ready", color: "#22c55e" },
  WORKSPACE_UNAVAILABLE: { label: "Needs attention", color: "#f87171" },
};

const DELIVERY_STATUS: Record<string, { label: string; color: string }> = {
  DELIVERY_READY: { label: "Ready to open", color: "#22c55e" },
  DELIVERY_PREPARING: { label: "Preparing", color: "#38bdf8" },
  DELIVERY_NOT_AVAILABLE: { label: "Not ready yet", color: "#94a3b8" },
  DELIVERY_EXTERNAL_GATE: { label: "Action needed", color: "#f59e0b" },
};

// Status-oriented copy for THIS read-only page. The interactive journey's own description is imperative
// ("Enter your broker account number…", "confirm it…"), which would tell the customer to do something they
// cannot do here — the actual controls live in the hosted flow the primary CTA opens. So we describe the
// state and point at that CTA, never instruct an action this page has no field/button for.
const STATUS_DESC: Record<string, string> = {
  NO_WORKSPACE: "You don't have a hosted workspace yet. Continue setup to get a managed MetaTrader terminal you log in to.",
  WORKSPACE_REQUESTED: "We're preparing your private MetaTrader workspace. This usually completes within a few minutes.",
  WORKSPACE_PREPARING: "We're building your private, isolated MetaTrader workspace. This usually completes within a few minutes.",
  AWAITING_BROKER_LOGIN: "Your workspace is ready. Continue setup to point it at your broker account and log in — inside MetaTrader, never here.",
  BROKER_CONNECTED: "Your workspace is open. Continue setup to finish logging in to the right account.",
  ACCOUNT_CONFIRMATION_REQUIRED: "We found your broker account. Continue setup to confirm it and finish.",
  ACCOUNT_BOUND: "Your account is confirmed — we're finishing the last step.",
  WORKSPACE_READY: "Your hosted MT5 workspace is connected and ready. Choose a strategy to get started.",
  WORKSPACE_UNAVAILABLE: "Your hosted workspace isn't available right now. Our team can help get it back.",
};

function maskNumber(n?: string | null): string {
  const s = (n || "").trim();
  if (!s) return "";
  return s.length <= 3 ? s : "•••" + s.slice(-3);
}

export function HostedWorkspaceStatus({ journey, accounts }: { journey: HostedJourney; accounts: Acct[] }) {
  const view = describeJourney(journey);
  const desc = STATUS_DESC[journey.phase] ?? view.description;
  const ws = WORKSPACE_STATUS[journey.phase] ?? { label: "In progress", color: "#38bdf8" };
  const del = DELIVERY_STATUS[journey.delivery] ?? { label: "Preparing", color: "#38bdf8" };
  const active = accounts.find((a) => a.is_active) ?? accounts[0];
  const brokerDetected = (journey.active_login_masked || "").trim();
  const ready = journey.phase === "WORKSPACE_READY";

  // "Trading readiness" is reported at the ASSIGNMENT tier (strategy_eligible), strictly below arming/order
  // authority — never implying it is safe to send an order.
  const readinessValue = journey.strategy_eligible
    ? "Ready — choose a strategy"
    : ready
      ? "Ready"
      : "Setting up";

  const rows: Array<{ label: string; value: string; color: string }> = [
    { label: "Workspace status", value: ws.label, color: ws.color },
    { label: "MetaTrader terminal", value: del.label, color: del.color },
    { label: "Broker account", value: brokerDetected || "Not yet", color: brokerDetected ? "#e5f4ff" : "#94a3b8" },
    { label: "Account type", value: active ? (active.is_demo === false ? "Live" : "Demo") : "—", color: "#e5f4ff" },
    {
      label: "Active account",
      value: active
        ? (active.name || `#${active.id}`) + (maskNumber(active.account_number) ? ` · ${maskNumber(active.account_number)}` : "")
        : "Not connected yet",
      color: active ? "#e5f4ff" : "#94a3b8",
    },
    { label: "Trading readiness", value: readinessValue, color: journey.strategy_eligible ? "#22c55e" : "#94a3b8" },
  ];

  // ONE clear next action, always. Ready → open the terminal; otherwise → continue the hosted journey.
  const primaryHref = ready ? "/trading/terminal-access" : "/onboarding/hosted";
  const primaryLabel = ready ? "Open MetaTrader" : "Continue setup";

  return (
    <div style={glass}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <h2 style={{ fontSize: "1.15rem", fontWeight: 600, color: "#e9f4ff", margin: 0 }}>Hosted Workspace</h2>
        <span style={{ fontSize: "0.8rem", fontWeight: 700, color: ws.color }}>{ws.label}</span>
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
            Choose a strategy →
          </Link>
        )}
      </div>

      <p style={{ margin: "1rem 0 0", fontSize: "0.78rem", color: "#64748b", lineHeight: 1.5 }}>
        GuvFX runs MetaTrader for you — you log in inside it, and we never see or store your broker password.
      </p>
    </div>
  );
}
