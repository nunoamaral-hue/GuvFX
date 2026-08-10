"use client";

// Hosted Workspace customer journey (G18) — renders the deterministic journey state machine + wires actions.
// Pure state logic lives in @/lib/hosted-journey (fully unit-tested); this component is the thin view + I/O.
// It never touches execution: the furthest action is "Choose a strategy", strictly below arming/order-time.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  confirmAccount, describeJourney, fetchJourney, requestWorkspace, STEPS,
  type HostedJourney, type JourneyView,
} from "@/lib/hosted-journey";

type Load = "loading" | "ready" | "unavailable" | "error";

export function HostedWorkspaceJourney() {
  const [journey, setJourney] = useState<HostedJourney | null>(null);
  const [load, setLoad] = useState<Load>("loading");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({ expected_login: "", expected_server: "", broker_name: "" });

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

  async function onRequest(e: React.FormEvent) {
    e.preventDefault();
    if (!form.expected_login.trim() || busy) return;
    setBusy(true);
    try {
      const j = await requestWorkspace(form);
      setJourney(j);
      setLoad("ready");
    } catch {
      setLoad("error");
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
    return <div className="p-6 text-sm text-gray-500">Loading your workspace…</div>;
  }
  if (load === "unavailable") {
    return (
      <div className="mx-auto max-w-lg p-6 text-center">
        <h2 className="text-lg font-semibold">Hosted workspace</h2>
        <p className="mt-2 text-sm text-gray-500">Your hosted trading workspace isn&apos;t available yet. We&apos;ll let you know when it&apos;s ready for you.</p>
      </div>
    );
  }
  if (load === "error" || !view) {
    return (
      <div className="mx-auto max-w-lg p-6 text-center">
        <p className="text-sm text-gray-500">We couldn&apos;t load your workspace status.</p>
        <button onClick={() => { setLoad("loading"); void refresh(); }} className="mt-3 rounded bg-gray-900 px-4 py-2 text-sm text-white">Try again</button>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-xl p-6">
      <Stepper current={view.stepIndex} />
      <div className={`mt-6 rounded-lg border p-5 ${toneClass(view.tone)}`}>
        <h2 className="text-lg font-semibold">{view.title}</h2>
        <p className="mt-2 text-sm">{view.description}</p>
        <div className="mt-4">
          {view.action?.kind === "request" && (
            <form onSubmit={onRequest} className="space-y-3">
              <input required value={form.expected_login} onChange={(e) => setForm({ ...form, expected_login: e.target.value })}
                     placeholder="Broker account number" className="w-full rounded border px-3 py-2 text-sm" />
              <input value={form.expected_server} onChange={(e) => setForm({ ...form, expected_server: e.target.value })}
                     placeholder="Broker server (optional)" className="w-full rounded border px-3 py-2 text-sm" />
              <input value={form.broker_name} onChange={(e) => setForm({ ...form, broker_name: e.target.value })}
                     placeholder="Broker name (optional)" className="w-full rounded border px-3 py-2 text-sm" />
              <button type="submit" disabled={busy || !form.expected_login.trim()}
                      className="w-full rounded bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50">
                {busy ? "Requesting…" : view.action.label}
              </button>
              <p className="text-xs text-gray-400">We only need your broker account details — never your password.</p>
            </form>
          )}
          {view.action?.kind === "launch" && (
            <Link href="/trading/terminal-access"
                  className={`inline-block rounded px-4 py-2 text-sm text-white ${view.canLaunch ? "bg-gray-900" : "bg-gray-400 pointer-events-none"}`}>
              {view.canLaunch ? view.action.label : "Preparing your terminal…"}
            </Link>
          )}
          {view.action?.kind === "confirm" && (
            <button onClick={onConfirm} disabled={busy} className="rounded bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50">
              {busy ? "Confirming…" : view.action.label}
            </button>
          )}
          {view.action?.kind === "assign" && (
            <Link href="/strategies" className="inline-block rounded bg-gray-900 px-4 py-2 text-sm text-white">{view.action.label}</Link>
          )}
          {view.action?.kind === "support" && (
            <p className="text-sm text-gray-500">Please contact support and we&apos;ll get this sorted for you.</p>
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
        return (
          <li key={label} className="flex flex-1 flex-col items-center gap-1">
            <span className={`h-2 w-full rounded ${state === "done" ? "bg-green-500" : state === "current" ? "bg-gray-900" : "bg-gray-200"}`} />
            <span className={state === "current" ? "font-medium" : "text-gray-400"}>{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function toneClass(tone: JourneyView["tone"]): string {
  switch (tone) {
    case "ready": return "border-green-300 bg-green-50";
    case "error": return "border-amber-300 bg-amber-50";
    case "action": return "border-gray-300 bg-white";
    default: return "border-gray-200 bg-gray-50";
  }
}
