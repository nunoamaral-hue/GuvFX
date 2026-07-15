"use client";

// =============================================================================
// Internal /operations status page. Read-only status + two narrow staff actions
// (acknowledge an alert; pause/enable a source-bound strategy assignment). It
// NEVER places an order, resizes, or changes global arming/kill-switch — those
// are not exposed here. Staff-only (endpoints are IsAdminUser; non-staff get 403).
// =============================================================================

import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";

type StratRow = {
  key: string; source_label: string; strategy: string; armed: boolean;
  provider_enabled: boolean; assignment_active: boolean; assignment_id: number | null;
  mode: string | null; per_leg_lot: string | null; total_lot: string | null;
  daily_cap: string | number;
  signals_today: number; accepted: number; rejected: number; plans_promoted: number;
  trades_closed: number; wins: number; losses: number; breakevens: number; realised_pnl: string;
  cards_delivered: number; cards_sent: number;
  rejection_reasons: Record<string, number>;
  last_signal_at: string | null; last_execution_at: string | null; last_notification_at: string | null;
};
type Heartbeat = { source: string; age_s: number | null; interval_s: number; state: string };
type Component = { component: string; status: string; since: string | null; consecutive_failures: number };
type AlertRow = {
  id: number; severity: string; component: string; status: string; title: string; detail: string;
  first_seen: string | null; acknowledged: boolean; acknowledged_by_username: string | null;
  resolved_at: string | null;
};
type InfraItem = { status: string; [k: string]: unknown };
type Summary = {
  generated_at: string;
  overall: "HEALTHY" | "WARNING" | "CRITICAL" | "DISABLED" | "UNKNOWN";
  control: { auto_execution: boolean; mode: string; kill_switch: boolean };
  components: Component[];
  heartbeats: Heartbeat[];
  infra: Record<string, InfraItem>;
  strategies: StratRow[];
  positions: { open: number; promoted_plans: number; pending_candidates: number; failed_candidates: number };
  dispatch: { enabled: boolean; transport: string; last_delivery_at: string | null };
  broker: { account: string | null; reachable: boolean; balance?: number; equity?: number; free_margin?: number; margin_level?: number; reason?: string };
  alerts: AlertRow[];
};

const STATE_COLOR: Record<string, "green" | "yellow" | "red" | "gray"> = {
  HEALTHY: "green", OK: "green", WARNING: "yellow", STALE: "yellow", DEGRADED: "yellow",
  CRITICAL: "red", FAILED: "red", DISABLED: "gray", UNKNOWN: "gray",
};

// infra keys rendered in the Infrastructure section, in order
const INFRA_ORDER = ["backend", "postgres", "ingest_worker", "monitor_chain", "bridge",
  "broker_registry", "telegram_transport", "listener", "shadow_worker", "redis"];

function ago(iso: string | null): string {
  if (!iso) return "—";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

export default function OperationsPage() {
  const [s, setS] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await apiFetch<Summary>("/api/reliability/operations-summary/");
      setS(data); setErr(null);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load operations status");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  const ackAlert = useCallback(async (id: number) => {
    setBusy(`ack-${id}`);
    try { await apiFetch(`/api/reliability/alerts/${id}/acknowledge/`, { method: "POST" }); await load(); }
    catch (e: unknown) { setErr(e instanceof Error ? e.message : "ack failed"); }
    finally { setBusy(null); }
  }, [load]);

  const toggleAssignment = useCallback(async (id: number, next: boolean) => {
    if (next && !confirm("Re-enable this source in routing? It will resume acting on new signals.")) return;
    setBusy(`asn-${id}`);
    try {
      await apiFetch(`/api/reliability/operations/assignment/${id}/set-active/`,
        { method: "POST", body: JSON.stringify({ active: next }) });
      await load();
    } catch (e: unknown) { setErr(e instanceof Error ? e.message : "toggle failed"); }
    finally { setBusy(null); }
  }, [load]);

  if (loading) return <div style={{ padding: 24 }}>Loading operational status…</div>;
  if (err && !s) return <div style={{ padding: 24, color: "#b91c1c" }}>Operational status unavailable: {err} <br /><span style={{ color: "#6b7280", fontSize: 13 }}>(This page is restricted to authorised internal staff.)</span></div>;
  if (!s) return null;

  const overallColor = STATE_COLOR[s.overall] || "gray";
  const coreOff = s.infra?.reliability_core_enabled === false;
  return (
    <div style={{ padding: 24, maxWidth: 1100, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <h1 style={{ fontSize: 26, fontWeight: 700 }}>Operational status</h1>
        <Badge color={overallColor}>{s.overall}</Badge>
      </div>
      <div style={{ color: "#6b7280", fontSize: 13, marginBottom: 20 }}>
        Updated {ago(s.generated_at)} · read-only monitoring{err ? ` · action error: ${err}` : ""}
      </div>

      <Section title="System control">
        <Row label="Auto execution" value={s.control.auto_execution ? "On" : "Off"} color={s.control.auto_execution ? "green" : "gray"} />
        <Row label="Mode" value={s.control.mode} />
        <Row label="Kill switch" value={s.control.kill_switch ? "ENGAGED" : "Off"} color={s.control.kill_switch ? "red" : "green"} />
      </Section>

      <Section title="Strategies">
        {s.strategies.map((st) => (
          <div key={st.key} style={{ border: "1px solid #edeff2", borderRadius: 10, padding: 14, marginBottom: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <strong>{st.strategy}</strong>
              <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <Badge color={st.provider_enabled ? "green" : "gray"}>{st.provider_enabled ? "Provider on" : "Provider off"}</Badge>
                <Badge color={st.assignment_active ? "green" : "yellow"}>{st.assignment_active ? "Assignment active" : "Paused"}</Badge>
                {st.assignment_id != null && (
                  <button
                    disabled={busy === `asn-${st.assignment_id}`}
                    onClick={() => toggleAssignment(st.assignment_id!, !st.assignment_active)}
                    style={{ fontSize: 12, padding: "3px 10px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", cursor: "pointer" }}>
                    {st.assignment_active ? "Pause" : "Enable"}
                  </button>
                )}
              </span>
            </div>
            <div style={{ color: "#6b7280", fontSize: 13, margin: "4px 0 8px" }}>
              Source: {st.source_label}{st.mode ? ` · ${st.mode}` : ""}
              {st.per_leg_lot ? ` · ${st.per_leg_lot}×${st.total_lot ?? "?"} lots` : ""} · cap {String(st.daily_cap)}/day
              · last exec {ago(st.last_execution_at)} · last card {ago(st.last_notification_at)}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 18, fontSize: 14 }}>
              <Stat k="Signals" v={st.signals_today} />
              <Stat k="Accepted" v={st.accepted} />
              <Stat k="Rejected" v={st.rejected} />
              <Stat k="Promoted" v={st.plans_promoted} />
              <Stat k="Closed" v={st.trades_closed} />
              <Stat k="Wins" v={st.wins} />
              <Stat k="Losses" v={st.losses} />
              <Stat k="BE" v={st.breakevens} />
              <Stat k="Realised PnL" v={`$${st.realised_pnl}`} />
              <Stat k="Cards" v={st.cards_delivered} />
            </div>
            {Object.keys(st.rejection_reasons || {}).length > 0 && (
              <div style={{ color: "#6b7280", fontSize: 12, marginTop: 8 }}>
                Rejections today: {Object.entries(st.rejection_reasons).map(([r, n]) => `${r}×${n}`).join(", ")}
              </div>
            )}
          </div>
        ))}
      </Section>

      <Section title="Broker / account">
        <Row label="Account" value={s.broker.account || "—"} />
        {s.broker.reachable ? (
          <>
            <Row label="Balance" value={s.broker.balance != null ? `$${s.broker.balance}` : "—"} />
            <Row label="Equity" value={s.broker.equity != null ? `$${s.broker.equity}` : "—"} />
            <Row label="Free margin" value={s.broker.free_margin != null ? `$${s.broker.free_margin}` : "—"} />
            <Row label="Margin level" value={s.broker.margin_level != null ? `${Math.round(s.broker.margin_level)}%` : "—"} />
          </>
        ) : (
          <Row label="Broker link" value={`Unreachable (${s.broker.reason || "n/a"})`} color="yellow" />
        )}
      </Section>

      <Section title="Positions & pipeline">
        <Row label="Open positions" value={String(s.positions.open)} />
        <Row label="Active (promoted) plans" value={String(s.positions.promoted_plans)} />
        <Row label="Pending notification cards" value={String(s.positions.pending_candidates)} />
        <Row label="Failed notification cards" value={String(s.positions.failed_candidates)} color={s.positions.failed_candidates ? "yellow" : "gray"} />
        <Row label="Dispatch" value={`${s.dispatch.enabled ? "on" : "off"} (${s.dispatch.transport}) · last sent ${ago(s.dispatch.last_delivery_at)}`} />
      </Section>

      <Section title="Infrastructure">
        {coreOff && <div style={{ color: "#a16207", fontSize: 12, marginBottom: 8 }}>Reliability core dormant — components without a live producer read UNKNOWN.</div>}
        {INFRA_ORDER.filter((k) => s.infra?.[k]).map((k) => (
          <Row key={k} label={k} value={String(s.infra[k].status)} color={STATE_COLOR[String(s.infra[k].status)] || "gray"} />
        ))}
      </Section>

      <Section title="Component health">
        {s.heartbeats.map((h) => (
          <Row key={h.source} label={h.source} value={h.age_s == null ? "no beat" : `${h.age_s}s ago (limit ${h.interval_s}s)`} color={STATE_COLOR[h.state] || "gray"} />
        ))}
        {s.components.map((c) => (
          <Row key={c.component} label={c.component} value={c.status} color={STATE_COLOR[c.status] || "gray"} />
        ))}
      </Section>

      <Section title={`Open alerts (${s.alerts.length})`}>
        {s.alerts.length === 0 ? <div style={{ color: "#16a34a" }}>No open alerts.</div> : s.alerts.map((a) => (
          <div key={a.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "6px 0", borderBottom: "1px solid #f4f6f8" }}>
            <span><Badge color={STATE_COLOR[a.severity] || "gray"}>{a.severity}</Badge> <strong style={{ marginLeft: 6 }}>{a.component}</strong> — {a.title || a.detail}</span>
            <span style={{ color: "#6b7280", fontSize: 12, display: "flex", gap: 8, alignItems: "center" }}>
              {ago(a.first_seen)}
              {a.acknowledged ? <Badge color="gray">ack{a.acknowledged_by_username ? ` · ${a.acknowledged_by_username}` : ""}</Badge> : (
                <button disabled={busy === `ack-${a.id}`} onClick={() => ackAlert(a.id)}
                  style={{ fontSize: 12, padding: "3px 10px", borderRadius: 6, border: "1px solid #cbd5e1", background: "#fff", cursor: "pointer" }}>
                  Acknowledge
                </button>
              )}
            </span>
          </div>
        ))}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <h2 style={{ fontSize: 15, fontWeight: 700, textTransform: "uppercase", color: "#6b7280", marginBottom: 10 }}>{title}</h2>
      {children}
    </div>
  );
}
function Row({ label, value, color }: { label: string; value: string; color?: "green" | "yellow" | "red" | "gray" }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid #f4f6f8" }}>
      <span style={{ color: "#374151" }}>{label}</span>
      {color ? <Badge color={color}>{value}</Badge> : <span style={{ fontWeight: 600 }}>{value}</span>}
    </div>
  );
}
function Stat({ k, v }: { k: string; v: string | number }) {
  return (
    <div><div style={{ color: "#6b7280", fontSize: 12 }}>{k}</div><div style={{ fontWeight: 700 }}>{v}</div></div>
  );
}
