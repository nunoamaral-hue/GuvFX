"use client";

import { AppShell } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";

export default function StrategyMarketplacePage() {
  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Strategy Marketplace</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Browse and select pre-made strategies (scaffold).
        </p>

        <Card title="Coming soon">
          <div style={{ padding: "1rem", opacity: 0.85 }}>
            This area will show marketplace strategies with:
            <ul style={{ marginTop: "0.75rem", paddingLeft: "1.25rem" }}>
              <li>Risk profile, symbols, timeframe</li>
              <li>MT5-certified backtest summary</li>
              <li>One-click “Add to My Strategies”</li>
            </ul>
          </div>
        </Card>
      </div>
    </AppShell>
  );
}
