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
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { HostedMt5RemoteApp } from "@/components/hosted/HostedMt5RemoteApp";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";
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

// AJ#4: de-emphasised secondary action (e.g. "Open MetaTrader" on the ready page) — clearly subordinate to the
// single primary CTA so each step still reads as ONE obvious action.
const secondaryButton: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "0.5rem 1.1rem",
  borderRadius: 999,
  background: "transparent",
  color: ACCENT,
  fontSize: "0.85rem",
  fontWeight: 600,
  border: "1px solid rgba(74, 179, 255, 0.4)",
  cursor: "pointer",
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
  const lang = useLang();
  return (
    <div role="status" aria-live="polite" style={{ ...waitCard, padding: "1.15rem 1.2rem" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "0.95rem", fontWeight: 600, color: "#5fd39a" }}>
        <span aria-hidden>✓</span> {t(lang, "hostedJourney.brokerLinked")}
      </div>
      <p style={{ marginTop: 10, fontSize: "0.9rem", lineHeight: 1.65, color: BODY }}>
        {t(lang, slow ? "hostedJourney.waitSlow" : "hostedJourney.waitNormal")}
      </p>
      <div style={{ marginTop: 14, height: 1, background: "rgba(74, 179, 255, 0.12)" }} />
      <ul style={{ listStyle: "none", margin: "14px 0 0", padding: 0, display: "flex", flexDirection: "column", gap: 11 }}>
        <PrepStep done>{t(lang, "hostedJourney.workspaceRequested")}</PrepStep>
        <PrepStep done>{t(lang, "hostedJourney.brokerLinked")}</PrepStep>
        <PrepStep current>{t(lang, "hostedJourney.settingUp")}</PrepStep>
        <PrepStep>{t(lang, "hostedJourney.finalVerification")}</PrepStep>
      </ul>
      {/* AJ#3 (Sponsor 2026-08-16): a final, explicit instruction — stay on THIS page; the single next action
          (Open MetaTrader) will appear HERE automatically the moment the workspace is ready. No navigation, no
          refresh, no second step to find. */}
      <p style={{ marginTop: 14, fontSize: "0.85rem", lineHeight: 1.55, color: BODY }}>
        {t(lang, "hostedJourney.keepOpen")}
      </p>
      {/* The declare form (with its own password-safety note) is gone once the account is linked, so carry a
          short password reassurance INTO the waiting state — the customer never types a password here; it is
          entered later, only inside MetaTrader. */}
      <p style={{ marginTop: 10, fontSize: "0.8rem", lineHeight: 1.5, color: MUTED }}>
        {t(lang, "hostedJourney.passwordLater")}
      </p>
    </div>
  );
}

// AJ#5.1 — LIVE STATUS wizard. The ordered customer-facing stages of the login→ready arc. Purely derived from
// genuine journey state (phase + whether a broker login has been observed) — NO percentages, NO fake progress
// bars, NO invented timings. The active stage is highlighted; completed stages stay visible with a ✓.
const WIZARD_STAGES = [
  "hostedJourney.brokerLinked",
  "hostedJourney.waitingLogin",
  "hostedJourney.detecting",
  "hostedJourney.accountDetected",
  "hostedJourney.confirmAccount",
  "hostedJourney.workspaceReady",
] as const;

/** Index of the CURRENTLY ACTIVE stage (stages before it are done, after it are pending). 6 = every stage done.
 *  Pure: derived only from the real journey — no timers, no guesses. */
export function deriveStageIndex(j: HostedJourney | null | undefined): number {
  const hasLogin = !!(j?.active_login_masked || "").trim();
  switch (j?.phase) {
    case "WORKSPACE_READY": return 6;                 // all six stages complete
    case "ACCOUNT_CONFIRMATION_REQUIRED": return 4;   // "Confirm your account" active (detected is done)
    case "BROKER_CONNECTED": return 2;                // detecting — a login is present but not yet the right one
    case "AWAITING_BROKER_LOGIN": return hasLogin ? 2 : 1;  // detecting once a login appears, else waiting for it
    default: return 0;                                // linked / earlier prep
  }
}

// The persistent wizard rail. Completed steps keep a green ✓; the active step pulses in accent; pending steps
// are muted ○. Presentation only.
function LiveStatusPanel({ activeIndex }: { activeIndex: number }) {
  const lang = useLang();
  return (
    <ol aria-label={t(lang, "hostedJourney.progressAria")} style={{ listStyle: "none", margin: 0, padding: 0,
                                                   display: "flex", flexDirection: "column", gap: 9 }}>
      {WIZARD_STAGES.map((label, i) => {
        const status = i < activeIndex ? "done" : i === activeIndex ? "active" : "todo";
        const color = status === "done" ? "#5fd39a" : status === "active" ? ACCENT : MUTED;
        const marker = status === "done" ? "✓" : status === "active" ? "●" : "○";
        return (
          <li key={label} aria-current={status === "active" ? "step" : undefined}
              style={{ display: "flex", alignItems: "center", gap: 10, fontSize: "0.85rem", color }}>
            <span aria-hidden className={status === "active" ? "animate-pulse" : undefined}
                  style={{ width: 14, textAlign: "center", fontSize: status === "active" ? "0.7rem" : undefined }}>{marker}</span>
            <span style={status === "active" ? { fontWeight: 600 } : undefined}>{t(lang, label)}</span>
          </li>
        );
      })}
    </ol>
  );
}

// AJ#4/AJ#5: the "Open MetaTrader" step — MetaTrader is EMBEDDED right here in onboarding (the RemoteApp
// component, reused verbatim), so the customer logs in without ever leaving the journey or discovering Terminal
// Access. The onboarding page keeps polling in the background; trusted observation moves us on automatically.
// AJ#5.1 (P1/P2/P5): the step now LEADS with the live status wizard, states plainly what GuvFX is doing / what
// the customer should do / what happens next, and WRAPS the terminal inside the same panel (accent-bordered
// "You're using MetaTrader" frame) so it reads as one guided step, never a separate app. Presentation only.
function EmbeddedMetaTraderStep({
  activeIndex, title = "Open MetaTrader", instruction, hint, detecting = false, children,
}: { activeIndex: number; title?: string; instruction?: string; hint?: string; detecting?: boolean; children: React.ReactNode }) {
  const lang = useLang();
  return (
    <div style={{ ...waitCard, padding: "1.25rem 1.35rem" }}>
      <LiveStatusPanel activeIndex={activeIndex} />
      <div style={{ marginTop: 16, height: 1, background: "rgba(74, 179, 255, 0.12)" }} />
      <h3 style={{ margin: "16px 0 0", fontSize: "1.1rem", fontWeight: 700, color: TITLE }}>{title}</h3>
      <p style={{ marginTop: 8, fontSize: "0.9rem", lineHeight: 1.6, color: BODY }}>
        {instruction
          ?? t(lang, "hostedJourney.loginInstruction")}
      </p>
      {/* P4/P5: a distinct, intentional MetaTrader frame (accent border + glow) with a "You're using MetaTrader"
          label wrapping the terminal, so it's unmistakably part of onboarding — not a separate app bolted on. */}
      <div style={{ marginTop: 14, borderRadius: 14, border: "1px solid rgba(74, 179, 255, 0.35)",
                    boxShadow: "0 0 0 3px rgba(74, 179, 255, 0.06)", overflow: "hidden", background: "rgba(0,0,0,0.15)" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
                      padding: "0.5rem 0.95rem", background: "rgba(74, 179, 255, 0.08)",
                      borderBottom: "1px solid rgba(74, 179, 255, 0.18)" }}>
          <span style={{ fontSize: "0.82rem", fontWeight: 700, color: TITLE }}>{t(lang, "hostedJourney.usingMetaTrader")}</span>
          {detecting && (
            <span role="status" aria-live="polite"
                  style={{ display: "flex", alignItems: "center", gap: 7, fontSize: "0.78rem", color: ACCENT }}>
              <Spinner size={12} /> {t(lang, "hostedJourney.detectingAccount")}
            </span>
          )}
        </div>
        <div>{children}</div>
      </div>
      {hint && (
        <p style={{ marginTop: 10, fontSize: "0.82rem", lineHeight: 1.5, color: MUTED }}>{hint}</p>
      )}
    </div>
  );
}

// AJ#4 (PP5): the manual confirmation is RETAINED as the explicit customer activation step — but identity is
// already proven by trusted observation, so it reads "I confirm this is my trading account", not "prove who you
// are". Rendered inside onboarding (never a detour to Broker Accounts). Heading is a real <h2> for a11y.
function ConfirmAccountPanel({
  activeIndex, maskedLogin, busy, onConfirm,
}: { activeIndex: number; maskedLogin?: string; busy: boolean; onConfirm: () => void }) {
  const acct = (maskedLogin || "").trim();
  const lang = useLang();
  return (
    <div style={{ ...waitCard, padding: "1.25rem 1.35rem", borderColor: "rgba(95, 211, 154, 0.28)",
                  background: "rgba(95, 211, 154, 0.05)" }}>
      <LiveStatusPanel activeIndex={activeIndex} />
      <div style={{ margin: "16px 0", height: 1, background: "rgba(74, 179, 255, 0.12)" }} />
      <h2 style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "1rem", fontWeight: 700,
                   color: "#5fd39a", margin: 0 }}>
        <span aria-hidden>✓</span> {t(lang, "hostedJourney.accountDetected")}
      </h2>
      <p style={{ marginTop: 10, fontSize: "0.9rem", lineHeight: 1.65, color: BODY }}>
        {t(lang, "hostedJourney.detectedBody", { account: acct || "—" })}
      </p>
      <div style={{ marginTop: 16 }}>
        <Button onClick={onConfirm} disabled={busy}>
          {busy ? t(lang, "hostedJourney.confirming") : t(lang, "hostedJourney.confirmButton")}
        </Button>
      </div>
    </div>
  );
}

// AJ#4 (PP7): the terminal Workspace Ready step. One primary action (Choose Strategy); a de-emphasised
// secondary "Open MetaTrader" re-opens the SAME embedded terminal inline (no navigation) so the customer can
// revisit MT5 without leaving onboarding or learning that Terminal Access exists.
function WorkspaceReadyPanel({
  onOpenTerminal, terminalOpen, children,
}: { onOpenTerminal: () => void; terminalOpen: boolean; children?: React.ReactNode }) {
  const lang = useLang();
  return (
    <div style={{ ...waitCard, padding: "1.25rem 1.3rem", borderColor: "rgba(95, 211, 154, 0.28)",
                  background: "rgba(95, 211, 154, 0.05)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "1.05rem", fontWeight: 700, color: "#5fd39a" }}>
        <span aria-hidden>✓</span> {t(lang, "hostedJourney.workspaceReady")}
      </div>
      <p style={{ marginTop: 10, fontSize: "0.9rem", lineHeight: 1.65, color: BODY }}>
        {t(lang, "hostedJourney.readyBody")}
      </p>
      <div style={{ marginTop: 18, display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap" as const }}>
        <Link href="/strategies/marketplace" style={primaryLink}>{t(lang, "hostedJourney.chooseStrategy")}</Link>
        <button type="button" onClick={onOpenTerminal} style={secondaryButton}>
          {terminalOpen ? t(lang, "hostedJourney.metaTraderOpen") : t(lang, "hostedJourney.openMetaTrader")}
        </button>
      </div>
      {terminalOpen && <div style={{ marginTop: 16 }}>{children}</div>}
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
  const lang = useLang();
  const [journey, setJourney] = useState<HostedJourney | null>(null);
  const [load, setLoad] = useState<Load>("loading");
  const [busy, setBusy] = useState(false);
  // Broker identity is declared LATER (deferred bind), at the "open your workspace" step — never at request.
  const [form, setForm] = useState({ expected_login: "", expected_server: "" });
  // AJ#3 UX: after the normal preparation window, flip to a "taking longer than expected" reassurance so the
  // customer is never staring at an endless spinner.
  const [slowWait, setSlowWait] = useState(false);
  // AJ#4: on the terminal Workspace Ready step, the secondary "Open MetaTrader" re-opens the embedded MT5 inline.
  const [showTerminalOnReady, setShowTerminalOnReady] = useState(false);

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

  // AJ#5.1 (Objective 4): lightweight, LOCAL diagnostic timing — records the wall-clock moment each onboarding
  // milestone is first reached so a future investigation can answer "how long does each stage actually take?".
  // Diagnostic ONLY: no telemetry, no analytics, no network, no backend — it writes to console.debug and to
  // window.__guvfxOnboardingTimings for inspection, and nothing else. Never shown to the customer as progress.
  const timingsRef = useRef<Record<string, string>>({});
  const markTiming = useCallback((key: string) => {
    if (timingsRef.current[key]) return;                    // first occurrence only
    const iso = new Date().toISOString();
    timingsRef.current[key] = iso;
    try { console.debug(`[guvfx-onboarding-timing] ${key} @ ${iso}`); } catch { /* non-fatal */ }
    try {
      (window as unknown as { __guvfxOnboardingTimings?: Record<string, string> })
        .__guvfxOnboardingTimings = { ...timingsRef.current };
    } catch { /* window/storage unavailable — non-fatal */ }
  }, []);
  useEffect(() => {
    const hasLogin = !!(journey?.active_login_masked || "").trim();
    const canOpen = journey?.delivery === "DELIVERY_READY" || journey?.delivery === "DELIVERY_DELIVERABLE";
    if (phase === "AWAITING_BROKER_LOGIN" && canOpen) markTiming("mt5_step_reached");
    if (hasLogin || phase === "BROKER_CONNECTED") markTiming("broker_login_detected");
    if (phase === "ACCOUNT_CONFIRMATION_REQUIRED") { markTiming("account_detected"); markTiming("confirmation_shown"); }
    if (phase === "WORKSPACE_READY") markTiming("workspace_ready");
  }, [phase, journey?.active_login_masked, journey?.delivery, markTiming]);

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
        <Spinner /> {t(lang, "hostedJourney.loading")}
      </div>
    );
  }
  if (load === "unavailable") {
    return (
      <div className="mx-auto max-w-lg p-6">
        <div style={glassCard}>
          <h2 style={{ fontSize: "1.15rem", fontWeight: 600, color: TITLE, margin: 0 }}>{t(lang, "hostedJourney.unavailableTitle")}</h2>
          <p style={{ marginTop: 8, fontSize: "0.9rem", color: BODY }}>
            {t(lang, "hostedJourney.unavailableBody")}
          </p>
        </div>
      </div>
    );
  }
  if (load === "error" || !view) {
    return (
      <div className="mx-auto max-w-lg p-6">
        <div style={{ ...glassCard, borderColor: "rgba(248, 113, 113, 0.3)" }}>
          <p style={{ fontSize: "0.9rem", color: BODY, margin: 0 }}>{t(lang, "hostedJourney.loadError")}</p>
          <div style={{ marginTop: 12 }}>
            <Button onClick={() => { setLoad("loading"); void refresh(); }}>{t(lang, "common.tryAgain")}</Button>
          </div>
        </div>
      </div>
    );
  }

  // AJ#4 — which bespoke panel owns the body at this step. The customer stays inside onboarding end-to-end:
  //   AWAITING + linked + not-openable  → waiting takeover (one "remain on this page" message)
  //   AWAITING + linked + openable      → EMBEDDED MetaTrader (one action: log in; we detect + continue)
  //   BROKER_CONNECTED (wrong account)  → corrective header + EMBEDDED MetaTrader (log into the right account)
  //   ACCOUNT_CONFIRMATION_REQUIRED     → Confirm panel (one action: I confirm this is my trading account)
  //   WORKSPACE_READY                   → Ready panel (Choose Strategy; secondary re-opens MetaTrader inline)
  // The onboarding poll keeps running through the AWAITING/CONNECTED steps (see the poll effect), so each of
  // these transitions happens AUTOMATICALLY — the customer never navigates away or loses progress.
  const waitingTakeover = phase === "AWAITING_BROKER_LOGIN" && identityDeclared && !view.canLaunch;
  const embedStep = phase === "AWAITING_BROKER_LOGIN" && identityDeclared && view.canLaunch;
  const brokerConnected = phase === "BROKER_CONNECTED";
  const confirmStep = view.action?.kind === "confirm";
  const readyStep = view.tone === "ready";

  // Suppress the generic header wherever a bespoke panel carries its own heading. AJ#5: BROKER_CONNECTED now
  // carries its corrective title/copy inside the EmbeddedMetaTraderStep frame, so it is suppressed here too.
  const showGenericHeader = !waitingTakeover && !embedStep && !brokerConnected && !confirmStep && !readyStep;
  // Only the genuine waiting takeover hides the stepper; the embed / confirm / ready steps keep it for progress.
  const showStepper = !waitingTakeover;
  // AJ#4 polish: whenever the embedded MT5 terminal is on screen, widen the whole card to a desktop-sized area so
  // MetaTrader feels like a normal desktop app (the RemoteApp desktop resizes to fill it — guac display-update).
  // All other steps keep the compact reading width. Toggling this does not remount the embed (the width just
  // animates); the embed already renders wide the first time it mounts, so there is no mid-login resize.
  const wide = embedStep || brokerConnected || (readyStep && showTerminalOnReady);
  // AJ#5.1: the live-status wizard's active stage + whether a broker login has been observed yet (drives the
  // "waiting to log in" vs "checking your account" copy). Both are pure journey state.
  const stageIndex = deriveStageIndex(journey);
  const hasLoginSeen = !!(journey?.active_login_masked || "").trim();

  // DECLARE — enter broker details + save (deferred bind). The one launch sub-state that still has a real form:
  // shown until the SERVER records the identity (write-once), after which the embed/waiting panels own the page.
  const declareForm = (
    <form onSubmit={onBind} className="space-y-3">
      <div>
        <label htmlFor="hw-login" style={{ display: "block", fontSize: "0.8rem", color: BODY, marginBottom: 4 }}>
          {t(lang, "hostedJourney.brokerNumber")}
        </label>
        <input id="hw-login" required value={form.expected_login}
               onChange={(e) => setForm({ ...form, expected_login: e.target.value })}
               placeholder="e.g. 1234567" style={inputStyle}
               onFocus={(e) => { e.currentTarget.style.borderColor = ACCENT; }}
               onBlur={(e) => { e.currentTarget.style.borderColor = "rgba(74, 179, 255, 0.2)"; }} />
      </div>
      <div>
        <label htmlFor="hw-server" style={{ display: "block", fontSize: "0.8rem", color: BODY, marginBottom: 4 }}>
          {t(lang, "hostedJourney.brokerServer")}
        </label>
        <input id="hw-server" value={form.expected_server}
               onChange={(e) => setForm({ ...form, expected_server: e.target.value })}
               placeholder="e.g. YourBroker-Demo" style={inputStyle}
               onFocus={(e) => { e.currentTarget.style.borderColor = ACCENT; }}
               onBlur={(e) => { e.currentTarget.style.borderColor = "rgba(74, 179, 255, 0.2)"; }} />
      </div>
      <Button type="submit" disabled={busy || !form.expected_login.trim()} style={{ width: "100%" }}>
        {busy ? t(lang, "hostedJourney.saving") : t(lang, "hostedJourney.saveDetails")}
      </Button>
      <p style={{ fontSize: "0.8rem", color: MUTED, margin: 0 }}>
        {t(lang, "hostedJourney.passwordSafety")}
      </p>
    </form>
  );

  let body: React.ReactNode;
  if (waitingTakeover) {
    body = <WaitingPanel slow={slowWait} />;
  } else if (embedStep) {
    // Obj 2: state plainly what GuvFX is doing / what the customer does / what's next. Before a login is seen we
    // ask them to log in; once observation reports a login we explain we're checking it matches what they linked.
    body = (
      <EmbeddedMetaTraderStep
        activeIndex={stageIndex}
        title={t(lang, hasLoginSeen ? "hostedJourney.checkingTitle" : "hostedJourney.loginTitle")}
        instruction={
          hasLoginSeen
            ? t(lang, "hostedJourney.checkingBody")
            : t(lang, "hostedJourney.loginBody")
        }
        hint={t(lang, "hostedJourney.loginHint")}
        detecting
      >
        <HostedMt5RemoteApp onConnected={() => markTiming("mt5_launched")} />
      </EmbeddedMetaTraderStep>
    );
  } else if (brokerConnected) {
    const m = (journey?.active_login_masked || "").trim();
    body = (
      <EmbeddedMetaTraderStep
        activeIndex={stageIndex}
        title={t(lang, "hostedJourney.correctTitle")}
        instruction={`${t(lang, "hostedJourney.correctBody")}${m ? ` (${m})` : ""}`}
        hint={t(lang, "hostedJourney.loginHint")}
        detecting
      >
        <HostedMt5RemoteApp onConnected={() => markTiming("mt5_launched")} />
      </EmbeddedMetaTraderStep>
    );
  } else if (confirmStep) {
    body = <ConfirmAccountPanel activeIndex={stageIndex} maskedLogin={journey?.active_login_masked} busy={busy} onConfirm={onConfirm} />;
  } else if (readyStep) {
    body = (
      <WorkspaceReadyPanel onOpenTerminal={() => setShowTerminalOnReady(true)} terminalOpen={showTerminalOnReady}>
        <HostedMt5RemoteApp onConnected={() => markTiming("mt5_launched")} />
      </WorkspaceReadyPanel>
    );
  } else if (view.action === null && view.tone === "progress") {
    // Progress phases carry no action — show live motion so it never looks frozen, with one "remain" message.
    body = (
      <div role="status" aria-live="polite"
           style={{ display: "flex", alignItems: "center", gap: 10, color: MUTED, fontSize: "0.85rem" }}>
        <Spinner />
        <span>{t(lang, "hostedJourney.working")}</span>
      </div>
    );
  } else if (view.action?.kind === "request") {
    body = (
      <Button onClick={onRequest} disabled={busy} style={{ width: "100%" }}>
        {busy ? t(lang, "hostedJourney.requesting") : t(lang, "hostedJourney.requestWorkspace")}
      </Button>
    );
  } else if (view.action?.kind === "launch") {
    // The only remaining launch case is AWAITING_BROKER_LOGIN with the identity NOT yet declared → the form.
    body = declareForm;
  } else if (view.action?.kind === "support") {
    body = (
      <div style={{ display: "flex", flexDirection: "column", gap: "0.7rem" }}>
        <p style={{ fontSize: "0.9rem", color: BODY, margin: 0 }}>{t(lang, "hostedJourney.supportBody")}</p>
        {/* Actionable next step — never a dead end. Opens the customer's mail client (no backend). */}
        <a href="mailto:support@guvfx.com?subject=Hosted%20Workspace%20help" style={primaryLink}>
          {view.action.label}
        </a>
      </div>
    );
  } else {
    body = null;
  }

  // Width switches instantly (no CSS transition): an animated width would continuously resize the iframe, and
  // with guac `resize-method=display-update` a live terminal resizing mid-animation could churn the remote
  // (black repaint / dropped keyboard focus). An instant switch = one clean display-update at mount.
  return (
    <div className="mx-auto p-6" style={{ maxWidth: wide ? 1400 : 576 }}>
      {showStepper && <Stepper current={view.stepIndex} />}
      <div style={{ ...glassCard, marginTop: showStepper ? "1.5rem" : 0 }}>
        {showGenericHeader && (
          <>
            <h2 style={{ fontSize: "1.15rem", fontWeight: 600, color: TITLE, margin: 0 }}>
              {phase === "NO_WORKSPACE" ? t(lang, "hostedJourney.startTitle") : view.title}
            </h2>
            <p style={{ marginTop: 8, fontSize: "0.9rem", lineHeight: 1.6, color: BODY }}>
              {phase === "NO_WORKSPACE" ? t(lang, "hostedJourney.startBody") : view.description}
            </p>
          </>
        )}
        <div style={{ marginTop: showGenericHeader ? 16 : 0 }}>{body}</div>
      </div>
    </div>
  );
}

function Stepper({ current }: { current: number }) {
  const lang = useLang();
  const labels = [
    "hostedJourney.step.request",
    "hostedJourney.step.preparing",
    "hostedJourney.step.open",
    "hostedJourney.step.confirm",
    "hostedJourney.step.ready",
  ];
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
              {t(lang, labels[i])}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
