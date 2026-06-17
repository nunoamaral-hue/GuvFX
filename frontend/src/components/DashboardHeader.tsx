"use client";

import { useEffect, useState } from "react";
import { CircleDot, Wallet, TrendingUp, TrendingDown, Clock } from "lucide-react";
import { apiFetch } from "@/lib/api";

/**
 * Dashboard header (PX Alignment Layer 1).
 *
 * Greeting + subtitle + Account Status Summary card.
 *
 * Data sources (existing endpoints only):
 * - GET  /api/auth/me/                      -> first_name (fallback "Trader")
 * - GET  /api/trading/accounts/             -> active account name/broker
 * - POST /api/trading/accounts/{id}/test/   -> real MT5 connection state (EA validation)
 *
 * Equity and Daily PnL have no backend data source yet; the card renders
 * them as unavailable ("—") rather than inferring or fabricating values.
 */

type Me = {
  id: number;
  email: string;
  username: string;
  first_name?: string;
};

type TradingAccount = {
  id: number;
  name: string;
  broker_display_name?: string;
  server_name?: string;
  broker_name?: string;
  is_active: boolean;
};

type ConnectionState = "checking" | "connected" | "disconnected" | "unknown";

function timeOfDayGreeting(date: Date): string {
  const h = date.getHours();
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

function isLocalhost(): boolean {
  return (
    typeof window !== "undefined" &&
    ["localhost", "127.0.0.1", "0.0.0.0"].includes(window.location.hostname)
  );
}

const labelStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  fontSize: "0.72rem",
  letterSpacing: 1.1,
  textTransform: "uppercase",
  color: "#6b7280",
};

const valueStyle: React.CSSProperties = {
  fontSize: "1.05rem",
  fontWeight: 600,
  color: "#e5f4ff",
  marginTop: 4,
};

export function DashboardHeader() {
  const [firstName, setFirstName] = useState<string | null>(null);
  // Localhost dev skips live API calls (same guard as AppShell); only mounted
  // client-side behind the auth gate, so window access here is safe.
  const [connection, setConnection] = useState<ConnectionState>(() =>
    isLocalhost() ? "unknown" : "checking"
  );
  const [accountLabel, setAccountLabel] = useState<string | null>(null);
  const [asOf, setAsOf] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    // Best-effort enrichment only — same localhost guard as AppShell.
    if (isLocalhost()) {
      const t = setTimeout(() => {
        setAsOf(
          new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
        );
      }, 0);
      return () => clearTimeout(t);
    }

    apiFetch<Me>("/api/auth/me/", {})
      .then((me) => {
        if (!cancelled && me.first_name?.trim()) {
          setFirstName(me.first_name.trim());
        }
      })
      .catch(() => {
        /* fallback to "Trader" */
      });

    apiFetch<TradingAccount[]>("/api/trading/accounts/")
      .then((accounts) => {
        if (cancelled) return;
        const active = accounts.find((a) => a.is_active) ?? accounts[0];
        if (!active) {
          setConnection("disconnected");
          setAsOf(
            new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
          );
          return;
        }
        const broker = active.broker_display_name || active.broker_name || "";
        setAccountLabel(broker ? `${broker} — ${active.name}` : active.name);

        return apiFetch<{ ok: boolean; valid: boolean; reason?: string }>(
          `/api/trading/accounts/${active.id}/test/`,
          { method: "POST" }
        )
          .then((res) => {
            if (cancelled) return;
            setConnection(res.valid ? "connected" : "disconnected");
          })
          .catch(() => {
            if (!cancelled) setConnection("unknown");
          });
      })
      .catch(() => {
        if (!cancelled) setConnection("unknown");
      })
      .finally(() => {
        if (!cancelled) {
          setAsOf(
            new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
          );
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const greeting = timeOfDayGreeting(new Date());
  const name = firstName ?? "Trader";

  const connectionColor =
    connection === "connected"
      ? "#34d399"
      : connection === "disconnected"
        ? "#f87171"
        : "#9ca3af";

  const connectionText =
    connection === "connected"
      ? "Connected"
      : connection === "disconnected"
        ? "Disconnected"
        : connection === "checking"
          ? "Checking…"
          : "Status unavailable";

  // No equity / daily PnL data source exists yet — render honestly as unavailable.
  const equity: number | null = null;
  const dailyPnl: number | null = null;

  const PnlIcon = dailyPnl !== null && dailyPnl < 0 ? TrendingDown : TrendingUp;
  const pnlColor =
    dailyPnl === null ? "#e5f4ff" : dailyPnl < 0 ? "#f87171" : "#34d399";

  return (
    <header
      style={{
        display: "flex",
        flexWrap: "wrap",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: "2rem",
        marginBottom: "2.5rem",
      }}
    >
      {/* Greeting */}
      <div style={{ paddingTop: "0.5rem" }}>
        <h1
          style={{
            fontSize: "2.25rem",
            fontWeight: 650,
            letterSpacing: -0.5,
            margin: 0,
            color: "#e5f4ff",
          }}
        >
          {greeting}, {name} 👋
        </h1>
        <p
          style={{
            fontSize: "1rem",
            color: "#8b9bb4",
            marginTop: "0.6rem",
            marginBottom: 0,
          }}
        >
          Here&apos;s your edge today.
        </p>
      </div>

      {/* Account Status Summary */}
      <section
        aria-label="Account status summary"
        style={{
          minWidth: 260,
          padding: "1.25rem 1.5rem",
          borderRadius: 14,
          border: "1px solid #1f2937",
          background: "rgba(15, 23, 42, 0.6)",
          display: "flex",
          flexDirection: "column",
          gap: "1rem",
        }}
      >
        <div>
          <div style={labelStyle}>
            <CircleDot size={13} color={connectionColor} aria-hidden />
            <span>MT5</span>
          </div>
          <div
            style={{
              ...valueStyle,
              color: connectionColor,
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <span
              aria-hidden
              style={{
                width: 9,
                height: 9,
                borderRadius: 999,
                background: connectionColor,
                boxShadow: `0 0 8px ${connectionColor}`,
              }}
            />
            {connectionText}
          </div>
          {accountLabel && (
            <div style={{ fontSize: "0.78rem", color: "#6b7280", marginTop: 4 }}>
              {accountLabel}
            </div>
          )}
        </div>

        <div>
          <div style={labelStyle}>
            <Wallet size={13} aria-hidden />
            <span>Equity</span>
          </div>
          <div style={valueStyle}>
            {equity !== null
              ? `$${Number(equity).toLocaleString("en-US", {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}`
              : "—"}
          </div>
        </div>

        <div>
          <div style={labelStyle}>
            <PnlIcon size={13} aria-hidden />
            <span>Daily PnL</span>
          </div>
          <div style={{ ...valueStyle, color: pnlColor }}>
            {dailyPnl !== null
              ? `${dailyPnl < 0 ? "-" : "+"}$${Math.abs(dailyPnl).toLocaleString(
                  "en-US",
                  { minimumFractionDigits: 2, maximumFractionDigits: 2 }
                )}`
              : "—"}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            paddingTop: "0.75rem",
            borderTop: "1px solid #1f2937",
            fontSize: "0.75rem",
            color: "#6b7280",
          }}
        >
          <Clock size={12} aria-hidden />
          <span>{asOf ? `as of ${asOf}` : "syncing…"}</span>
        </div>
      </section>
    </header>
  );
}
