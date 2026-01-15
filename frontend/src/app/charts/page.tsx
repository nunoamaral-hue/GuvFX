

"use client";

import { useState } from "react";
import { AppShell } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";

export default function ChartsPage() {
  const [mt5Url, setMt5Url] = useState<string>("");
  const [mt5Loading, setMt5Loading] = useState(false);
  const [mt5Error, setMt5Error] = useState<string | null>(null);

  const getMt5Url = async (): Promise<string> => {
    const data = await apiFetch<{ ok: boolean; launch_url: string }>(
      "/api/mt5/launch/",
      { method: "POST" }
    );
    return data.launch_url;
  };

  const launchMt5 = async () => {
    setMt5Error(null);
    setMt5Loading(true);
    try {
      const url = await getMt5Url();
      setMt5Url(url);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to launch MT5.";
      setMt5Error(msg);
    } finally {
      setMt5Loading(false);
    }
  };

  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Charts</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Live MT5 charts and market visualisation.
        </p>

        <Card title="MT5 Terminal">
          <div style={{ display: "flex", gap: "0.5rem", marginBottom: "0.75rem" }}>
            <Button type="button" onClick={launchMt5} disabled={mt5Loading}>
              {mt5Loading ? "Launching…" : "Preview MT5"}
            </Button>
            <Button
              type="button"
              variant="secondary"
              disabled={mt5Loading}
              onClick={async () => {
                const url = await getMt5Url();
                window.open(url, "_blank", "noopener,noreferrer");
              }}
            >
              Open Fullscreen
            </Button>
          </div>

          {mt5Error && (
            <div style={{ color: "#f87171", marginBottom: "0.75rem" }}>
              {mt5Error}
            </div>
          )}

          <div
            style={{
              height: 520,
              borderRadius: 12,
              overflow: "hidden",
              border: "1px solid rgba(255,255,255,0.1)",
              background: "#020617",
            }}
          >
            {mt5Url ? (
              <iframe
                key={mt5Url}
                src={mt5Url}
                title="MT5 Terminal"
                style={{ width: "100%", height: "100%", border: 0 }}
                allow="clipboard-read; clipboard-write"
              />
            ) : (
              <div
                style={{
                  height: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  opacity: 0.7,
                }}
              >
                Click “Preview MT5” to load charts.
              </div>
            )}
          </div>
        </Card>
      </div>
    </AppShell>
  );
}