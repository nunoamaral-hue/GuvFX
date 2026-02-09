"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useLang } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { t } from "@/lib/i18n";
import { apiFetch } from "@/lib/api";

// =============================================================================
// Types
// =============================================================================

type Strategy = {
  id: number;
  name: string;
};

type TradingAccount = {
  id: number;
  name: string;
  broker_name: string;
  is_active: boolean;
};

type BacktestConfig = {
  id: number;
  name: string;
};

// =============================================================================
// Icons (inline SVG)
// =============================================================================

function StrategyIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
    </svg>
  );
}

function TestIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  );
}

function LiveIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  );
}

function HistoryIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

function CircleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="10" />
    </svg>
  );
}

// =============================================================================
// Action Tile Component
// =============================================================================

type ActionTileProps = {
  icon: React.ReactNode;
  title: string;
  description: string;
  ctaLabel: string;
  href: string;
};

function ActionTile({ icon, title, description, ctaLabel, href }: ActionTileProps) {
  const router = useRouter();

  return (
    <div
      style={{
        padding: "1.25rem",
        borderRadius: 12,
        border: "1px solid rgba(255, 255, 255, 0.08)",
        background: "rgba(15, 23, 42, 0.5)",
        display: "flex",
        flexDirection: "column",
        gap: "0.75rem",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
        }}
      >
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 10,
            background: "rgba(59, 130, 246, 0.1)",
            border: "1px solid rgba(59, 130, 246, 0.2)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#60a5fa",
          }}
        >
          {icon}
        </div>
        <div>
          <h3 style={{ margin: 0, fontSize: "1rem", fontWeight: 600, color: "#f0f6ff" }}>
            {title}
          </h3>
        </div>
      </div>
      <p
        style={{
          margin: 0,
          fontSize: "0.82rem",
          color: "#9ca3af",
          lineHeight: 1.5,
          flex: 1,
        }}
      >
        {description}
      </p>
      <Button
        variant="secondary"
        onClick={() => router.push(href)}
        style={{ alignSelf: "flex-start" }}
      >
        {ctaLabel}
      </Button>
    </div>
  );
}

// =============================================================================
// Stat Item Component
// =============================================================================

type StatItemProps = {
  label: string;
  value: string | number;
};

function StatItem({ label, value }: StatItemProps) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "0.6rem 0",
        borderBottom: "1px solid rgba(255, 255, 255, 0.05)",
      }}
    >
      <span style={{ fontSize: "0.85rem", color: "#9ca3af" }}>{label}</span>
      <span style={{ fontSize: "0.95rem", fontWeight: 500, color: "#f0f6ff" }}>{value}</span>
    </div>
  );
}

// =============================================================================
// Checklist Item Component
// =============================================================================

type ChecklistItemProps = {
  label: string;
  completed: boolean;
};

function ChecklistItem({ label, completed }: ChecklistItemProps) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.5rem",
        padding: "0.4rem 0",
        color: completed ? "#4ade80" : "#9ca3af",
        fontSize: "0.85rem",
      }}
    >
      <span
        style={{
          width: 20,
          height: 20,
          borderRadius: "50%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: completed
            ? "rgba(74, 222, 128, 0.15)"
            : "rgba(148, 163, 184, 0.1)",
        }}
      >
        {completed ? <CheckIcon /> : <CircleIcon />}
      </span>
      <span style={{ textDecoration: completed ? "line-through" : "none" }}>{label}</span>
    </div>
  );
}

// =============================================================================
// Main Component
// =============================================================================

export default function DashboardPage() {
  const lang = useLang();

  // Data for system status
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [configs, setConfigs] = useState<BacktestConfig[]>([]);
  const [loading, setLoading] = useState(true);

  // Fetch all counts
  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      try {
        const [strats, accts, cfgs] = await Promise.all([
          apiFetch<Strategy[]>("/api/strategies/strategies/", {}).catch(() => []),
          apiFetch<TradingAccount[]>("/api/trading/accounts/", {}).catch(() => []),
          apiFetch<BacktestConfig[]>("/api/backtests/configs/", {}).catch(() => []),
        ]);
        setStrategies(strats);
        setAccounts(accts);
        setConfigs(cfgs);
      } catch {
        // Silent fail, show dashes
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, []);

  // Derive checklist completion
  const hasStrategy = strategies.length > 0;
  const hasConfig = configs.length > 0;
  const hasAccount = accounts.length > 0;
  // "Reviewed results" is implicit if they have configs (they can see results)
  const hasReviewedResults = hasConfig;

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      {/* Header */}
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem", color: "#f0f6ff" }}>
        {t(lang, "dashboard.title")}
      </h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
        {t(lang, "dashboard.subtitle")}
      </p>
      <p style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "1.5rem" }}>
        {t(lang, "legal.microDisclaimer")}
      </p>

      {/* Primary Action Tiles */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: "1rem",
          marginBottom: "1.5rem",
        }}
      >
        <ActionTile
          icon={<StrategyIcon />}
          title={t(lang, "dashboard.tile.createStrategy.title")}
          description={t(lang, "dashboard.tile.createStrategy.description")}
          ctaLabel={t(lang, "dashboard.tile.createStrategy.cta")}
          href="/strategies/create"
        />
        <ActionTile
          icon={<TestIcon />}
          title={t(lang, "dashboard.tile.runBacktests.title")}
          description={t(lang, "dashboard.tile.runBacktests.description")}
          ctaLabel={t(lang, "dashboard.tile.runBacktests.cta")}
          href="/backtests"
        />
        <ActionTile
          icon={<LiveIcon />}
          title={t(lang, "dashboard.tile.liveTrading.title")}
          description={t(lang, "dashboard.tile.liveTrading.description")}
          ctaLabel={t(lang, "dashboard.tile.liveTrading.cta")}
          href="/trading/live-trading"
        />
        <ActionTile
          icon={<HistoryIcon />}
          title={t(lang, "dashboard.tile.tradeHistory.title")}
          description={t(lang, "dashboard.tile.tradeHistory.description")}
          ctaLabel={t(lang, "dashboard.tile.tradeHistory.cta")}
          href="/trading/trade-history"
        />
      </div>

      {/* Two-column grid for status and next steps */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))",
          gap: "1rem",
        }}
      >
        {/* System Status */}
        <Card
          title={t(lang, "dashboard.systemStatus.title")}
          subtitle={t(lang, "dashboard.systemStatus.subtitle")}
        >
          <div style={{ display: "flex", flexDirection: "column" }}>
            <StatItem
              label={t(lang, "dashboard.systemStatus.strategies")}
              value={loading ? "—" : strategies.length}
            />
            <StatItem
              label={t(lang, "dashboard.systemStatus.linkedAccounts")}
              value={loading ? "—" : accounts.length}
            />
            <StatItem
              label={t(lang, "dashboard.systemStatus.testConfigs")}
              value={loading ? "—" : configs.length}
            />
          </div>
          <p
            style={{
              marginTop: "0.75rem",
              fontSize: "0.72rem",
              color: "#64748b",
              lineHeight: 1.5,
            }}
          >
            {t(lang, "dashboard.systemStatus.note")}
          </p>
        </Card>

        {/* Next Steps */}
        <Card
          title={t(lang, "dashboard.nextSteps.title")}
          subtitle={t(lang, "dashboard.nextSteps.subtitle")}
        >
          <div style={{ display: "flex", flexDirection: "column" }}>
            <ChecklistItem
              label={t(lang, "dashboard.nextSteps.createStrategy")}
              completed={hasStrategy}
            />
            <ChecklistItem
              label={t(lang, "dashboard.nextSteps.runTest")}
              completed={hasConfig}
            />
            <ChecklistItem
              label={t(lang, "dashboard.nextSteps.reviewResults")}
              completed={hasReviewedResults}
            />
            <ChecklistItem
              label={t(lang, "dashboard.nextSteps.linkAccount")}
              completed={hasAccount}
            />
          </div>
          <p
            style={{
              marginTop: "0.75rem",
              fontSize: "0.72rem",
              color: "#64748b",
              lineHeight: 1.5,
            }}
          >
            {t(lang, "dashboard.nextSteps.note")}
          </p>
        </Card>
      </div>

      {/* Quick Links */}
      <div
        style={{
          marginTop: "1.5rem",
          padding: "1rem 1.25rem",
          borderRadius: 10,
          background: "rgba(15, 23, 42, 0.4)",
          border: "1px solid rgba(255, 255, 255, 0.06)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          flexWrap: "wrap",
          gap: "0.75rem",
        }}
      >
        <span style={{ fontSize: "0.85rem", color: "#9ca3af" }}>
          {t(lang, "dashboard.quickLinks.label")}
        </span>
        <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
          <Link
            href="/strategies"
            style={{
              fontSize: "0.8rem",
              color: "#60a5fa",
              textDecoration: "none",
            }}
          >
            {t(lang, "dashboard.quickLinks.strategies")}
          </Link>
          <Link
            href="/accounts"
            style={{
              fontSize: "0.8rem",
              color: "#60a5fa",
              textDecoration: "none",
            }}
          >
            {t(lang, "dashboard.quickLinks.accounts")}
          </Link>
          <Link
            href="/profile"
            style={{
              fontSize: "0.8rem",
              color: "#60a5fa",
              textDecoration: "none",
            }}
          >
            {t(lang, "dashboard.quickLinks.profile")}
          </Link>
        </div>
      </div>
    </div>
  );
}
