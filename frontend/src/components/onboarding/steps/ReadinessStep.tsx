"use client";

import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { apiFetch } from "@/lib/api";
import type { ReadinessResponse } from "@/types/onboarding";

const CHECK_LABELS: Record<string, string> = {
  has_active_account: "Active Trading Account",
  has_live_assignment: "Live Strategy Assignment",
  entitlement_valid: "Valid Entitlement",
  terminal_node_valid: "Terminal Node Available",
};

export function ReadinessStep() {
  const [readiness, setReadiness] = useState<ReadinessResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchReadiness = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<ReadinessResponse>("/api/onboarding/readiness/", {});
      setReadiness(data);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load readiness status.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchReadiness();
  }, []);

  return (
    <div>
      <h2 style={{ fontSize: "1.25rem", fontWeight: 600, color: "#e9f4ff", marginBottom: "0.5rem" }}>
        Readiness Review
      </h2>
      <p style={{ color: "#b7c5dd", fontSize: "0.9rem", marginBottom: "1.25rem", lineHeight: 1.6 }}>
        Review your platform readiness. All checks below must pass before your strategies can
        execute in the live environment.
      </p>

      {loading && (
        <p style={{ color: "#94a3b8", fontSize: "0.85rem" }}>Loading readiness status...</p>
      )}

      {error && (
        <p style={{ color: "#f87171", fontSize: "0.85rem", marginBottom: "0.75rem" }}>{error}</p>
      )}

      {readiness && (
        <>
          {/* Onboarding completion */}
          <div
            style={{
              padding: "0.75rem 1rem",
              borderRadius: 10,
              border: `1px solid ${readiness.onboarding_completed
                ? "rgba(34, 197, 94, 0.3)"
                : "rgba(251, 191, 36, 0.3)"}`,
              background: readiness.onboarding_completed
                ? "rgba(34, 197, 94, 0.04)"
                : "rgba(251, 191, 36, 0.04)",
              marginBottom: "1rem",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span style={{ fontSize: "0.9rem", color: "#e9f4ff", fontWeight: 600 }}>
              Onboarding
            </span>
            <Badge color={readiness.onboarding_completed ? "green" : "yellow"}>
              {readiness.onboarding_completed ? "Complete" : "Incomplete"}
            </Badge>
          </div>

          {readiness.missing_steps.length > 0 && (
            <p style={{ color: "#fbbf24", fontSize: "0.82rem", marginBottom: "1rem" }}>
              Missing steps: {readiness.missing_steps.join(", ")}
            </p>
          )}

          {/* Readiness checks */}
          <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", marginBottom: "1.25rem" }}>
            {Object.entries(readiness.readiness_checks).map(([key, value]) => (
              <div
                key={key}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  padding: "0.6rem 1rem",
                  borderRadius: 8,
                  border: "1px solid rgba(74, 179, 255, 0.08)",
                  background: "rgba(255, 255, 255, 0.02)",
                }}
              >
                <span style={{ fontSize: "0.85rem", color: "#b7c5dd" }}>
                  {CHECK_LABELS[key] ?? key}
                </span>
                <Badge color={value ? "green" : "red"}>
                  {value ? "Pass" : "Fail"}
                </Badge>
              </div>
            ))}
          </div>

          {/* Overall readiness */}
          <div
            style={{
              padding: "1rem",
              borderRadius: 10,
              border: `1px solid ${readiness.permitted
                ? "rgba(34, 197, 94, 0.3)"
                : "rgba(248, 113, 113, 0.3)"}`,
              background: readiness.permitted
                ? "rgba(34, 197, 94, 0.06)"
                : "rgba(248, 113, 113, 0.04)",
              textAlign: "center",
            }}
          >
            <p style={{
              fontSize: "1rem",
              fontWeight: 700,
              color: readiness.permitted ? "#86efac" : "#fca5a5",
              margin: 0,
            }}>
              {readiness.permitted
                ? "Platform Ready — All gates passed"
                : "Not Yet Ready — Complete remaining steps and checks"}
            </p>
          </div>
        </>
      )}
    </div>
  );
}
