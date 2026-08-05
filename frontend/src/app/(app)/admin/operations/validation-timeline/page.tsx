"use client";

import { useState } from "react";
import { AdminSectionHeader, LoadingState } from "@/components/admin/AdminShared";
import { useAdminRole } from "@/components/admin/useAdminRole";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ValidationTimelinePanel } from "@/components/broker/ValidationTimelinePanel";
import { getValidationTimeline } from "@/lib/broker-api";
import type { ValidationTimeline } from "@/types/broker";

/** WS-D/Phase-3 — Operations → Validation Timeline. Staff-only support tool: look up one broker-validation by
 * Correlation ID / Account ID / Validation Attempt ID and see WHERE it stopped and WHY, without SSH. Read-only;
 * the backend enforces IsAdminUser + darkness. No customer exposure. */
type Mode = "correlation" | "account" | "attempt";
const MODES: { key: Mode; label: string; placeholder: string }[] = [
  { key: "correlation", label: "Correlation ID", placeholder: "validate-acct-13-…" },
  { key: "account", label: "Account ID", placeholder: "e.g. 13" },
  { key: "attempt", label: "Validation Attempt ID", placeholder: "e.g. 7" },
];

const input: React.CSSProperties = {
  background: "rgba(12,18,40,0.6)", border: "1px solid rgba(255,255,255,0.14)", borderRadius: 8,
  padding: "0.5rem 0.7rem", color: "#e9f4ff", fontSize: "0.9rem", minWidth: 260,
};

export default function ValidationTimelinePage() {
  const admin = useAdminRole();
  const [mode, setMode] = useState<Mode>("correlation");
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [timeline, setTimeline] = useState<ValidationTimeline | null>(null);

  const search = async () => {
    const v = value.trim();
    if (!v) return;
    setLoading(true); setError(""); setTimeline(null);
    try {
      const params = mode === "correlation" ? { correlationId: v }
        : mode === "account" ? { accountId: v } : { attemptId: v };
      setTimeline(await getValidationTimeline(params));
    } catch (err) {
      // Phase-4 WS-A (S4): this is a STAFF diagnostic tool (IsAdminUser-gated) — surface the REAL error
      // (DRF detail / status), NOT the customer-sanitised wording, so the one person allowed to see the
      // underlying failure actually does. "Not found" is handled separately via timeline.found === false.
      setError(err instanceof Error ? err.message : "Failed to load validation timeline.");
    } finally {
      setLoading(false);
    }
  };

  if (admin.loading) {
    return <div style={{ maxWidth: 900, margin: "0 auto", padding: "2rem 1rem" }}><LoadingState message="Verifying access…" /></div>;
  }
  if (!admin.authorized) {
    return <div style={{ maxWidth: 900, margin: "0 auto", padding: "2rem 1rem" }}><Alert type="error">You do not have permission to view validation diagnostics.</Alert></div>;
  }

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "1.5rem 1rem" }}>
      <AdminSectionHeader title="Validation Timeline" subtitle="Trace a broker-validation end to end — no SSH required. Staff only." />
      <form onSubmit={(e) => { e.preventDefault(); void search(); }}
            style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", margin: "1rem 0" }}>
        <select aria-label="Search by" value={mode} onChange={(e) => setMode(e.target.value as Mode)} style={input}>
          {MODES.map((m) => <option key={m.key} value={m.key}>{m.label}</option>)}
        </select>
        <input aria-label={MODES.find((m) => m.key === mode)!.label} value={value}
               onChange={(e) => setValue(e.target.value)}
               placeholder={MODES.find((m) => m.key === mode)!.placeholder} style={input} />
        <Button type="submit" disabled={loading || !value.trim()}>{loading ? "Searching…" : "Search"}</Button>
      </form>

      {error && <Alert type="error">{error}</Alert>}
      {loading && <LoadingState message="Loading timeline…" />}
      {timeline && !loading && (
        <Card>
          <ValidationTimelinePanel timeline={timeline} />
        </Card>
      )}
    </div>
  );
}
