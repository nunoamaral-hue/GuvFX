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
type AccountStatus = { ok: boolean; stages?: Stage[] };

const POLL_MS = 5000;

type Tone = "progress" | "pending" | "failed" | "ready";

/**
 * ADR-0021 — customer-visible progress is STATE-DRIVEN: derived from the runtime's durable state, never
 * from whether an operation call happened to succeed. The FRONTEND owns the wording; the backend emits
 * only structured state / reason codes. This maps a durable runtime state (from the account-status
 * panel) OR a structured reason code (from a 409 on complete-step) to a friendly, customer-facing message.
 */
function friendlyForState(runtimeState: string): { title: string; body: string; tone: Tone } {
  switch (runtimeState) {
    case "RUNNING":
      return {
        title: "Your trading terminal is ready.",
        body: "Connection complete — finishing up…",
        tone: "ready",
      };
    case "QUEUED":
    case "PROVISIONING":
    case "STARTING":
    case "AUTHENTICATING":
    case "REPAIRING":
      return {
        title: "Setting up your dedicated trading terminal…",
        body: "This usually takes a minute or two. Keep this page open — it updates automatically.",
        tone: "progress",
      };
    case "BLOCKED":
      return {
        title: "Your terminal is queued.",
        body: "All setup slots are busy right now. Yours will start automatically as soon as one frees up.",
        tone: "pending",
      };
    case "FAILED":
      return {
        title: "We hit a problem setting up your terminal.",
        body: "Our team has been notified. Please try again shortly, or contact support if this persists.",
        tone: "failed",
      };
    case "NOT_CONFIGURED":
    case "NOT_PROVISIONED":
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

/** Structured reason codes the backend returns on a 409 from complete-step → friendly runtime state. */
function stateForReason(reason: string): string {
  switch (reason) {
    case "runtime_pending":
      return "NOT_CONFIGURED";
    case "runtime_provisioning":
      return "PROVISIONING";
    case "capacity_blocked":
      return "BLOCKED";
    case "runtime_failed":
      return "FAILED";
    default:
      return "PROVISIONING"; // runtime_not_ready / anything unknown → keep the neutral "in progress" view
  }
}

const TONE_COLOR: Record<Tone, string> = {
  ready: "#86efac",
  progress: "#4ab3ff",
  pending: "#b7c5dd",
  failed: "#f87171",
};

export function AccountConnectionStep({ state, onComplete }: Props) {
  const [runtimeState, setRuntimeState] = useState<string>("NOT_CONFIGURED");
  const [checking, setChecking] = useState(true);
  const advancingRef = useRef(false);

  // Advance the onboarding step once the durable runtime is ready (idempotent on the backend).
  const advance = useCallback(async () => {
    if (advancingRef.current) return;
    advancingRef.current = true;
    try {
      await apiFetch("/api/onboarding/complete-step/", {
        method: "POST",
        body: JSON.stringify({ step: "account_connected" }),
      });
      onComplete();
    } catch (err: unknown) {
      // Not ready after all (state moved) — reflect the structured reason and resume polling.
      advancingRef.current = false;
      const detail = err instanceof Error ? err.message : "";
      setRuntimeState(stateForReason(detail));
    }
  }, [onComplete]);

  // Poll the durable, read-only account status and drive the UI from it.
  const poll = useCallback(async () => {
    try {
      const status = await apiFetch<AccountStatus>("/api/onboarding/account-status/");
      const stages = status.stages ?? [];
      const runtime = stages.find((s) => s.key === "mt5_runtime");
      const terminal = stages.find((s) => s.key === "hosted_terminal");
      const rs = terminal?.state === "RUNNING" ? "RUNNING" : runtime?.state ?? "NOT_CONFIGURED";
      setRuntimeState(rs);
      if (rs === "RUNNING") {
        void advance();
      }
    } catch {
      // No account yet (404) or a transient error — treat as "waiting to start".
      setRuntimeState("NOT_CONFIGURED");
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

  const msg = friendlyForState(runtimeState);
  const color = TONE_COLOR[msg.tone];

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Connecting your trading account
      </h2>

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
          {msg.title}
        </p>
        <p style={{ color: "#b7c5dd", fontSize: "0.85rem", lineHeight: 1.6 }}>{msg.body}</p>
      </div>

      {msg.tone === "pending" && (
        <p style={{ color: "#b7c5dd", fontSize: "0.85rem", lineHeight: 1.6 }}>
          Haven’t added your account yet? Do it on the{" "}
          <a href="/accounts" style={{ color: "#4ab3ff", textDecoration: "none" }}>
            Broker Accounts
          </a>{" "}
          page — setup starts automatically.
        </p>
      )}

      {msg.tone === "failed" && (
        <Button onClick={() => void poll()} disabled={checking}>
          {checking ? "Checking…" : "Check again"}
        </Button>
      )}
    </div>
  );
}
