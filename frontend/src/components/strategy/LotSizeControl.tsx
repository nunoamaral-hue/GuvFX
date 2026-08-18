"use client";

/**
 * P0-A — customer-owned Wayond lot size (PER LEG/POSITION).
 *
 * The customer sets the lot size for EACH position the strategy opens. A Wayond signal can open up to
 * `max_legs` positions, so the maximum aggregate at a setting is `per_leg × max_legs` — shown
 * transparently so 0.01 is never mistaken for the total signal volume. Reads/writes the owner-scoped
 * `/api/assignments/<id>/leg-sizing/` endpoint (source-capped server-side). Editable even while the
 * strategy is enabled; the new value applies to FUTURE signals only and never touches open positions.
 */
import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { t, type Lang } from "@/lib/i18n";

type Sizing = {
  lot_per_leg: string;
  default_lot_per_leg: string;
  min: string;
  step: string;
  max: string;
  source_cap: string;
  max_legs: number;
  is_override: boolean;
};

function fmtMax(perLeg: string, maxLegs: number): string {
  const v = Number(perLeg);
  if (!Number.isFinite(v) || v <= 0) return "—";
  // 2dp is the broker lot precision; avoids float drift in the display.
  return (v * maxLegs).toFixed(2);
}

export default function LotSizeControl({ assignmentId, lang }: { assignmentId: number | null; lang: Lang }) {
  const [sizing, setSizing] = useState<Sizing | null>(null);
  const [value, setValue] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "success" | "error"; msg: string } | null>(null);

  const load = useCallback(async () => {
    if (assignmentId == null) { setUnavailable(true); setLoading(false); return; }
    setLoading(true);
    try {
      const data = await apiFetch<Sizing & { ok: boolean }>(`/api/assignments/${assignmentId}/leg-sizing/`, { method: "GET" });
      setSizing(data);
      setValue(data.lot_per_leg);
      setUnavailable(false);
    } catch {
      setUnavailable(true);
    } finally {
      setLoading(false);
    }
  }, [assignmentId]);

  useEffect(() => { void load(); }, [load]);

  const save = useCallback(async () => {
    if (assignmentId == null || saving) return;
    setSaving(true);
    setFeedback(null);
    try {
      const data = await apiFetch<Sizing & { ok: boolean; errors?: Record<string, string[]> }>(
        `/api/assignments/${assignmentId}/leg-sizing/`,
        { method: "PUT", body: JSON.stringify({ lot_per_leg: value }) },
      );
      setSizing(data);
      setValue(data.lot_per_leg);
      setFeedback({ kind: "success", msg: t(lang, "configure.lot.saved") });
    } catch (e: unknown) {
      // apiFetch throws an Error carrying the parsed DRF body on `.body`; surface a customer-safe message.
      const body = (e as { body?: { errors?: Record<string, string[]> } })?.body;
      const first = body?.errors?.lot_per_leg?.[0];
      setFeedback({ kind: "error", msg: first || t(lang, "configure.lot.error") });
    } finally {
      setSaving(false);
    }
  }, [assignmentId, value, saving, lang]);

  if (unavailable) return null;

  const dirty = sizing != null && value !== sizing.lot_per_leg;

  return (
    <div style={{ marginTop: "0.9rem", paddingTop: "0.9rem", borderTop: "1px solid rgba(255,255,255,0.06)" }}>
      <div style={{ color: "#8fa0b7", fontSize: "0.82rem", marginBottom: 4 }}>{t(lang, "configure.lot.label")}</div>
      {loading || !sizing ? (
        <div style={{ color: "#6d7a92", fontSize: "0.8rem" }}>{t(lang, "configure.loading")}</div>
      ) : (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexWrap: "wrap" }}>
            <input
              type="number"
              inputMode="decimal"
              aria-label={t(lang, "configure.lot.label")}
              value={value}
              min={sizing.min}
              max={sizing.max}
              step={sizing.step}
              onChange={(e) => { setValue(e.target.value); setFeedback(null); }}
              style={{
                width: 110, padding: "0.45rem 0.6rem", borderRadius: 8, fontSize: "0.95rem", fontWeight: 700,
                color: "#e8f0ff", background: "rgba(6,10,25,0.7)", border: "1px solid rgba(148,163,184,0.4)",
              }}
            />
            <span style={{ color: "#8fa0b7", fontSize: "0.82rem" }}>{t(lang, "configure.lot.unit")}</span>
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving || !dirty}
              style={{
                padding: "0.45rem 0.9rem", borderRadius: 8, fontSize: "0.82rem", fontWeight: 700,
                cursor: saving || !dirty ? "default" : "pointer",
                color: saving || !dirty ? "#64748b" : "#0b1020",
                background: saving || !dirty ? "rgba(148,163,184,0.15)" : "#7c9cff",
                border: "1px solid rgba(124,156,255,0.5)",
              }}
            >
              {saving ? t(lang, "configure.lot.saving") : t(lang, "configure.lot.save")}
            </button>
          </div>
          <div style={{ color: "#6d7a92", fontSize: "0.74rem", marginTop: 6, lineHeight: 1.5 }}>
            {t(lang, "configure.lot.help", { legs: String(sizing.max_legs), max: fmtMax(value, sizing.max_legs) })}
          </div>
          {feedback && (
            <div
              role={feedback.kind === "error" ? "alert" : undefined}
              style={{
                marginTop: 6, fontSize: "0.78rem", fontWeight: 600,
                color: feedback.kind === "error" ? "#fca5a5" : "#86efac",
              }}
            >
              {feedback.msg}
            </div>
          )}
        </>
      )}
    </div>
  );
}
