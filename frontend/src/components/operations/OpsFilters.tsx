"use client";

import React from "react";
import { CATEGORIES, SEVERITIES } from "@/lib/operations-status";

/** WP5.3 — the timeline filter bar. `category` is applied SERVER-SIDE (the only filter the WP5.1 API
 * supports); severity / resolution / visibility / date-range / search are applied client-side over the
 * fetched page (the API is not redesigned). Presentational + controlled. */
export type OpsFilterState = {
  severity: string;    // "" = all
  category: string;    // "" = all (server-side)
  resolution: string;  // "" = all | "open" | "resolved"
  visibility: string;  // "" = all | "customer" | "operator"
  from: string;        // YYYY-MM-DD or ""
  to: string;          // YYYY-MM-DD or ""
  search: string;
};

export const EMPTY_FILTERS: OpsFilterState = {
  severity: "", category: "", resolution: "", visibility: "", from: "", to: "", search: "",
};

const fieldWrap: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 4 };
const lbl: React.CSSProperties = { color: "#8fa0b7", fontSize: "0.72rem" };
const control: React.CSSProperties = {
  background: "rgba(12,18,40,0.9)", color: "#e5f4ff", border: "1px solid rgba(255,255,255,0.14)",
  borderRadius: 8, padding: "0.4rem 0.55rem", fontSize: "0.85rem", minWidth: 120,
};

export const OpsFilters: React.FC<{
  value: OpsFilterState;
  onChange: (next: OpsFilterState) => void;
}> = ({ value, onChange }) => {
  const set = (patch: Partial<OpsFilterState>) => onChange({ ...value, ...patch });
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-end" }}
         role="search" aria-label="Filter operational events">
      <div style={fieldWrap}>
        <label style={lbl} htmlFor="ops-f-search">Search</label>
        <input id="ops-f-search" type="search" style={control} value={value.search}
               placeholder="summary, reason, source…"
               onChange={(e) => set({ search: e.target.value })} />
      </div>
      <div style={fieldWrap}>
        <label style={lbl} htmlFor="ops-f-severity">Severity</label>
        <select id="ops-f-severity" style={control} value={value.severity}
                onChange={(e) => set({ severity: e.target.value })}>
          <option value="">All</option>
          {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      <div style={fieldWrap}>
        <label style={lbl} htmlFor="ops-f-category">Category</label>
        <select id="ops-f-category" style={control} value={value.category}
                onChange={(e) => set({ category: e.target.value })}>
          <option value="">All</option>
          {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>
      <div style={fieldWrap}>
        <label style={lbl} htmlFor="ops-f-resolution">Status</label>
        <select id="ops-f-resolution" style={control} value={value.resolution}
                onChange={(e) => set({ resolution: e.target.value })}>
          <option value="">All</option>
          <option value="open">Open</option>
          <option value="resolved">Resolved</option>
        </select>
      </div>
      <div style={fieldWrap}>
        <label style={lbl} htmlFor="ops-f-visibility">Visibility</label>
        <select id="ops-f-visibility" style={control} value={value.visibility}
                onChange={(e) => set({ visibility: e.target.value })}>
          <option value="">All</option>
          <option value="customer">Customer-visible</option>
          <option value="operator">Operator-only</option>
        </select>
      </div>
      <div style={fieldWrap}>
        <label style={lbl} htmlFor="ops-f-from">From</label>
        <input id="ops-f-from" type="date" style={control} value={value.from}
               onChange={(e) => set({ from: e.target.value })} />
      </div>
      <div style={fieldWrap}>
        <label style={lbl} htmlFor="ops-f-to">To</label>
        <input id="ops-f-to" type="date" style={control} value={value.to}
               onChange={(e) => set({ to: e.target.value })} />
      </div>
      <button type="button" style={{ ...control, cursor: "pointer", minWidth: 0 }}
              onClick={() => onChange(EMPTY_FILTERS)}>Clear</button>
    </div>
  );
};

/** Apply the client-side filters (everything except `category`, which is server-side) over a page of
 * events. Pure + generic — preserves the caller's element type (e.g. OperationalEvent[] in → out) so it
 * drops straight into the timeline table. Exported for reuse + unit testing. */
export function applyClientFilters<E extends OpsEventLike>(events: E[], f: OpsFilterState): E[] {
  const q = f.search.trim().toLowerCase();
  // Parse the YYYY-MM-DD bounds as LOCAL midnight (not UTC) so day boundaries align with the timestamps
  // the operator sees — formatWhen renders via toLocaleString (viewer-local TZ). `${d}T00:00:00` (no "Z")
  // is parsed in local time; a bare Date.parse("YYYY-MM-DD") would be UTC and drift by the TZ offset.
  const fromMs = f.from ? new Date(`${f.from}T00:00:00`).getTime() : null;
  const toMs = f.to ? new Date(`${f.to}T00:00:00`).getTime() + 86_400_000 : null; // inclusive end-of-day
  return events.filter((e) => {
    if (f.severity && (e.severity || "").toUpperCase() !== f.severity) return false;
    if (f.resolution === "open" && e.resolved) return false;
    if (f.resolution === "resolved" && !e.resolved) return false;
    if (f.visibility === "customer" && !e.customer_visible) return false;
    if (f.visibility === "operator" && e.customer_visible) return false;
    if (fromMs != null || toMs != null) {
      const t = e.timestamp ? Date.parse(e.timestamp) : NaN;
      if (Number.isNaN(t)) return false;
      if (fromMs != null && t < fromMs) return false;
      if (toMs != null && t >= toMs) return false;
    }
    if (q) {
      const hay = `${e.summary} ${e.reason_code} ${e.source} ${e.event_type} ${e.category}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

type OpsEventLike = {
  severity: string; resolved: boolean; customer_visible: boolean; timestamp: string | null;
  summary: string; reason_code: string; source: string; event_type: string; category: string;
};
