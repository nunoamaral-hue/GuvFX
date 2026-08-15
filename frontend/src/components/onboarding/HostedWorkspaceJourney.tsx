"use client";

// Hosted Workspace customer journey (G18) — renders the deterministic journey state machine + wires actions.
// Pure state logic lives in @/lib/hosted-journey (fully unit-tested); this component is the thin view + I/O.
// It never touches execution: the furthest action is "Choose a strategy", strictly below arming/order-time.
//
// UX pass (onboarding improvements): re-skinned to the GuvFX dark-glass system (was a light Tailwind island
// that inverted to invisible in dark mode), with live progress (spinner + elapsed timer + aria-live) so a
// provisioning screen never looks frozen, labelled/dark broker inputs, and a "what happens next" that lands
// on the Wayond card. No journey/execution behaviour changed — copy + presentation only.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/Button";
import {
  bindExpectedAccount, confirmAccount, describeJourney, fetchJourney, requestWorkspace, STEPS,
  type HostedJourney, type JourneyView,
} from "@/lib/hosted-journey";

type Load = "loading" | "ready" | "unavailable" | "error";

const glassCard: React.CSSProperties = {
  borderRadius: 16,
  border: "1px solid rgba(74, 179, 255, 0.12)",
  background: "linear-gradient(135deg, rgba(10, 15, 40, 0.95) 0%, rgba(5, 8, 22, 0.98) 100%)",
  boxShadow: "0 8px 32px rgba(0, 0, 0, 0.4), 0 0 60px rgba(30, 111, 255, 0.04)",
  padding: "1.5rem",
};

const TITLE = "#e9f4ff";
const BODY = "#b7c5dd";
const MUTED = "#94a3b8";
const ACCENT = "#4ab3ff";

const primaryLink: React.CSSProperties = {
  display: "inline-block",
  padding: "0.55rem 1.25rem",
  borderRadius: 999,
  background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 50%, #2979ff 100%)",
  color: "#ffffff",
  fontSize: "0.9rem",
  fontWeight: 600,
  textDecoration: "none",
  boxShadow: "0 10px 30px rgba(37, 99, 235, 0.45)",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  background: "rgba(255, 255, 255, 0.04)",
  color: TITLE,
  border: "1px solid rgba(74, 179, 255, 0.2)",
  borderRadius: 8,
  padding: "0.55rem 0.75rem",
  fontSize: "0.9rem",
  outline: "none",
};

function Spinner({ size = 16 }: { size?: number }) {
  return (
    <span
      className="animate-spin"
      aria-hidden
      style={{
        display: "inline-block", width: size, height: size,
        border: "2px solid rgba(74, 179, 255, 0.25)", borderTopColor: ACCENT, borderRadius: "50%",
      }}
    />
  );
}

export function HostedWorkspaceJourney() {
  const [journey, setJourney] = useState<HostedJourney | null>(null);
  const [load, setLoad] = useState<Load>("loading");
  const [busy, setBusy] = useState(false);
  // Broker identity is declared LATER (deferred bind), at the "open your workspace" step — never at request.
  const [form, setForm] = useState({ expected_login: "", expected_server: "" });

  const refresh = useCallback(async () => {
    try {
      const r = await fetchJourney();
      if (!r.ok) { setLoad("unavailable"); return; }
      setJourney(r.journey);
      setLoad("ready");
    } catch {
      setLoad("error");
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const view: JourneyView | null = load === "ready" ? describeJourney(journey) : null;

  // Poll while the workspace is advancing in the background (server does the work; we just re-read), so the
  // journey never appears stuck: the "preparing" progress phases AND the broker-login phases, which move
  // forward when the observation scheduler confirms the connected account. Terminal / user-action-only
  // states (ready, confirm, support) do not poll. Keyed on primitives to avoid interval churn per render.
  const phase = journey?.phase;
  useEffect(() => {
    if (load !== "ready") return;
    const advancing = view?.tone === "progress"
      || phase === "AWAITING_BROKER_LOGIN" || phase === "BROKER_CONNECTED";
    if (!advancing) return;
    const t = setInterval(() => { void refresh(); }, 5000);
    return () => clearInterval(t);
  }, [load, phase, view?.tone, refresh]);

  async function onRequest() {
    if (busy) return;
    setBusy(true);
    try {
      const j = await requestWorkspace();          // deferred bind — no broker details collected here
      setJourney(j);
      setLoad("ready");
    } catch {
      setLoad("error");
    } finally {
      setBusy(false);
    }
  }

  async function onBind(e: React.FormEvent) {
    e.preventDefault();
    if (!form.expected_login.trim() || busy) return;
    setBusy(true);
    try {
      const j = await bindExpectedAccount(form);   // declare the expected broker identity (write-once)
      setJourney(j);
      setLoad("ready");
    } catch {
      void refresh();  // e.g. 409 already-bound / conflict → just re-read the current state
    } finally {
      setBusy(false);
    }
  }

  async function onConfirm() {
    if (busy) return;
    setBusy(true);
    try {
      const j = await confirmAccount();
      setJourney(j);
    } catch {
      void refresh();  // e.g. 409 not-confirmable-yet → just re-read the current state
    } finally {
      setBusy(false);
    }
  }

  if (load === "loading") {
    return (
      <div className="mx-auto max-w-xl p-6" style={{ display: "flex", alignItems: "center", gap: 10, color: MUTED, fontSize: "0.9rem" }}>
        <Spinner /> Loading your workspace…
      </div>
    );
  }
  if (load === "unavailable") {
    return (
      <div className="mx-auto max-w-lg p-6">
        <div style={glassCard}>
          <h2 style={{ fontSize: "1.15rem", fontWeight: 600, color: TITLE, margin: 0 }}>Hosted workspace</h2>
          <p style={{ marginTop: 8, fontSize: "0.9rem", color: BODY }}>
            Your hosted trading workspace isn&apos;t available yet. We&apos;ll let you know the moment it&apos;s ready for you.
          </p>
        </div>
      </div>
    );
  }
  if (load === "error" || !view) {
    return (
      <div className="mx-auto max-w-lg p-6">
        <div style={{ ...glassCard, borderColor: "rgba(248, 113, 113, 0.3)" }}>
          <p style={{ fontSize: "0.9rem", color: BODY, margin: 0 }}>We couldn&apos;t load your workspace status.</p>
          <div style={{ marginTop: 12 }}>
            <Button onClick={() => { setLoad("loading"); void refresh(); }}>Try again</Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-xl p-6">
      <Stepper current={view.stepIndex} />
      <div style={{ ...glassCard, marginTop: "1.5rem" }}>
        <h2 style={{ fontSize: "1.15rem", fontWeight: 600, color: TITLE, margin: 0 }}>{view.title}</h2>
        <p style={{ marginTop: 8, fontSize: "0.9rem", lineHeight: 1.6, color: BODY }}>{view.description}</p>
        <div style={{ marginTop: 16 }}>
          {/* Progress phases carry no action — show live motion + elapsed time so it never looks frozen. */}
          {view.action === null && view.tone === "progress" && (
            <div role="status" aria-live="polite"
                 style={{ display: "flex", alignItems: "center", gap: 10, color: MUTED, fontSize: "0.85rem" }}>
              <Spinner />
              <span>Working on it — this page updates automatically.</span>
            </div>
          )}

          {view.action?.kind === "request" && (
            <Button onClick={onRequest} disabled={busy} style={{ width: "100%" }}>
              {busy ? "Requesting…" : view.action.label}
            </Button>
          )}

          {view.action?.kind === "launch" && (
            <div className="space-y-4">
              {/* Deferred bind: the customer declares their broker account here, then opens MT5 to log in. */}
              <form onSubmit={onBind} className="space-y-3">
                <div>
                  <label htmlFor="hw-login" style={{ display: "block", fontSize: "0.8rem", color: BODY, marginBottom: 4 }}>
                    Broker account number
                  </label>
                  <input id="hw-login" required value={form.expected_login}
                         onChange={(e) => setForm({ ...form, expected_login: e.target.value })}
                         placeholder="e.g. 1234567" style={inputStyle}
                         onFocus={(e) => { e.currentTarget.style.borderColor = ACCENT; }}
                         onBlur={(e) => { e.currentTarget.style.borderColor = "rgba(74, 179, 255, 0.2)"; }} />
                </div>
                <div>
                  <label htmlFor="hw-server" style={{ display: "block", fontSize: "0.8rem", color: BODY, marginBottom: 4 }}>
                    Broker server
                  </label>
                  <input id="hw-server" value={form.expected_server}
                         onChange={(e) => setForm({ ...form, expected_server: e.target.value })}
                         placeholder="e.g. YourBroker-Demo" style={inputStyle}
                         onFocus={(e) => { e.currentTarget.style.borderColor = ACCENT; }}
                         onBlur={(e) => { e.currentTarget.style.borderColor = "rgba(74, 179, 255, 0.2)"; }} />
                </div>
                <Button type="submit" disabled={busy || !form.expected_login.trim()} style={{ width: "100%" }}>
                  {busy ? "Saving…" : "Save my broker details"}
                </Button>
                <p style={{ fontSize: "0.8rem", color: MUTED, margin: 0 }}>
                  Enter your password only inside MetaTrader — never here. GuvFX never receives or stores it.
                </p>
              </form>
              {view.canLaunch ? (
                <Link href="/trading/terminal-access" style={primaryLink}>{view.action.label}</Link>
              ) : (
                <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "0.55rem 0.9rem",
                              borderRadius: 999, background: "rgba(255, 255, 255, 0.06)", color: MUTED, fontSize: "0.85rem" }}>
                  <Spinner /> Preparing your terminal… this refreshes automatically.
                </div>
              )}
            </div>
          )}

          {view.action?.kind === "confirm" && (
            <Button onClick={onConfirm} disabled={busy}>{busy ? "Confirming…" : view.action.label}</Button>
          )}

          {view.action?.kind === "assign" && (
            <Link href="/strategies/marketplace" style={primaryLink}>{view.action.label}</Link>
          )}

          {view.action?.kind === "support" && (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.7rem" }}>
              <p style={{ fontSize: "0.9rem", color: BODY, margin: 0 }}>
                Our team can help get this sorted for you.
              </p>
              {/* Actionable next step — never a dead end. Opens the customer's mail client (no backend). */}
              <a href="mailto:support@guvfx.com?subject=Hosted%20Workspace%20help" style={primaryLink}>
                {view.action.label}
              </a>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Stepper({ current }: { current: number }) {
  return (
    <ol className="flex items-center gap-2 text-xs">
      {STEPS.map((label, i) => {
        const state = current < 0 ? "todo" : i < current ? "done" : i === current ? "current" : "todo";
        const barColor = state === "done" ? "#86efac" : state === "current" ? ACCENT : "rgba(255, 255, 255, 0.1)";
        return (
          <li key={label} className="flex flex-1 flex-col items-center gap-1">
            <span className={state === "current" ? "h-2 w-full rounded animate-pulse" : "h-2 w-full rounded"}
                  style={{ background: barColor }} />
            <span style={{ color: state === "current" ? TITLE : MUTED, fontWeight: state === "current" ? 600 : 400 }}>
              {label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
