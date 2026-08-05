"use client";

import { useLang } from "@/components/AppShell";
import { Button } from "@/components/ui/Button";
import { t } from "@/lib/i18n";
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "@/lib/api";
import { brokerConnectivityEnabled } from "@/lib/flags";
import { SignalCopyReadiness } from "@/components/marketplace/SignalCopyReadiness";
import Link from "next/link";
import { useRouter } from "next/navigation";

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
  style: string;
  execution: string;
  summary: string;
  timeframes: string[];
  pairs: string[];
  tags?: string[];
  // Signal-copy strategy (e.g. Wayond WIM): shows an enable/disable toggle that pauses/resumes
  // its already-armed auto-demo assignment instead of the generic Assign flow.
  signalCopy?: boolean;
};

type SignalCopyStatus = {
  armed: boolean;
  enabled: boolean;
  ambiguous?: boolean;
  loading?: boolean;
};

type TradingAccount = {
  id: number;
  name: string;
  broker_name?: string;
  account_number?: string;
  // IPR Area D: both serialized by the backend — used to hint eligibility in the arm selector
  // (arm rejects non-demo / inactive with `account_not_ready`; arm remains the authority).
  is_demo?: boolean;
  is_active?: boolean;
  // IPR Area B (C6): canonical dedicated-runtime readiness — gates the arm affordance in the UI
  // (the backend re-checks it authoritatively).
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

// ─────────────────────────────────────────────────────────────────────
// Seeded Marketplace Strategies
// ─────────────────────────────────────────────────────────────────────
const MARKETPLACE_SEED: MarketplaceStrategy[] = [
  {
    id: "mp-001",
    name: "London Session Box Breakout",
    category: "Breakout",
    accent: "purple",
    style: "Volatility Breakout",
    execution: "Manual review required",
    summary: "Example ruleset for Asian session range breakouts during London open. Review and test before use.",
    timeframes: ["M15", "M30"],
    pairs: ["GBPUSD", "EURUSD", "GBPJPY"],
    tags: ["Template"],
  },
  {
    id: "mp-002",
    name: "Trend EMA Crossover (HTF filter)",
    category: "Trend",
    accent: "blue",
    style: "Trend Following",
    execution: "Manual review required",
    summary: "20/50 EMA cross on M15 with H4 trend alignment. Designed to be configured and tested by the user.",
    timeframes: ["M15", "H1"],
    pairs: ["EURUSD", "USDJPY", "AUDUSD"],
    tags: ["Template"],
  },
  {
    id: "mp-003",
    name: "Bollinger Mean Reversion",
    category: "Reversion",
    accent: "green",
    style: "Mean Reversion",
    execution: "Manual review required",
    summary: "Enters on 2σ touches with RSI divergence. Example template — review and test before use.",
    timeframes: ["M5", "M15"],
    pairs: ["EURUSD", "GBPUSD", "USDCHF"],
    tags: ["Example"],
  },
  {
    id: "mp-004",
    name: "Head & Shoulders Reversal",
    category: "Patterns",
    accent: "yellow",
    style: "Chart Patterns",
    execution: "User-controlled execution",
    summary: "Automated chart pattern recognition for H&S reversals with volume confirmation. Currently in beta — review and test before use.",
    timeframes: ["H1", "H4"],
    pairs: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
    tags: ["Beta"],
  },
  {
    id: "mp-005",
    name: "Trendline Break Pocket",
    category: "System-grade",
    accent: "cyan",
    style: "HTF Zone + Structure",
    execution: "Automation-ready",
    summary: "HTF zone + trendline break + structure shift. Fixed 2R model. Manual zones editable. Designed by Ali.",
    timeframes: ["H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "Ali"],
  },
  {
    id: "mp-006",
    name: "Adaptive Liquidity Trap Scalper",
    category: "System-grade",
    accent: "purple",
    style: "Liquidity / Mean reversion",
    execution: "Automation-ready",
    summary: "Range-regime liquidity sweep + displacement + confirmation. M5 execution with M15 regime filter.",
    timeframes: ["M5", "M15"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "ALTS"],
  },
  {
    id: "mp-007",
    name: "Structural Continuation Engine",
    category: "System-grade",
    accent: "purple",
    style: "Trend continuation",
    execution: "Automation-ready",
    summary: "H4 bias + H1 BOS + pullback + rejection continuation. H1 execution with H4 context.",
    timeframes: ["H1", "H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "SCE"],
  },
  {
    id: "mp-009",
    name: "TBP V3 Hybrid Sleeve v1",
    category: "System-grade",
    accent: "cyan",
    style: "HTF Zone + Macro Overlay",
    execution: "Automation-ready",
    summary: "CORE (TBP trendline break pocket) + SLEEVE (TC1 trend continuation on risk-on days). H4 execution.",
    timeframes: ["H4"],
    pairs: ["EURUSD", "GBPUSD"],
    tags: ["Automation-ready", "Hybrid"],
  },
  {
    id: "mp-010",
    // NOTE: `name` is an internal codename shown to customers — a product/marketing rename is
    // recommended (see product review WS-I) but deferred because the arm flow keys the created Strategy
    // off this name; the `execution`/`summary` below are display-only and are made customer-safe here.
    name: "Wayond WIM Strategy",
    category: "System-grade",
    accent: "green",
    style: "Telegram signal copy",
    execution: "Automated · demo",
    summary: "Automatically mirrors a curated Telegram signal provider into your demo account.",
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
  const [assigning, setAssigning] = useState<Record<string, boolean>>({});
  const [selectedAccount, setSelectedAccount] = useState<Record<string, number | "">>({});
  // Per-card enable/disable state for signal-copy strategies (e.g. Wayond WIM).
  const [copyState, setCopyState] = useState<Record<string, SignalCopyStatus>>({});
  const [copyBusy, setCopyBusy] = useState<Record<string, boolean>>({});
  // IPR Area D: per-card busy state for the self-service Enable-Trading (arm) action.
  const [armBusy, setArmBusy] = useState<Record<string, boolean>>({});
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
  // Fetch enable/disable status for signal-copy strategies (e.g. Wayond WIM)
  // ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (!authChecked || !isAuthed) return;
    const copyCards = MARKETPLACE_SEED.filter((s) => s.signalCopy);
    if (copyCards.length === 0) return;
    copyCards.forEach(async (card) => {
      try {
        const st = await apiFetch<SignalCopyStatus>(
          `/api/strategies/strategies/signal-copy/status/?marketplace_strategy_id=${encodeURIComponent(card.id)}`
        );
        setCopyState((prev) => ({ ...prev, [card.id]: { ...st, loading: false } }));
      } catch {
        // Leave undefined → the card shows an unavailable state without breaking the grid.
        setCopyState((prev) => ({ ...prev, [card.id]: { armed: false, enabled: false, loading: false } }));
      }
    });
  }, [authChecked, isAuthed]);

  // Load saved default account for marketplace dropdowns — but only one the CURRENT user owns.
  // localStorage is per-browser, not per-user: a default persisted by a previous session/user (e.g.
  // account #1 on a shared machine) must never leak into this session. Applying an unowned id makes the
  // read-only signal-copy readiness endpoint 404 ("We couldn't check your account status right now") and
  // would aim Assign at a foreign account. So we wait for the owned-accounts list to load and apply the
  // saved default only when it is actually owned; otherwise we ignore it and let each card pick its own
  // owned account (the signal-copy panel auto-selects the first demo account; generic cards prompt).
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
          s.summary.toLowerCase().includes(q) ||
          s.pairs.some((p) => p.toLowerCase().includes(q))
      );
    }

    return result;
  }, [search, activeFilter]);

  // ─────────────────────────────────────────────────────────────────────
  // Assign Handler
  // ─────────────────────────────────────────────────────────────────────
  const handleAssign = async (strategyId: string) => {
    const accountId = selectedAccount[strategyId];
    if (!accountId) {
      setAlert(t(lang, "marketplace.alertSelectAccount"));
      setAlertType("error");
      return;
    }

    setAssigning({ ...assigning, [strategyId]: true });

    try {
      await apiFetch("/api/strategies/strategies/marketplace/assign/", {
        method: "POST",
        body: JSON.stringify({
          marketplace_strategy_id: strategyId,
          account_id: accountId,
        }),
      });
      setAlert(null); // Clear any previous errors
      setAlert(t(lang, "marketplace.alertAssigned"));
      setAlertType("success");

      // Keep the selected account as the default for next time
      if (typeof window !== "undefined") {
        const v = selectedAccount[strategyId];
        if (typeof v === "number" && v > 0) {
          window.localStorage.setItem(LS_DEFAULT_ACCOUNT_KEY, String(v));
          setDefaultAccountId(v);
        }
      }
    } catch (err) {
      const e = err as { status?: number; message?: string };
      const msg = (e?.message || "").trim();

      // If backend returned an HTML 404 page (common when hitting wrong route),
      // don't dump HTML into the UI.
      const looksLikeHtml =
        msg.toLowerCase().includes("<!doctype") ||
        msg.toLowerCase().includes("<html") ||
        msg.toLowerCase().includes("<body");

      if (e?.status === 401 || msg.toLowerCase().includes("unauthorized")) {
        setAlert(t(lang, "marketplace.alertSessionExpired"));
        setAlertType("error");
        setIsAuthed(false);
        return;
      }

      // WS-I: a plan/entitlement denial (403) must show plain guidance, never the raw backend slug
      // (e.g. ENTITLEMENT_RESTRICTED) that apiFetch surfaces as the error message.
      if (e?.status === 403 || msg.toUpperCase().includes("ENTITLEMENT")) {
        setAlert(t(lang, "marketplace.alertPlanRestricted"));
        setAlertType("error");
        return;
      }

      if (e?.status === 404 || msg.includes("404")) {
        setAlert(t(lang, "marketplace.alertEndpointNotFound"));
        setAlertType("error");
        return;
      }

      if (looksLikeHtml) {
        setAlert(t(lang, "marketplace.alertUnexpectedResponse"));
        setAlertType("error");
        return;
      }

      // WS-I: never dump a raw backend message/slug to the customer — fall back to generic safe copy.
      setAlert(t(lang, "marketplace.alertAssignFailed"));
      setAlertType("error");
    } finally {
      setAssigning({ ...assigning, [strategyId]: false });
    }
  };

  // ─────────────────────────────────────────────────────────────────────
  // Signal-copy enable/disable — pauses/resumes an already-armed auto-demo assignment.
  // Never arms (arming is a separate, gated step); a 409 means "not armed yet".
  // ─────────────────────────────────────────────────────────────────────
  const refreshCopyStatus = async (strategyId: string): Promise<SignalCopyStatus | null> => {
    try {
      const st = await apiFetch<SignalCopyStatus>(
        `/api/strategies/strategies/signal-copy/status/?marketplace_strategy_id=${encodeURIComponent(strategyId)}`
      );
      setCopyState((prev) => ({ ...prev, [strategyId]: { ...st, loading: false } }));
      return st;
    } catch {
      return null;
    }
  };

  const handleSignalCopyToggle = async (strategyId: string, nextEnabled: boolean) => {
    setCopyBusy((b) => ({ ...b, [strategyId]: true }));
    try {
      const res = await apiFetch<{ status: string; enabled: boolean }>(
        "/api/strategies/strategies/signal-copy/toggle/",
        { method: "POST", body: JSON.stringify({ marketplace_strategy_id: strategyId, enabled: nextEnabled }) }
      );
      setCopyState((prev) => ({
        ...prev,
        [strategyId]: { armed: true, enabled: res.enabled, loading: false },
      }));
      setAlert(nextEnabled ? t(lang, "marketplace.copyEnabled") : t(lang, "marketplace.copyDisabled"));
      setAlertType("success");
    } catch (err) {
      const msg = ((err as { message?: string })?.message || "").toLowerCase();
      if (msg.includes("unauthorized")) {
        setAlert(t(lang, "marketplace.alertSessionExpired"));
        setAlertType("error");
        setIsAuthed(false);
        return;
      }
      // WS-G: a still-armed ENABLE (resume) refused by the cohort gate is a PERMANENT denial — the
      // safety-stop Disable stays allowed, but re-enabling needs approval. Say so plainly instead of
      // the generic retriable "try again".
      const slug = (err as { body?: { status?: string } })?.body?.status;
      if (slug === "not_pilot_approved") {
        setAlert(t(lang, "marketplace.armNotPilotApproved"));
        setAlertType("error");
        return;
      }
      // Refetch the authoritative status so the card reflects reality (armed / ambiguous /
      // not-armed) instead of guessing from an opaque error.
      const st = await refreshCopyStatus(strategyId);
      setAlert(
        st?.ambiguous
          ? t(lang, "marketplace.copyAmbiguous")
          : st && !st.armed
            ? t(lang, "marketplace.copyNotArmed")
            : t(lang, "marketplace.copyToggleFailed")
      );
      setAlertType("error");
    } finally {
      setCopyBusy((b) => ({ ...b, [strategyId]: false }));
    }
  };

  // IPR Area D — self-service Enable-Trading (arm). Creates the AUTO_DEMO signal-copy authority for the
  // chosen account (backend `signal_copy_arm`, gated OFF by BETA_SELF_SERVE_ARM_ENABLED and fail-closed).
  // On success we NEVER trust the optimistic click — we re-read the authoritative status so the card's
  // ON state comes only from backend-confirmed `enabled`. Failures branch on the machine-readable
  // `status` slug (not `detail`, which is identical for the two readiness reasons) into customer-safe
  // wording — raw slugs / detail strings are never shown.
  const handleSignalCopyArm = async (strategyId: string, accountId: number) => {
    setArmBusy((b) => ({ ...b, [strategyId]: true }));
    try {
      await apiFetch<{ status: string; enabled: boolean }>(
        "/api/strategies/strategies/signal-copy/arm/",
        { method: "POST", body: JSON.stringify({ marketplace_strategy_id: strategyId, account_id: accountId }) }
      );
      // Refresh-after-arm: authoritative armed/enabled/ambiguous, not the local optimistic state.
      await refreshCopyStatus(strategyId);
      setAlert(t(lang, "marketplace.armSuccess"));
      setAlertType("success");
    } catch (err) {
      const e = err as { httpStatus?: number; body?: { status?: string }; message?: string };
      if ((e?.message || "").toLowerCase().includes("unauthorized")) {
        setAlert(t(lang, "marketplace.alertSessionExpired"));
        setAlertType("error");
        setIsAuthed(false);
        return;
      }
      const slug = e?.body?.status;
      const key =
        slug === "arming_disabled" ? "marketplace.armDisabled"
        // WS-G: permanent / attention denials must map to their OWN customer-safe copy, never the
        // generic retriable "try again" — a default-deny cohort or a failed validation is not transient.
        : slug === "not_pilot_approved" ? "marketplace.armNotPilotApproved"
        : slug === "broker_validation_unhealthy" ? "marketplace.armValidationUnhealthy"
        : slug === "runtime_paused" ? "marketplace.armPaused"
        : slug === "duplicate_active_assignment" ? "marketplace.armDuplicate"
        : slug === "account_not_ready" ? "marketplace.armAccountNotReady"
        : slug === "credentials_missing" ? "marketplace.armCredentialsMissing"
        : slug === "runtime_not_ready" ? "marketplace.armRuntimeNotReady"
        : slug === "broker_not_connected" ? "marketplace.armBrokerNotConnected"
        : slug === "source_single_tenant" ? "marketplace.armSingleTenant"
        : e?.httpStatus === 404 ? "marketplace.armAccountNotFound"
        : "marketplace.armFailed";
      setAlert(t(lang, key));
      setAlertType("error");
    } finally {
      setArmBusy((b) => ({ ...b, [strategyId]: false }));
    }
  };


  // ─────────────────────────────────────────────────────────────────────
  // Render
  // ─────────────────────────────────────────────────────────────────────
  return (
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
          {filteredStrategies.map((strategy) => (
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
                  <span style={pillStyle(strategy.accent)}>{strategy.category}</span>
                  <div style={{ display: "flex", gap: "0.4rem" }}>
                    {strategy.tags?.map((tag) => (
                      <span key={tag} style={badgeStyle()}>
                        {tag}
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
                {strategy.summary}
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
                      {strategy.style}
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
                    <div style={{ fontSize: "0.8rem", fontWeight: 600, color: "#e2e8f0" }}>
                      {strategy.execution}
                    </div>
                  </div>
                </div>

                {/* Signal-copy strategies: enable/disable toggle instead of the Assign flow */}
                {strategy.signalCopy ? (
                  (() => {
                    const st = copyState[strategy.id];
                    const armed = !!st?.armed;
                    const enabled = !!st?.enabled;
                    const ambiguous = !!st?.ambiguous;
                    const busy = !!copyBusy[strategy.id];
                    const arming = !!armBusy[strategy.id];
                    const selAcct = selectedAccount[strategy.id];
                    // The self-service Enable-Trading (arm) button is surfaced ONLY when the broker-
                    // connectivity journey is intentionally built (armUiEnabled) — independent of the
                    // backend BETA_SELF_SERVE_ARM_ENABLED flag, so a DARK build never shows a live arm
                    // control. The readiness PANEL (checklist + next action) always renders so the customer
                    // understands where they are; can_arm from the backend gates the actual button.
                    const armUiEnabled = brokerConnectivityEnabled();
                    // ON/badge state comes ONLY from the backend-confirmed status — never from a click.
                    const statusLabel = ambiguous
                      ? t(lang, "marketplace.copyAmbiguousShort")
                      : !armed
                        ? t(lang, "marketplace.copyNotArmedShort")
                        : enabled
                          ? t(lang, "marketplace.copyOn")
                          : t(lang, "marketplace.copyOff");
                    const statusColor = ambiguous
                      ? "#f59e0b"
                      : !armed
                        ? "#f59e0b"
                        : enabled
                          ? "#22c55e"
                          : "#94a3b8";
                    return (
                      <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
                        <div
                          style={{
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "space-between",
                            padding: "0.5rem 0.75rem",
                            borderRadius: 8,
                            background: "rgba(0,0,0,0.25)",
                          }}
                        >
                          <span style={{ fontSize: "0.7rem", color: "#64748b" }}>
                            {t(lang, "marketplace.copyStatusLabel")}
                          </span>
                          <span style={{ fontSize: "0.8rem", fontWeight: 700, color: statusColor }}>
                            {statusLabel}
                          </span>
                        </div>
                        {armed ? (
                          // Armed → the Enable/Disable toggle (resume/pause the copy) — full width, no
                          // dead Preview affordance.
                          <Button
                            variant={enabled ? "secondary" : "primary"}
                            onClick={() => handleSignalCopyToggle(strategy.id, !enabled)}
                            disabled={!isAuthed || busy}
                          >
                            {busy
                              ? t(lang, "marketplace.copyWorking")
                              : enabled
                                ? t(lang, "marketplace.copyDisable")
                                : t(lang, "marketplace.copyEnable")}
                          </Button>
                        ) : (
                          // Not armed → WS-D: the readiness panel replaces the opaque "not armed" hint.
                          // It ALWAYS renders (customer sees exactly what's needed via a ✓/✕ checklist +
                          // one next action), backed by the read-only readiness endpoint. The Enable-
                          // Trading button inside it appears only when the broker-connectivity journey is
                          // built (armUiEnabled) AND the backend reports can_arm — so a DARK build shows
                          // the guidance but never a live arm control.
                          <SignalCopyReadiness
                            lang={lang}
                            marketplaceStrategyId={strategy.id}
                            accounts={accounts}
                            selectedAccountId={selAcct}
                            onSelectAccount={(v) =>
                              setSelectedAccount({ ...selectedAccount, [strategy.id]: v })
                            }
                            armUiEnabled={armUiEnabled}
                            isAuthed={isAuthed}
                            arming={arming}
                            onArm={(id) => handleSignalCopyArm(strategy.id, id)}
                          />
                        )}
                      </div>
                    );
                  })()
                ) : authChecked && isAuthed && !loadingAccounts && accounts.length === 0 ? (
                  // BLOCKED (WS-G redesign): no account to assign into — shown ONLY once auth is resolved AND
                  // the accounts fetch has completed empty, so we never flash a false "no account" during the
                  // pre-auth window or the fetch. Say exactly what's missing and the next action, instead of a
                  // silently-disabled Assign button with no explanation.
                  <div style={{ fontSize: "0.72rem", color: "#94a3b8" }}>
                    <p style={{ margin: "0 0 6px" }}>{t(lang, "marketplace.assignNeedsAccount")}</p>
                    <Link href="/accounts" style={{ color: "#93c5fd", textDecoration: "none" }}>
                      {t(lang, "marketplace.readinessAddAccount")} →
                    </Link>
                  </div>
                ) : (
                // CTA — dropdown full-width, then a single Assign action (no dead Preview).
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
                    onClick={() => handleAssign(strategy.id)}
                    disabled={!isAuthed || !selectedAccount[strategy.id] || assigning[strategy.id]}
                  >
                    {assigning[strategy.id] ? t(lang, "marketplace.assigning") : t(lang, "marketplace.assign")}
                  </Button>
                  {!selectedAccount[strategy.id] && (
                    <p style={{ fontSize: "0.68rem", color: "#64748b", margin: 0 }}>
                      {t(lang, "marketplace.assignSelectAccountHint")}
                    </p>
                  )}
                </div>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Empty State */}
        {filteredStrategies.length === 0 && (
          <div style={{ textAlign: "center", padding: "3rem 1rem", color: "#64748b" }}>
            <p style={{ fontSize: "1rem" }}>{t(lang, "marketplace.emptyTitle")}</p>
            <p style={{ fontSize: "0.85rem", marginTop: "0.5rem" }}>{t(lang, "marketplace.emptyHint")}</p>
          </div>
        )}
      </div>
  );
}