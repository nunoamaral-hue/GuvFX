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

// AJ#3 UX: how long the "preparing your workspace" wait may run before we swap the reassuring copy for a
// "taking longer than expected" message — so the customer is never left staring at an endless spinner.
const SLOW_WAIT_MS = 120_000;

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

const waitCard: React.CSSProperties = {
  borderRadius: 12,
  border: "1px solid rgba(74, 179, 255, 0.15)",
  background: "rgba(255, 255, 255, 0.03)",
  padding: "0.9rem 1rem",
};

// AJ#3 UX: one row of the "what's happening" preparation timeline. done → green ✓, current → accent ● (gently
// pulsing so the page feels alive), else muted ○. Purely presentational (NO invented percentages, NO timings,
// NO new state) driven by the props above.
function PrepStep({ done, current, children }: { done?: boolean; current?: boolean; children: React.ReactNode }) {
  const color = done ? "#5fd39a" : current ? ACCENT : MUTED;
  const marker = done ? "✓" : current ? "●" : "○";
  return (
    <li style={{ display: "flex", alignItems: "center", gap: 10, fontSize: "0.85rem", color }}>
      <span aria-hidden className={current ? "animate-pulse" : undefined}
            style={{ width: 14, textAlign: "center", fontSize: current ? "0.7rem" : undefined }}>{marker}</span>
      <span style={current ? { fontWeight: 600 } : undefined}>{children}</span>
    </li>
  );
}

// AJ#3 waiting-experience redesign — the page OWNS the customer after "Save my broker details": no form, no
// competing buttons, one instruction ("remain on this page"), an active preparation timeline, and an automatic
// hand-off to the single "Open MetaTrader" action when ready. Presentation only.
function WaitingPanel({ slow }: { slow: boolean }) {
  return (
    <div role="status" aria-live="polite" style={{ ...waitCard, padding: "1.15rem 1.2rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "0.95rem", fontWeight: 600, color: "#5fd39a" }}>
        <span aria-hidden>✓</span> Broker account linked
      </div>
      <p style={{ marginTop: 10, fontSize: "0.9rem", lineHeight: 1.65, color: BODY }}>
        {slow
          ? "This is taking a little longer than expected. Your workspace is still being prepared. Please remain on this "
            + "page — we'll automatically continue as soon as everything is ready. If preparation still doesn't complete "
            + "after several more minutes, you may safely refresh this page or contact support."
          : "We've received your broker account information. We're now setting up your secure MetaTrader workspace and "
            + "connecting it to your broker account. You don't need to do anything else right now — please remain on this "
            + "page, and we'll automatically continue when everything is ready."}
      </p>
      <div style={{ marginTop: 14, height: 1, background: "rgba(74, 179, 255, 0.12)" }} />
      <ul style={{ listStyle: "none", margin: "14px 0 0", padding: 0, display: "flex", flexDirection: "column", gap: 11 }}>
        <PrepStep done>Workspace requested</PrepStep>
        <PrepStep done>Broker account linked</PrepStep>
        <PrepStep current>Setting up your secure MetaTrader workspace</PrepStep>
        <PrepStep>Final verification</PrepStep>
      </ul>
      {/* AJ#3 (Sponsor 2026-08-16): a final, explicit instruction — stay on THIS page; the single next action
          (Open MetaTrader) will appear HERE automatically the moment the workspace is ready. No navigation, no
          refresh, no second step to find. */}
      <p style={{ marginTop: 14, fontSize: "0.85rem", lineHeight: 1.55, color: BODY }}>
        Please keep this page open — the{" "}
        <strong style={{ color: TITLE, fontWeight: 600 }}>Open MetaTrader</strong> button will appear here
        automatically as soon as your workspace is ready.
      </p>
      {/* The declare form (with its own password-safety note) is gone once the account is linked, so carry a
          short password reassurance INTO the waiting state — the customer never types a password here; it is
          entered later, only inside MetaTrader. */}
      <p style={{ marginTop: 10, fontSize: "0.8rem", lineHeight: 1.5, color: MUTED }}>
        {"You'll enter your broker password later inside MetaTrader. We never ask for it here."}
      </p>
    </div>
  );
}

function ReadyPanel() {
  return (
    <div style={{ ...waitCard, padding: "1.15rem 1.2rem", borderColor: "rgba(95, 211, 154, 0.28)",
                  background: "rgba(95, 211, 154, 0.05)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "1rem", fontWeight: 700, color: "#5fd39a" }}>
        <span aria-hidden>✓</span> Your MetaTrader workspace is ready
      </div>
      <p style={{ marginTop: 10, fontSize: "0.9rem", lineHeight: 1.65, color: BODY }}>
        Everything has been prepared. You can now open MetaTrader and log in using your broker password.
      </p>
      <div style={{ marginTop: 16 }}>
        <Link href="/trading/terminal-access" style={primaryLink}>Open MetaTrader</Link>
      </div>
    </div>
  );
}

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
  // AJ#3 UX: after the normal preparation window, flip to a "taking longer than expected" reassurance so the
  // customer is never staring at an endless spinner.
  const [slowWait, setSlowWait] = useState(false);

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

  // AJ#3 (Sponsor 2026-08-16): the SERVER is the single source of truth for whether the customer's broker
  // identity is already recorded (write-once bind → onboarding_read_model.identity_declared). This replaces the
  // old local `savedAck` flag, which was lost on refresh and briefly re-showed the declaration form on reload.
  // Being server-derived, it is deterministic across reloads, devices and resumed sessions — once declared, the
  // declaration form is never shown again, with no reliance on a 409 in the normal flow.
  const identityDeclared = journey?.identity_declared === true;

  useEffect(() => {
    if (load !== "ready") return;
    const advancing = view?.tone === "progress"
      || phase === "AWAITING_BROKER_LOGIN" || phase === "BROKER_CONNECTED";
    if (!advancing) return;
    const t = setInterval(() => { void refresh(); }, 5000);
    return () => clearInterval(t);
  }, [load, phase, view?.tone, refresh]);

  // AJ#3 UX: once the customer has LINKED (identity declared) and is waiting for the slot to become openable,
  // start a one-shot timer; after SLOW_WAIT_MS show the "taking longer than expected" copy. Gated on the linked
  // + genuine-waiting state so the countdown is anchored to the link event — NOT to time spent on the declare
  // form (otherwise a slow form-filler would see "taking longer than expected" the instant they link). Resets the
  // moment the wait ends (it becomes openable). Presentation only — no polling / lifecycle change.
  const waiting = view?.action?.kind === "launch" && identityDeclared
    && phase === "AWAITING_BROKER_LOGIN" && !view.canLaunch;
  useEffect(() => {
    if (!waiting) { setSlowWait(false); return; }
    const t = setTimeout(() => setSlowWait(true), SLOW_WAIT_MS);
    return () => clearTimeout(t);
  }, [waiting]);

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
      setJourney(j);                               // the response carries identity_declared=true → form hides
      setLoad("ready");
    } catch {
      // The journey's server-derived identity_declared is the source of truth, so on ANY error just re-read it.
      // If the identity was already recorded (write-once), the refreshed journey has identity_declared=true and
      // the waiting/ready panel owns the page — a 409 is no longer a special UX signal.
      void refresh();
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

  // AJ#3: the waiting/ready panel OWNS the page — no top stepper, no generic header, no form, no competing
  // navigation — ONLY in the genuine waiting state: the customer has linked their account (identityDeclared) AND
  // is still at AWAITING_BROKER_LOGIN. Later launch phases (e.g. BROKER_CONNECTED = wrong account logged in) keep
  // the normal header so their corrective guidance ("switch to the account you told us") stays visible.
  const owns = phase === "AWAITING_BROKER_LOGIN" && identityDeclared;

  return (
    <div className="mx-auto max-w-xl p-6">
      {!owns && <Stepper current={view.stepIndex} />}
      <div style={{ ...glassCard, marginTop: owns ? 0 : "1.5rem" }}>
        {!owns && (
          <>
            <h2 style={{ fontSize: "1.15rem", fontWeight: 600, color: TITLE, margin: 0 }}>{view.title}</h2>
            <p style={{ marginTop: 8, fontSize: "0.9rem", lineHeight: 1.6, color: BODY }}>{view.description}</p>
          </>
        )}
        <div style={{ marginTop: owns ? 0 : 16 }}>
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
            phase === "AWAITING_BROKER_LOGIN" ? (
            !identityDeclared ? (
              // DECLARE — enter broker details + save. Shown until the SERVER records the identity (write-once):
              // the one launch sub-state where the page is NOT owned by the waiting panel, because the customer
              // still has a real action (deferred bind: declare, then open MT5).
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
            ) : view.canLaunch ? (
              // READY — the workspace is openable. The waiting panel disappears; a single "Open MetaTrader" action.
              <ReadyPanel />
            ) : (
              // WAITING — the page OWNS the customer: account linked, active preparation timeline, one instruction
              // ("remain on this page"), automatic continuation. Swaps to a longer-wait reassurance after SLOW_WAIT_MS.
              <WaitingPanel slow={slowWait} />
            )
            ) : (
              // A LATER launch phase, identity already declared — e.g. BROKER_CONNECTED (the WRONG broker account
              // is logged in). The corrective header above (view.title/description) stays visible; offer one action
              // to open MetaTrader and switch to the account they told us. Never re-shows the declaration form.
              <Link href="/trading/terminal-access" style={primaryLink}>{view.action.label}</Link>
            )
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
