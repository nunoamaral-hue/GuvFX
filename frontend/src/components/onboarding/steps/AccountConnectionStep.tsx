"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { OnboardingState } from "@/types/onboarding";

type Props = {
  state: OnboardingState;
  onComplete: () => void;
};

type Stage = { key: string; label: string; state: string; detail: string; at: string | null };
type LifecycleStep = { key: string; label: string; status: string };
// ADR-0021 — the explicit customer-facing lifecycle, owned by the backend (Account received →
// Provisioning runtime → Connecting to broker → Validated / Connection failed → Retry). The frontend
// renders it; it never invents the phase or copy.
type Lifecycle = { phase: string; label: string; detail: string; retryable: boolean; steps?: LifecycleStep[] };
type AccountStatus = { ok: boolean; stages?: Stage[]; lifecycle?: Lifecycle };

const POLL_MS = 5000;

type Tone = "progress" | "pending" | "failed";

/**
 * ADR-0021 — customer-visible progress is STATE-DRIVEN: derived from the runtime's durable state, never
 * from whether an operation call succeeded. The FRONTEND owns the wording; the backend emits only
 * structured state / reason codes. The states below are exactly the panel vocabulary emitted by the
 * backend's ``user_facing_state`` (NOT the raw RuntimeState enum). RUNNING is deliberately a PROGRESS
 * ("finishing") message, not a green "ready" claim — the real completion signal is the step advancing
 * (``onComplete``), which only happens once the backend confirms the runtime is fully ready.
 */
function friendlyForState(runtimeState: string): { title: string; body: string; tone: Tone } {
  switch (runtimeState) {
    case "RUNNING":
      return {
        title: "Almost there — finishing setup…",
        body: "Your terminal is up; we’re running the final readiness checks. This page advances on its own.",
        tone: "progress",
      };
    case "QUEUED":
    case "PROVISIONING":
      return {
        title: "Setting up your dedicated trading terminal…",
        body: "This usually takes a minute or two. Keep this page open — it updates automatically.",
        tone: "progress",
      };
    case "DEGRADED":
      return {
        title: "Reconnecting your terminal…",
        body: "Your terminal needs a moment to recover. We’re handling it automatically — no action needed.",
        tone: "progress",
      };
    case "BLOCKED":
      return {
        title: "Your terminal is queued.",
        body: "All setup slots are busy right now. Yours will start automatically as soon as one frees up.",
        tone: "pending",
      };
    case "STOPPED":
      return {
        title: "Your terminal is paused.",
        body: "It will resume automatically. If it stays paused, contact support.",
        tone: "pending",
      };
    case "FAILED":
    case "REMOVING":
    case "REMOVED":
      return {
        title: "We hit a problem setting up your terminal.",
        body: "Our team has been notified. Please try again shortly, or contact support if this persists.",
        tone: "failed",
      };
    case "NOT_CONFIGURED":
      return {
        title: "Waiting to start setup…",
        body: "Once your broker account is saved on the Broker Accounts page, we begin setting up your terminal automatically.",
        tone: "pending",
      };
    default:
      return {
        title: "Setting up your dedicated trading terminal…",
        body: "This usually takes a minute or two. Keep this page open — it updates automatically.",
        tone: "progress",
      };
  }
}

/**
 * Extract the backend's structured detail code from a thrown apiFetch error. apiFetch may surface either
 * the bare `detail` string OR the raw JSON body (`{"detail":"..."}`) depending on its internal parse path,
 * so we tolerate both. (A central apiFetch fix to always surface the bare `detail` is a separate follow-up.)
 */
function reasonFromError(err: unknown): string {
  if (!(err instanceof Error)) return "";
  const m = err.message;
  try {
    const o = JSON.parse(m) as { detail?: unknown };
    return o && typeof o === "object" && typeof o.detail === "string" ? o.detail : m;
  } catch {
    return m;
  }
}

/** Structured reason codes the backend returns on a 409 from complete-step → panel state (for display). */
function stateForReason(reason: string): string {
  switch (reason) {
    case "runtime_pending":
      return "NOT_CONFIGURED";
    case "capacity_blocked":
      return "BLOCKED";
    case "runtime_failed":
      return "FAILED";
    case "runtime_provisioning":
    case "runtime_not_ready":
    default:
      return "PROVISIONING"; // still coming up (e.g. RUNNING but readiness checks not yet complete)
  }
}

const TONE_COLOR: Record<Tone, string> = {
  progress: "#4ab3ff",
  pending: "#b7c5dd",
  failed: "#f87171",
};

// ADR-0021 — map the backend's explicit lifecycle phase to a panel tone. The backend owns the phase and
// its copy; the frontend owns only the presentation (colour + retry affordance).
const PHASE_TONE: Record<string, Tone> = {
  account_received: "pending",
  provisioning_runtime: "progress",
  connecting_broker: "progress",
  validated: "progress",
  connection_failed: "failed",
};

export function AccountConnectionStep({ state, onComplete }: Props) {
  const [runtimeState, setRuntimeState] = useState<string>("NOT_CONFIGURED");
  const [lifecycle, setLifecycle] = useState<Lifecycle | null>(null);
  const [checking, setChecking] = useState(true);
  const advancingRef = useRef(false);

  // Attempt to advance the onboarding step. The backend is authoritative: it only marks the step
  // complete when the runtime is genuinely ready, and returns a structured reason code otherwise.
  const advance = useCallback(async () => {
    if (advancingRef.current) return;
    advancingRef.current = true;
    try {
      await apiFetch("/api/onboarding/complete-step/", {
        method: "POST",
        body: JSON.stringify({ step: "account_connected" }),
      });
      onComplete();   // real completion — the step unmounts; no misleading "ready" flash before this
    } catch (err: unknown) {
      // Not actually ready yet — reflect the structured reason and keep polling. Reset the guard so the
      // next poll can re-attempt once the runtime finishes coming up.
      advancingRef.current = false;
      setRuntimeState(stateForReason(reasonFromError(err)));
    }
  }, [onComplete]);

  // Poll the durable, read-only account status and drive the UI from it.
  const poll = useCallback(async () => {
    setChecking(true);
    try {
      const status = await apiFetch<AccountStatus>("/api/onboarding/account-status/");
      setLifecycle(status.lifecycle ?? null);   // backend-authoritative explicit lifecycle (for display)
      const stages = status.stages ?? [];
      const runtime = stages.find((s) => s.key === "mt5_runtime");
      const terminal = stages.find((s) => s.key === "hosted_terminal");
      const rs = terminal?.state === "RUNNING" ? "RUNNING" : runtime?.state ?? "NOT_CONFIGURED";
      setRuntimeState(rs);
      if (rs === "RUNNING") {
        void advance();   // ask the backend to confirm readiness + advance (authoritative)
      }
    } catch (err: unknown) {
      // A 404 means "no account yet" (backend returns {"detail":"not_found"}) → show the pending prompt.
      // Any OTHER error is a transient blip — do NOT regress the display to "waiting to start"; keep the
      // last known state (the initial state is already NOT_CONFIGURED, so a first-load blip is benign).
      if (reasonFromError(err) === "not_found") {
        setRuntimeState("NOT_CONFIGURED");
      }
    } finally {
      setChecking(false);
    }
  }, [advance]);

  useEffect(() => {
    if (state.account_connected) return;
    void poll();
    const id = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(id);
  }, [state.account_connected, poll]);

  if (state.account_connected) {
    return (
      <div>
        <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
          Account Connection
        </h2>
        <p style={{ color: "#86efac", fontSize: "0.9rem" }}>Your trading account is connected.</p>
      </div>
    );
  }

  // The backend owns the explicit lifecycle phase + copy; fall back to the state-derived message only if
  // the lifecycle field is absent (older backend / first-load blip) so the panel stays stable.
  const fallback = friendlyForState(runtimeState);
  const title = lifecycle ? lifecycle.label : fallback.title;
  const body = lifecycle ? lifecycle.detail : fallback.body;
  const tone: Tone = lifecycle ? (PHASE_TONE[lifecycle.phase] ?? "progress") : fallback.tone;
  const color = TONE_COLOR[tone];
  const showRetry = lifecycle ? lifecycle.retryable : tone === "failed";

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Connecting your trading account
      </h2>

      {lifecycle && lifecycle.steps && lifecycle.steps.length > 0 && (
        <ol style={{ listStyle: "none", padding: 0, margin: "0 0 1rem", display: "flex", flexWrap: "wrap", gap: "0.5rem" }}>
          {lifecycle.steps.map((s) => {
            const dotColor = s.status === "failed" ? TONE_COLOR.failed
              : s.status === "done" ? "#4ade80"
              : s.status === "current" ? TONE_COLOR.progress
              : "#3a4658";
            return (
              <li key={s.key} style={{ display: "flex", alignItems: "center", gap: "0.4rem",
                color: s.status === "pending" ? "#6b7a90" : "#b7c5dd", fontSize: "0.78rem" }}>
                <span aria-hidden style={{ width: 8, height: 8, borderRadius: "50%", background: dotColor,
                  display: "inline-block" }} />
                {s.label}
              </li>
            );
          })}
        </ol>
      )}

      <div
        role="status"
        aria-live="polite"
        style={{
          border: `1px solid ${color}33`,
          background: `${color}14`,
          borderRadius: "0.5rem",
          padding: "1rem",
          marginBottom: "1rem",
        }}
      >
        <p style={{ color, fontSize: "0.95rem", fontWeight: 600, marginBottom: "0.35rem" }}>
          {title}
        </p>
        <p style={{ color: "#b7c5dd", fontSize: "0.85rem", lineHeight: 1.6 }}>{body}</p>
      </div>

      {tone === "pending" && (
        <p style={{ color: "#b7c5dd", fontSize: "0.85rem", lineHeight: 1.6 }}>
          Haven’t added your account yet? Do it on the{" "}
          <a href="/accounts" style={{ color: "#4ab3ff", textDecoration: "none" }}>
            Broker Accounts
          </a>{" "}
          page — setup starts automatically.
        </p>
      )}

      {showRetry && (
        // Re-checks the durable status (a real re-provision endpoint is a deferred follow-up — ADR-0021
        // addendum), so the label stays honest: this re-reads state, it does not re-attempt setup.
        <Button onClick={() => void poll()} disabled={checking}>
          {checking ? "Checking…" : "Check again"}
        </Button>
      )}
    </div>
  );
}
