"use client";

import { useLang } from "@/components/AppShell";
import { Button } from "@/components/ui/Button";
import { t } from "@/lib/i18n";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import {
  priceFor,
  priceLabel,
  getStrategy,
  fetchSignalCopyStatus,
  type SignalCopyStatus,
} from "@/lib/strategy-journey";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { LocalizedBetaSurface } from "@/components/i18n/LocalizedBetaSurface";

// ─────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────
type MarketCategory = "Trend" | "Breakout" | "Reversion" | "Patterns" | "System-grade";

type Accent = "blue" | "green" | "purple" | "yellow" | "cyan";

type MarketplaceStrategy = {
  id: string;
  name: string;
  category: MarketCategory;
  accent: Accent;
  timeframes: string[];
  pairs: string[];
  tags?: string[];
  // Signal-copy strategy (e.g. Wayond WIM): the ONLY beta strategy with an automated-execution path. "Get
  // Strategy" acquires it (non-executing), then Configure → Enable turns on automated trading. Generic cards
  // are research/templates and never auto-execute.
  signalCopy?: boolean;
};

// Per-card owned/enabled state for signal-copy strategies (drives the "Get Strategy" vs "Configure" CTA and
// the owned-state badge). `loading` while the first status fetch is in flight.
type CardCopyState = SignalCopyStatus & { loading?: boolean };

type TradingAccount = {
  id: number;
  name: string;
  broker_name?: string;
  account_number?: string;
  is_demo?: boolean;
  is_active?: boolean;
  runtime_ready?: boolean;
};

// ─────────────────────────────────────────────────────────────────────
// Styling Helpers
// ─────────────────────────────────────────────────────────────────────
const accentPill = (accent: Accent) => {
  const map = {
    blue: { bg: "rgba(59,130,246,0.16)", border: "rgba(59,130,246,0.35)", text: "#93c5fd" },
    green: { bg: "rgba(34,197,94,0.14)", border: "rgba(34,197,94,0.35)", text: "#86efac" },
    purple: { bg: "rgba(168,85,247,0.14)", border: "rgba(168,85,247,0.35)", text: "#d8b4fe" },
    yellow: { bg: "rgba(250,204,21,0.14)", border: "rgba(250,204,21,0.40)", text: "#fde047" },
    cyan: { bg: "rgba(34,211,238,0.14)", border: "rgba(34,211,238,0.40)", text: "#67e8f9" },
  } as const;
  return map[accent];
};

const glassCardStyle: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.10)",
  borderRadius: 14,
  overflow: "hidden",
  background: "linear-gradient(180deg, rgba(10,16,35,0.72), rgba(6,10,25,0.85))",
  boxShadow: "0 10px 30px rgba(0,0,0,0.45)",
};

const pillStyle = (accent: Accent): React.CSSProperties => {
  const a = accentPill(accent);
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 6,
    padding: "0.18rem 0.55rem",
    borderRadius: 999,
    border: `1px solid ${a.border}`,
    background: a.bg,
    color: a.text,
    fontSize: "0.75rem",
    fontWeight: 600,
    whiteSpace: "nowrap",
  };
};

const badgeStyle = (): React.CSSProperties => ({
  display: "inline-flex",
  alignItems: "center",
  padding: "0.15rem 0.5rem",
  borderRadius: 999,
  border: "1px solid rgba(100,116,139,0.35)",
  background: "rgba(100,116,139,0.14)",
  color: "#94a3b8",
  fontSize: "0.7rem",
  fontWeight: 600,
  whiteSpace: "nowrap",
});

// Owned-state chip on a signal-copy card. Customer vocabulary only.
const ownedChip = (tone: "ready" | "action" | "attention"): React.CSSProperties => {
  const m = {
    ready: { bg: "rgba(34,197,94,0.14)", border: "rgba(34,197,94,0.35)", text: "#86efac" },
    action: { bg: "rgba(59,130,246,0.14)", border: "rgba(59,130,246,0.35)", text: "#93c5fd" },
    attention: { bg: "rgba(245,158,11,0.14)", border: "rgba(245,158,11,0.40)", text: "#fcd34d" },
  }[tone];
  return {
    display: "inline-flex",
    alignItems: "center",
    padding: "0.15rem 0.5rem",
    borderRadius: 999,
    border: `1px solid ${m.border}`,
    background: m.bg,
    color: m.text,
    fontSize: "0.72rem",
    fontWeight: 700,
    whiteSpace: "nowrap",
  };
};

const freeBadgeStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "0.15rem 0.55rem",
  borderRadius: 999,
  border: "1px solid rgba(34,197,94,0.35)",
  background: "rgba(34,197,94,0.12)",
  color: "#86efac",
  fontSize: "0.72rem",
  fontWeight: 700,
  whiteSpace: "nowrap",
};

// ─────────────────────────────────────────────────────────────────────
// Seeded Marketplace Strategies
// ─────────────────────────────────────────────────────────────────────
const MARKETPLACE_SEED: MarketplaceStrategy[] = [
  {
    id: "mp-001",
    name: "London Session Box Breakout",
    category: "Breakout",
    accent: "purple",
    timeframes: ["M15", "M30"],
    pairs: ["GBPUSD", "EURUSD", "GBPJPY"],
    tags: ["Template"],
  },
  {
    id: "mp-002",
    name: "Trend EMA Crossover (HTF filter)",
    category: "Trend",
    accent: "blue",
    timeframes: ["M15", "H1"],
    pairs: ["EURUSD", "USDJPY", "AUDUSD"],
    tags: ["Template"],
  },
  {
    id: "mp-003",
    name: "Bollinger Mean Reversion",
    category: "Reversion",
    accent: "green",
    timeframes: ["M5", "M15"],
    pairs: ["EURUSD", "GBPUSD", "USDCHF"],
    tags: ["Example"],
  },
  {
    id: "mp-004",
    name: "Head & Shoulders Reversal",
    category: "Patterns",
    accent: "yellow",
    timeframes: ["H1", "H4"],
    pairs: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    tags: ["Beta"],
  },
  {
    id: "mp-005",
    name: "Trendline Break Pocket",
    category: "System-grade",
    accent: "cyan",
    timeframes: ["H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "Ali"],
  },
  {
    id: "mp-006",
    name: "Adaptive Liquidity Trap Scalper",
    category: "System-grade",
    accent: "purple",
    timeframes: ["M5", "M15"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "ALTS"],
  },
  {
    id: "mp-007",
    name: "Structural Continuation Engine",
    category: "System-grade",
    accent: "purple",
    timeframes: ["H1", "H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "SCE"],
  },
  {
    id: "mp-009",
    name: "TBP V3 Hybrid Sleeve v1",
    category: "System-grade",
    accent: "cyan",
    timeframes: ["H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "Hybrid"],
  },
  {
    id: "mp-010",
    // NOTE: `name` is an internal codename shown to customers — a product/marketing rename is
    // recommended (see product review WS-I) but deferred because the arm flow keys the created Strategy
    // off this name; its display copy resolves through the EN/JA catalogue.
    name: "Wayond WIM Strategy",
    category: "System-grade",
    accent: "green",
    timeframes: ["M15"],
    pairs: ["XAUUSD"],
    tags: ["Signal copy", "Demo"],
    signalCopy: true,
  },
];

// ─────────────────────────────────────────────────────────────────────
// Main Component
// ─────────────────────────────────────────────────────────────────────
export default function StrategyMarketplacePage() {
  const router = useRouter();
  const lang = useLang();

  const LS_DEFAULT_ACCOUNT_KEY = "guvfx_marketplace_default_account_id";

  const [search, setSearch] = useState("");
  const [activeFilter, setActiveFilter] = useState<MarketCategory | "All">("All");
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  // Per-card busy state for the "Get Strategy" acquisition action.
  const [getting, setGetting] = useState<Record<string, boolean>>({});
  const [selectedAccount, setSelectedAccount] = useState<Record<string, number | "">>({});
  // Per-card owned/enabled state for signal-copy strategies (switches the CTA to "Configure" once owned).
  const [copyState, setCopyState] = useState<Record<string, CardCopyState>>({});
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const [defaultAccountId, setDefaultAccountId] = useState<number | null>(null);
  const [alert, setAlert] = useState<string | null>(null);
  const [alertType, setAlertType] = useState<"info" | "error" | "success">("info");

  const [authChecked, setAuthChecked] = useState(false);
  const [isAuthed, setIsAuthed] = useState(false);

  // ─────────────────────────────────────────────────────────────────────
  // Auth Check (cookie auth)
  // ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    const checkAuth = async () => {
      try {
        await apiFetch("/api/auth/me/", { method: "GET" });
        setIsAuthed(true);
      } catch {
        setIsAuthed(false);
      } finally {
        setAuthChecked(true);
      }
    };

    checkAuth();
  }, []);

  // ─────────────────────────────────────────────────────────────────────
  // Fetch Accounts
  // ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!authChecked) return;   // auth not resolved yet — stay in the loading state (don't flash "no account")
    if (!isAuthed) {            // definitively unauthenticated — there are no accounts to load
      setLoadingAccounts(false);
      return;
    }

    const fetchAccounts = async () => {
      // The pre-auth run of this effect already set loadingAccounts=false; mark it true again for the
      // duration of the real fetch so the blocked-state guard (`!loadingAccounts && …`) never flashes a
      // false "no account" while accounts are still loading.
      setLoadingAccounts(true);
      try {
        // Try primary endpoint
        const data = await apiFetch<TradingAccount[]>("/api/trading/accounts/");
        setAccounts(data);
      } catch {
        // Fallback endpoint
        try {
          const data = await apiFetch<TradingAccount[]>("/api/trading/trading-accounts/");
          setAccounts(data);
        } catch {
          // Quietly fail - marketplace should still render
          setAccounts([]);
        }
      } finally {
        setLoadingAccounts(false);
      }
    };
    fetchAccounts();
  }, [authChecked, isAuthed]);

  // ─────────────────────────────────────────────────────────────────────
  // Fetch owned/enabled status for signal-copy strategies (e.g. Wayond WIM). Read-only; drives the CTA
  // ("Get Strategy" vs "Configure") and the owned-state chip. Fail-soft: an error leaves a not-owned card.
  // ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!authChecked || !isAuthed) return;
    const copyCards = MARKETPLACE_SEED.filter((s) => s.signalCopy);
    if (copyCards.length === 0) return;
    copyCards.forEach(async (card) => {
      try {
        const st = await fetchSignalCopyStatus(card.id);
        setCopyState((prev) => ({ ...prev, [card.id]: { ...st, loading: false } }));
      } catch {
        setCopyState((prev) => ({ ...prev, [card.id]: { armed: false, enabled: false, loading: false } }));
      }
    });
  }, [authChecked, isAuthed]);

  // Load saved default account for marketplace dropdowns — but only one the CURRENT user owns.
  // localStorage is per-browser, not per-user: a default persisted by a previous session/user (e.g.
  // account #1 on a shared machine) must never leak into this session, so we wait for the owned-accounts
  // list and apply the saved default only when it is actually owned.
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (loadingAccounts) return;                       // wait until we know what this user owns

    const raw = window.localStorage.getItem(LS_DEFAULT_ACCOUNT_KEY);
    if (!raw) return;

    const n = Number(raw);
    if (!(Number.isFinite(n) && n > 0)) return;
    if (!accounts.some((a) => a.id === n)) return;     // stale / foreign default → do not apply

    setDefaultAccountId(n);
    // Preselect this account for all seed strategies, without clobbering an in-session pick.
    setSelectedAccount((prev) => {
      const map: Record<string, number | ""> = {};
      for (const s of MARKETPLACE_SEED) map[s.id] = n;
      return { ...map, ...prev };
    });
  }, [accounts, loadingAccounts]);

  // ─────────────────────────────────────────────────────────────────────
  // Filtered Strategies
  // ─────────────────────────────────────────────────────────────────────
  const filteredStrategies = useMemo(() => {
    let result = MARKETPLACE_SEED;

    // Category filter
    if (activeFilter !== "All") {
      result = result.filter((s) => s.category === activeFilter);
    }

    // Search filter
    if (search.trim()) {
      const q = search.toLowerCase();
      result = result.filter(
        (s) =>
          s.name.toLowerCase().includes(q) ||
          t(lang, `marketplace.strategy.${s.id}.summary`).toLowerCase().includes(q) ||
          s.pairs.some((p) => p.toLowerCase().includes(q))
      );
    }

    return result;
  }, [search, activeFilter, lang]);

  // ─────────────────────────────────────────────────────────────────────
  // Persist the last-used account (per browser) as the default for next time.
  // ─────────────────────────────────────────────────────────────────────
  const persistDefaultAccount = (accountId: number) => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(LS_DEFAULT_ACCOUNT_KEY, String(accountId));
    setDefaultAccountId(accountId);
  };

  const configureHref = (strategyId: string, accountId?: number | "") =>
    accountId
      ? `/strategies/configure?mp=${encodeURIComponent(strategyId)}&account=${accountId}`
      : `/strategies/configure?mp=${encodeURIComponent(strategyId)}`;

  // ─────────────────────────────────────────────────────────────────────
  // GET STRATEGY — acquire the strategy for the chosen account WITHOUT enabling execution, then hand off to
  // the Configure page. For signal-copy this creates the owned (non-executing, is_active=False) assignment
  // via /signal-copy/get; for generic templates it uses the existing marketplace assign. Neither creates an
  // order, arms, or authorizes execution — that is the deliberate, confirmed Enable step on Configure.
  // ─────────────────────────────────────────────────────────────────────
  const handleGet = async (strategy: MarketplaceStrategy) => {
    const accountId = selectedAccount[strategy.id];
    if (!accountId || typeof accountId !== "number") {
      setAlert(t(lang, "marketplace.alertSelectAccount"));
      setAlertType("error");
      return;
    }

    setGetting((g) => ({ ...g, [strategy.id]: true }));
    try {
      if (strategy.signalCopy) {
        await getStrategy(strategy.id, accountId);
      } else {
        await apiFetch("/api/strategies/strategies/marketplace/assign/", {
          method: "POST",
          body: JSON.stringify({ marketplace_strategy_id: strategy.id, account_id: accountId }),
        });
      }
      persistDefaultAccount(accountId);
      // Hand off to Configure — never dump the customer straight into My Strategies.
      router.push(configureHref(strategy.id, accountId));
    } catch (err) {
      handleGetError(err, strategy);
    } finally {
      setGetting((g) => ({ ...g, [strategy.id]: false }));
    }
  };

  // Map Get failures to customer-safe copy (never a raw slug/detail/HTML). Session-expiry and account-not-
  // ready get their own wording; everything else falls back to a generic retriable message.
  const handleGetError = (err: unknown, strategy: MarketplaceStrategy) => {
    const e = err as { status?: number; httpStatus?: number; body?: { status?: string }; message?: string };
    const msg = (e?.message || "").toLowerCase();
    const httpStatus = e?.status ?? e?.httpStatus;
    if (httpStatus === 401 || msg.includes("unauthorized")) {
      setAlert(t(lang, "marketplace.alertSessionExpired"));
      setAlertType("error");
      setIsAuthed(false);
      return;
    }
    if (httpStatus === 403 || msg.toUpperCase().includes("ENTITLEMENT")) {
      setAlert(t(lang, strategy.signalCopy ? "marketplace.armNotPilotApproved" : "marketplace.alertPlanRestricted"));
      setAlertType("error");
      return;
    }
    const slug = e?.body?.status;
    if (slug === "account_not_ready") {
      setAlert(t(lang, "marketplace.armAccountNotReady"));
      setAlertType("error");
      return;
    }
    if (slug === "not_pilot_approved") {
      setAlert(t(lang, "marketplace.armNotPilotApproved"));
      setAlertType("error");
      return;
    }
    if (slug === "arming_disabled") {
      setAlert(t(lang, "marketplace.armDisabled"));
      setAlertType("error");
      return;
    }
    if (httpStatus === 404) {
      setAlert(t(lang, strategy.signalCopy ? "marketplace.armAccountNotFound" : "marketplace.alertEndpointNotFound"));
      setAlertType("error");
      return;
    }
    setAlert(t(lang, "marketplace.getFailed"));
    setAlertType("error");
  };

  // ─────────────────────────────────────────────────────────────────────
  // Render
  // ─────────────────────────────────────────────────────────────────────
  return (
    <LocalizedBetaSurface lang={lang}>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        {/* Header */}
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>{t(lang, "marketplace.title")}</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
          {t(lang, "marketplace.subtitle")}
        </p>
        <p style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.35rem" }}>
          {t(lang, "legal.microDisclaimer")}
        </p>
        <p style={{ fontSize: "0.72rem", color: "#64748b", marginBottom: "1.5rem", lineHeight: 1.5 }}>
          {t(lang, "marketplace.disclaimerLine1")}
        </p>

        {authChecked && !isAuthed && (
          <div
            style={{
              marginBottom: "1rem",
              padding: "0.75rem 1rem",
              borderRadius: 8,
              border: "1px solid rgba(239,68,68,0.35)",
              background: "rgba(239,68,68,0.08)",
              color: "#fca5a5",
              fontSize: "0.9rem",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: "0.75rem",
              flexWrap: "wrap",
            }}
          >
            <div>
              {t(lang, "marketplace.unauthMessage")}
            </div>
            <button
              type="button"
              onClick={() => router.push("/login?reason=unauthenticated")}
              style={{
                background: "rgba(59,130,246,0.18)",
                border: "1px solid rgba(59,130,246,0.40)",
                color: "#93c5fd",
                padding: "0.35rem 0.75rem",
                borderRadius: 999,
                fontSize: "0.85rem",
                fontWeight: 700,
                cursor: "pointer",
              }}
            >
              {t(lang, "marketplace.goToLogin")}
            </button>
          </div>
        )}

        {/* Alert */}
        {alert && (
          <div
            style={{
              marginBottom: "1rem",
              padding: "0.75rem 1rem",
              borderRadius: 8,
              border: `1px solid ${
                alertType === "error"
                  ? "rgba(239,68,68,0.4)"
                  : alertType === "success"
                  ? "rgba(34,197,94,0.4)"
                  : "rgba(59,130,246,0.4)"
              }`,
              background: `${
                alertType === "error"
                  ? "rgba(239,68,68,0.1)"
                  : alertType === "success"
                  ? "rgba(34,197,94,0.1)"
                  : "rgba(59,130,246,0.1)"
              }`,
              color: `${
                alertType === "error" ? "#fca5a5" : alertType === "success" ? "#86efac" : "#93c5fd"
              }`,
              fontSize: "0.875rem",
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
              <span>{alert}</span>

              {alertType === "success" && (
                <button
                  type="button"
                  onClick={() => router.push("/strategies")}
                  style={{
                    background: "rgba(59,130,246,0.18)",
                    border: "1px solid rgba(59,130,246,0.40)",
                    color: "#93c5fd",
                    padding: "0.25rem 0.6rem",
                    borderRadius: 999,
                    fontSize: "0.78rem",
                    fontWeight: 700,
                    cursor: "pointer",
                  }}
                >
                  {t(lang, "marketplace.viewMyStrategies")}
                </button>
              )}
            </div>
            <button
              onClick={() => setAlert(null)}
              style={{
                background: "none",
                border: "none",
                color: "inherit",
                cursor: "pointer",
                fontSize: "1.25rem",
                lineHeight: 1,
                padding: "0 0.25rem",
              }}
            >
              ×
            </button>
          </div>
        )}

        {/* Search + Filters */}
        <div style={{ marginBottom: "1.5rem" }}>
          <input
            type="text"
            placeholder={t(lang, "marketplace.searchPlaceholder")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{
              width: "100%",
              padding: "0.65rem 1rem",
              borderRadius: 10,
              border: "1px solid rgba(255,255,255,0.12)",
              background: "rgba(10,16,35,0.6)",
              color: "#e2e8f0",
              fontSize: "0.9rem",
              marginBottom: "1rem",
            }}
          />

          <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
            {(["All", "Trend", "Breakout", "Reversion", "Patterns", "System-grade"] as const).map((cat) => {
              const isActive = activeFilter === cat;
              const filterKey = `marketplace.filter${cat}` as const;
              return (
                <button
                  key={cat}
                  onClick={() => setActiveFilter(cat)}
                  style={{
                    padding: "0.4rem 0.9rem",
                    borderRadius: 999,
                    border: isActive ? "1px solid rgba(59,130,246,0.5)" : "1px solid rgba(255,255,255,0.15)",
                    background: isActive ? "rgba(59,130,246,0.2)" : "rgba(10,16,35,0.4)",
                    color: isActive ? "#93c5fd" : "#b7c5dd",
                    fontSize: "0.8rem",
                    fontWeight: 600,
                    cursor: "pointer",
                    transition: "all 0.2s",
                  }}
                >
                  {t(lang, filterKey)}
                </button>
              );
            })}
          </div>
        </div>

        {/* Strategy Cards Grid */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
            gap: "1.25rem",
          }}
        >
          {filteredStrategies.map((strategy) => {
            const st = copyState[strategy.id];
            const owned = !!strategy.signalCopy && !!st?.armed;
            const enabled = !!st?.enabled;
            const ambiguous = !!st?.ambiguous;
            const priceSpec = priceFor(strategy.id);
            const price = priceSpec.kind === "free" ? t(lang, "configure.free") : priceLabel(priceSpec);
            return (
            <div
              key={strategy.id}
              style={{
                ...glassCardStyle,
                padding: "1.25rem",
                display: "flex",
                flexDirection: "column",
              }}
            >
              {/* ── Zone 1 — Header ── */}
              <div style={{ marginBottom: "0.75rem" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.6rem" }}>
                  <span style={pillStyle(strategy.accent)}>{t(lang, `marketplace.filter${strategy.category}`)}</span>
                  <div style={{ display: "flex", gap: "0.4rem" }}>
                    {strategy.tags?.map((tag) => (
                      <span key={tag} style={badgeStyle()}>
                        {t(lang, `marketplace.tag.${tag.toLowerCase().replaceAll(" ", "-")}`)}
                      </span>
                    ))}
                  </div>
                </div>
                <h3 style={{ fontSize: "1.1rem", fontWeight: 600, color: "#e2e8f0", margin: 0 }}>
                  {strategy.name}
                </h3>
              </div>

              {/* ── Zone 2 — Summary ── */}
              <p
                style={{
                  fontSize: "0.85rem",
                  color: "#94a3b8",
                  lineHeight: 1.5,
                  marginBottom: "0.75rem",
                  display: "-webkit-box",
                  WebkitLineClamp: 3,
                  WebkitBoxOrient: "vertical" as const,
                  overflow: "hidden",
                }}
              >
                {t(lang, `marketplace.strategy.${strategy.id}.summary`)}
              </p>

              {/* Pairs + Timeframes */}
              <div style={{ marginBottom: "0.75rem" }}>
                <div style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.2rem" }}>{t(lang, "marketplace.pairsLabel")}</div>
                <div style={{ fontSize: "0.8rem", color: "#cbd5e1", marginBottom: "0.4rem" }}>{strategy.pairs.join(", ")}</div>
                <div style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.2rem" }}>
                  {t(lang, "marketplace.timeframesLabel")}
                </div>
                <div style={{ fontSize: "0.8rem", color: "#cbd5e1" }}>{strategy.timeframes.join(", ")}</div>
              </div>


              {/* ── Zone 4 — Footer / Actions (bottom-anchored) ── */}
              <div style={{ marginTop: "auto" }}>
                {/* Template Info */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(3, 1fr)",
                    gap: "0.75rem",
                    padding: "0.75rem",
                    borderRadius: 8,
                    background: "rgba(0,0,0,0.25)",
                    marginBottom: "0.75rem",
                  }}
                >
                  <div>
                    <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>
                      {t(lang, "marketplace.styleLabel")}
                    </div>
                    <div style={{ fontSize: "0.8rem", fontWeight: 600, color: "#e2e8f0" }}>
                      {t(lang, `marketplace.strategy.${strategy.id}.style`)}
                    </div>
                  </div>
                  <div>
                    <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>
                      {t(lang, "marketplace.timeframesLabel")}
                    </div>
                    <div style={{ fontSize: "0.8rem", fontWeight: 600, color: "#e2e8f0" }}>
                      {strategy.timeframes.join(", ")}
                    </div>
                  </div>
                  <div>
                    <div style={{ fontSize: "0.65rem", color: "#64748b", marginBottom: "0.2rem" }}>
                      {t(lang, "marketplace.executionLabel")}
                    </div>
                    <div style={{ fontSize: "0.8rem", fontWeight: 600, color: strategy.signalCopy ? "#94a3b8" : "#e2e8f0" }}>
                      {t(lang, `marketplace.strategy.${strategy.id}.execution`)}
                    </div>
                  </div>
                </div>

                {/* Price + owned-state row (uniform across all cards — extensible for future paid pricing) */}
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: "0.6rem" }}>
                  <span style={{ fontSize: "0.7rem", color: "#64748b" }}>{t(lang, "marketplace.priceLabel")}</span>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                    {owned && !ambiguous && (
                      <span style={ownedChip(enabled ? "ready" : "action")}>
                        {enabled ? t(lang, "marketplace.stateEnabled") : t(lang, "marketplace.stateOwned")}
                      </span>
                    )}
                    {ambiguous && (
                      <span style={ownedChip("attention")}>{t(lang, "marketplace.stateNeedsAttention")}</span>
                    )}
                    <span style={freeBadgeStyle}>{price}</span>
                  </div>
                </div>

                {/* CTA */}
                {authChecked && !isAuthed ? (
                  // A logged-out card carries its OWN next action (a sign-in link), never an inert button.
                  <div style={{ fontSize: "0.72rem", color: "#94a3b8" }}>
                    <p style={{ margin: "0 0 6px" }}>{t(lang, "marketplace.getNeedsSignIn")}</p>
                    <Link href="/login?reason=unauthenticated" style={{ color: "#93c5fd", textDecoration: "none" }}>
                      {t(lang, "marketplace.goToLogin")} →
                    </Link>
                  </div>
                ) : owned || ambiguous ? (
                  // Already owned (signal-copy) → jump straight to Configure/Manage. Never guess-to-find.
                  <Button variant="primary" onClick={() => router.push(configureHref(strategy.id))} disabled={!isAuthed}>
                    {enabled || ambiguous ? t(lang, "marketplace.manage") : t(lang, "marketplace.configure")}
                  </Button>
                ) : authChecked && isAuthed && !loadingAccounts && accounts.length === 0 ? (
                  // No account to acquire into — say exactly what's missing and the next action.
                  <div style={{ fontSize: "0.72rem", color: "#94a3b8" }}>
                    <p style={{ margin: "0 0 6px" }}>{t(lang, "marketplace.getNeedsAccount")}</p>
                    <Link href="/accounts" style={{ color: "#93c5fd", textDecoration: "none" }}>
                      {t(lang, "marketplace.readinessAddAccount")} →
                    </Link>
                  </div>
                ) : (
                  // Acquisition: account dropdown + a single "Get Strategy" action.
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                    <select
                      value={selectedAccount[strategy.id] || ""}
                      onChange={(e) => {
                        const nextVal = e.target.value ? Number(e.target.value) : "";
                        setSelectedAccount({
                          ...selectedAccount,
                          [strategy.id]: nextVal,
                        });

                        if (typeof window !== "undefined") {
                          if (nextVal === "") {
                            window.localStorage.removeItem(LS_DEFAULT_ACCOUNT_KEY);
                            setDefaultAccountId(null);
                          } else {
                            window.localStorage.setItem(LS_DEFAULT_ACCOUNT_KEY, String(nextVal));
                            setDefaultAccountId(nextVal);
                          }
                        }
                      }}
                      disabled={loadingAccounts}
                      style={{
                        width: "100%",
                        padding: "0.5rem",
                        borderRadius: 8,
                        border: "1px solid rgba(255,255,255,0.15)",
                        background: "rgba(10,16,35,0.6)",
                        color: "#e2e8f0",
                        fontSize: "0.85rem",
                      }}
                    >
                      <option value="">{t(lang, "marketplace.selectAccount")}</option>
                      {accounts.map((acc) => (
                        <option key={acc.id} value={acc.id}>
                          {acc.name}
                        </option>
                      ))}
                    </select>
                    <Button
                      variant="primary"
                      onClick={() => handleGet(strategy)}
                      disabled={!isAuthed || !selectedAccount[strategy.id] || getting[strategy.id]}
                    >
                      {getting[strategy.id] ? t(lang, "marketplace.getting") : t(lang, "marketplace.getStrategy")}
                    </Button>
                    {!selectedAccount[strategy.id] && (
                      <p style={{ fontSize: "0.68rem", color: "#64748b", margin: 0 }}>
                        {t(lang, "marketplace.getSelectAccountHint")}
                      </p>
                    )}
                  </div>
                )}
              </div>
            </div>
            );
          })}
        </div>

        {/* Empty State */}
        {filteredStrategies.length === 0 && (
          <div style={{ textAlign: "center", padding: "3rem 1rem", color: "#64748b" }}>
            <p style={{ fontSize: "1rem" }}>{t(lang, "marketplace.emptyTitle")}</p>
            <p style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}>{t(lang, "marketplace.emptyHint")}</p>
          </div>
        )}
      </div>
    </LocalizedBetaSurface>
  );
}
