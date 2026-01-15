/* 
 * ReleaseBot note:
 * Baseline snapshot uses `any` in transitional UI code.
 * Suppressed to allow baseline reconciliation.
 * Must be cleaned up in follow-up PRs.
 */
/* eslint-disable @typescript-eslint/no-explicit-any */
"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import React, { useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type {
  StrategyAssignment,
  TradingAccount,
} from "@/types/strategies";

type HelpIconProps = {
  text: string;
};

type StrategySummary = {
  id: number;
  name: string;
  is_active: boolean;
};

type ExecutionJob = {
  id: number;
  job_type: string;
  status: string;
  account: number;
  strategy: number | null;
  created_at: string;
  result?: Record<string, unknown>;
  payload?: Record<string, unknown>;
};
type BrokerServerSuggestion = {
  id: string;
  broker_display_name: string;
  server_name: string;
  environment: "demo" | "live";
};
const HelpIcon: React.FC<HelpIconProps> = ({ text }) => {
    const [visible, setVisible] = useState(false);
    const [align, setAlign] = useState<"left" | "right">("right");
    const wrapperRef = React.useRef<HTMLSpanElement | null>(null);
  
    const handleMouseEnter = () => {
      if (wrapperRef.current && typeof window !== "undefined") {
        const rect = wrapperRef.current.getBoundingClientRect();
        const viewportWidth = window.innerWidth;
        const midpoint = viewportWidth / 2;
  
        // If the icon is on the left half of the screen, open tooltip to the right.
        // If it's on the right half, open tooltip to the left.
        if (rect.left < midpoint) {
          setAlign("right");
        } else {
          setAlign("left");
        }
      }
      setVisible(true);
    };
  
    const handleMouseLeave = () => {
      setVisible(false);
    };
  
    return (
      <span
        ref={wrapperRef}
        style={{
          position: "relative",
          display: "inline-block",
          marginLeft: 6,
          cursor: "default",
        }}
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
      >
        <span
          style={{
            display: "inline-flex",
            width: 16,
            height: 16,
            borderRadius: 4,
            alignItems: "center",
            justifyContent: "center",
            fontSize: "0.7rem",
            fontWeight: 600,
            background: "rgba(148,163,184,0.6)",
            color: "#020617",
          }}
        >
          ?
        </span>
        {visible && (
          <span
            style={{
              position: "absolute",
              bottom: "115%",
              left: align === "right" ? 0 : "auto",
              right: align === "left" ? 0 : "auto",
              transform: "translateY(-4px)",
              background: "#020617",
              color: "#e5f4ff",
              padding: "0.6rem 0.8rem",
              borderRadius: 4,
              fontSize: "0.8rem",
              width: 340,
              maxWidth: "min(340px, 80vw)",
              border: "1px solid rgba(148,163,184,0.9)",
              boxShadow: "0 8px 24px rgba(0,0,0,0.75)",
              zIndex: 9999,
              whiteSpace: "normal",
            }}
          >
            {text}
          </span>
        )}
      </span>
    );
  };

export default function AccountsPage() {


  const loadAccounts = async () => {
    setLoading(true);
    setError(null);
    try {
      const list = await apiFetch<any[]>("/api/trading/accounts/");
      setAccounts(Array.isArray(list) ? list : []);
    } catch (err: any) {
      setError(err?.message || "Failed to load trading accounts");
      setAccounts([]);
    } finally {
      setLoading(false);
    }
  };


  useEffect(() => {
    loadAccounts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [assignments, setAssignments] = useState<StrategyAssignment[]>([]);

  const assignmentsByAccount = useMemo(() => {
    const m = new Map<number, StrategyAssignment[]>();
    for (const a of assignments) {
      const accountId: number | undefined = (a as any).account ?? (a as any).account_id;
      if (typeof accountId !== "number") continue;
      const cur = m.get(accountId) ?? [];
      cur.push(a);
      m.set(accountId, cur);
    }
    return m;
  }, [assignments]);



  const router = useRouter();
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);

  const strategyLookup = useMemo(() => {
    const m = new Map<number, StrategySummary>();
    for (const st of strategies) {
      m.set(st.id, st);
    }
    return m;
  }, [strategies]);

  const [testingId, setTestingId] = useState<number | null>(null);
  const [activeTogglingId, setActiveTogglingId] = useState<number | null>(null);
  // UI helpers (restored after refactor)
  const labelStyle: React.CSSProperties = { fontSize: "0.85rem", color: "#94a3b8" };
  const valueStyle: React.CSSProperties = { fontSize: "0.85rem", color: "#e5f4ff" };

  const [loading, setLoading] = useState<boolean>(false);
  const [assignmentsError, setAssignmentsError] = useState<string | null>(null);
  const [assignmentsLoading, setAssignmentsLoading] = useState<boolean>(false);
  const [jobsLoading, setJobsLoading] = useState<boolean>(false);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<ExecutionJob[]>([]);

  // Derived: account_id -> execution jobs[]
  const jobsByAccount = useMemo(() => {
    const out: Record<number, ExecutionJob[]> = {};
    for (const j of jobs) {
      const accountId = (j as any).account;
      if (typeof accountId !== "number") continue;
      (out[accountId] ||= []).push(j);
    }
    return out;
  }, [jobs]);

  const [info, setInfo] = useState<string | null>(null);
  const [testMessage, setTestMessage] = useState<string | null>(null);

  // Restored form state (cookie-auth refactor fallout)
  const [name, setName] = useState<string>("");
  const [brokerName, setBrokerName] = useState<string>("");
  const [accountNumber, setAccountNumber] = useState<string>("");
  const [platformPassword, setPlatformPassword] = useState<string>("");
  const [isDemo, setIsDemo] = useState<boolean>(true);
  const [selectedBrokerServer, setSelectedBrokerServer] = useState<BrokerServerSuggestion | null>(null);
  const [brokerSuggestions, setBrokerSuggestions] = useState<BrokerServerSuggestion[]>([]);

  // Derived list used by the suggestions dropdown (restore after refactor)
  const visibleBrokerSuggestions = useMemo(() => brokerSuggestions, [brokerSuggestions]);

  useEffect(() => {
    // reset highlight when suggestions list changes
    setActiveIdx(0);
  }, [visibleBrokerSuggestions.length]);

  const [activeIdx, setActiveIdx] = useState<number>(0);

  const [brokerSuggestLoading, setBrokerSuggestLoading] = useState<boolean>(false);
  const [brokerSuggestError, setBrokerSuggestError] = useState<string | null>(null);

  // Handles broker input keyboard UX (stubbed for now).
  // - Enter: pick the first suggested server (if any)
  // - Escape: clear suggestions
  const handleBrokerInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      if (brokerSuggestions.length > 0) {
        e.preventDefault();
        setSelectedBrokerServer(brokerSuggestions[0] ?? null);
      }
    } else if (e.key === "Escape") {
      setBrokerSuggestions([]);
      setSelectedBrokerServer(null);
    }
  };




  // Select a broker server suggestion from the dropdown
  const handleSelectBrokerSuggestion = (s: BrokerServerSuggestion) => {
    setSelectedBrokerServer(s);
    // Keep brokerName aligned with selected suggestion (helps UX + later POST body)
    setBrokerName(s.server_name || s.broker_display_name || "");
    setBrokerSuggestions([]);
    setActiveIdx(0);
  };

  // TEMP: Restored stub after refactor. Replace with real create-account logic.
    const [creating, setCreating] = useState<boolean>(false);

  const handleCreateAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setError(null);
    setInfo(null);

    try {
      // Atomic: ask MT5 to login + validate (Windows Agent EA), then create the DB record
      const res = await apiFetch<{
        ok: boolean;
        valid: boolean;
        reason?: string;
        created?: boolean;
        account?: any;
        agent?: any;
      }>("/api/trading/accounts/add-with-mt5-login/", {
        method: "POST",
        body: JSON.stringify({
          name,
          broker_name: brokerName, // free-text server name (or switch to broker_server later)
          account_number: accountNumber,
          password: platformPassword,
          is_demo: isDemo,
        }),
      });

      if (!res.ok) {
        throw new Error("Windows agent/backend error while validating MT5 login.");
      }

      if (!res.valid) {
        throw new Error(res.reason || "Unable to add account. Login details are not valid.");
      }

      // Refresh list so the UI updates
      await loadAccounts();

      // Clear form
      setName("");
      setBrokerName("");
      setAccountNumber("");
      setPlatformPassword("");
      setSelectedBrokerServer(null);
      setBrokerSuggestions([]);

      setInfo("✅ Account added / MT5 login successful.");
    } catch (err: any) {
      setError(err?.message || "Failed to create account");
    } finally {
      setCreating(false);
    }
  };


  // Test connection (stubbed for now)
  const handleTestConnection = async (accountId: number) => {
    setTestMessage(null);
    setError(null);
    setTestingId(accountId);
    try {
      // TODO: restore real endpoint call
      // await apiFetch(`/api/trading/accounts/${accountId}/test/`, { method: "POST" });
      const res = await apiFetch<{ ok: boolean; valid: boolean; reason?: string }>(
        `/api/trading/accounts/${accountId}/test/`,
        { method: "POST" }
     );

     if (res.valid) {
       setTestMessage("✅ MT5 session matches this account (EA validation OK).");
     } else {
       setTestMessage(`❌ Not matched: ${res.reason || "invalid"}`);
     }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Test failed.";
      setError(msg);
    } finally {
      setTestingId(null);
    }
  };

  const handleToggleActive = async (accId: number, nextActive: boolean) => {
    setError(null);
    setInfo(null);
    try {
      await apiFetch(`/api/trading/accounts/${accId}/set-active/`, {
        method: "POST",
        body: JSON.stringify({ is_active: nextActive }),
      });

      // refresh list
      const list = await apiFetch<any[]>("/api/trading/accounts/");
      setAccounts(Array.isArray(list) ? list : []);
      setInfo(nextActive ? "Account set to ACTIVE." : "Account set to INACTIVE.");
    } catch (err: any) {
      setError(err?.message || "Failed to change active status");
    }
  };

  const toggleActive = async (accId: number, next: boolean) => {
    setError(null);
    try {
      await apiFetch(`/api/trading/accounts/${accId}/set-active/`, {
        method: "POST",
        body: JSON.stringify({ is_active: next }),
      });

      // reload accounts
      const list = await apiFetch<any[]>("/api/trading/accounts/");
      setAccounts(Array.isArray(list) ? list : []);
    } catch (err: any) {
      setError(err?.message || "Failed to update active account");
    }
  };
  const [error, setError] = useState<string | null>(null);
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  
  // --- MT5 RDP session (Guacamole hidden) ---
  
  // --- MT5 RDP session (Guacamole hidden) ---
  const [mt5Url, setMt5Url] = useState<string>("");
  const [mt5Loading, setMt5Loading] = useState(false);
  const [mt5Error, setMt5Error] = useState<string | null>(null);

  // Fetch a fresh Guacamole URL (does NOT change the iframe URL)
  const getMt5Url = async (): Promise<string> => {
    const data = await apiFetch<{ ok: boolean; launch_url: string; expires_in_seconds: number }>(
      "/api/mt5/launch/",
      { method: "POST" }
    );
    return data.launch_url;
  };

  // Preview MT5 in the embedded iframe (sets mt5Url)
  const launchMt5 = async (): Promise<string> => {
    setMt5Error(null);
    setMt5Loading(true);
    try {
      const url = await getMt5Url();
      setMt5Url(url);
      return url;
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to launch MT5.";
      setMt5Error(msg);
      throw err;
    } finally {
      setMt5Loading(false);
    }
  };

return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      
      {/* --- MT5 Terminal (Preview + Fullscreen) --- */}
      <div style={{ marginBottom: "1.25rem", padding: "1rem", border: "1px solid rgba(255,255,255,0.12)", borderRadius: 12 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: "0.95rem", fontWeight: 600 }}>MT5 Terminal</div>
            <div style={{ fontSize: "0.85rem", opacity: 0.8 }}>
              Preview embedded. For best trading experience, open fullscreen.
            </div>
          </div>

          <div style={{ display: "flex", gap: "0.5rem" }}>
            <button
              type="button"
              onClick={() => launchMt5()}
              disabled={mt5Loading}
              style={{
                padding: "0.55rem 0.9rem",
                borderRadius: 10,
                border: "1px solid rgba(255,255,255,0.18)",
                background: "rgba(74,179,255,0.12)",
                color: "#e5f4ff",
                cursor: mt5Loading ? "not-allowed" : "pointer",
              }}
            >
              {mt5Loading ? "Launching…" : "Preview MT5"}
            </button>

            <button
              type="button"
              onClick={async () => {
                const url = await getMt5Url();
                window.open(url, "_blank", "noopener,noreferrer");
              }}
              disabled={mt5Loading}
              style={{
                padding: "0.55rem 0.9rem",
                borderRadius: 10,
                border: "1px solid rgba(255,255,255,0.18)",
                background: "rgba(255,255,255,0.06)",
                color: "#e5f4ff",
                cursor: mt5Loading ? "not-allowed" : "pointer",
              }}
            >
              Open Fullscreen
            </button>
          </div>
        </div>

        <div style={{ marginTop: "0.75rem", borderRadius: 12, overflow: "hidden", border: "1px solid rgba(255,255,255,0.10)" }}>
          {mt5Url ? (
            <iframe
              key={mt5Url}
              src={mt5Url}
              title="MT5 Terminal"
              style={{ width: "100%", height: 520, border: 0, display: "block", background: "black" }}
              allow="clipboard-read; clipboard-write"
            />
          ) : (
            <div style={{ height: 520, display: "flex", alignItems: "center", justifyContent: "center", opacity: 0.7 }}>
              Click “Preview MT5” to start.
            </div>
          )}
        </div>
      </div>

<h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          Trading Accounts
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Link your broker / MT5 accounts so GuvFX can map strategies and trades.
        </p>

        {error && <Alert type="error">{error}</Alert>}
        {info && <Alert type="info">{info}</Alert>}
        {testMessage && <Alert type="info">{testMessage}</Alert>}

        {/* New account form */}
        <Card
          title="Add Trading Account"
          subtitle="Create a link to a broker or MT5 account. GuvFX will use this for mapping strategies and trades."
        >

          <form onSubmit={handleCreateAccount}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                gap: "0.75rem 1.5rem",
              }}
            >
              <div>
                <label
                  htmlFor="acc-name"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Account name
                  <HelpIcon text="This is a friendly name for you to recognise the account on your list." />
                </label>
                <input
                  id="acc-name"
                  type="text"
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="e.g. Main MT5"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>

              <div>
                <label
                  htmlFor="broker-name"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Broker server name
                  <HelpIcon text="This is the server name of your broker! If you are unsure, check directly with your broker what this is. It is usually in the email you receive from your broker with your access details." />
                </label>

                <input
                  id="broker-name"
                  type="text"
                  value={brokerName}
                  onChange={(e) => {
                    setBrokerName(e.target.value);
                    setSelectedBrokerServer(null);
                  }}
                  onKeyDown={handleBrokerInputKeyDown}
                  placeholder="e.g. Broker-Live01 or Broker-Demo02"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />

                {brokerSuggestLoading && brokerName.trim().length >= 2 && (
                  <div style={{ marginTop: 6, fontSize: "0.8rem", color: "#94a3b8" }}>
                    Searching broker servers…
                  </div>
                )}

                {!brokerSuggestLoading && visibleBrokerSuggestions.length > 0 && (
                  <div
                    style={{
                      marginTop: 6,
                      border: "1px solid rgba(148,163,184,0.45)",
                      borderRadius: 8,
                      background: "rgba(3, 7, 18, 0.98)",
                      overflow: "hidden",
                    }}
                  >
                    {visibleBrokerSuggestions.map((s, index) => {
                      const isActive = index === activeIdx;
                      return (
                        <div
                          key={s.id}
                          onMouseDown={(e) => {
                            e.preventDefault();
                            handleSelectBrokerSuggestion(s);
                          }}
                          style={{
                            padding: "0.55rem 0.7rem",
                            cursor: "pointer",
                            display: "flex",
                            justifyContent: "space-between",
                            gap: 10,
                            borderTop: "1px solid rgba(148,163,184,0.12)",
                            background: isActive
                              ? "rgba(59,130,246,0.2)"
                              : "transparent",
                          }}
                        >
                          <div style={{ display: "flex", flexDirection: "column" }}>
                            <span
                              style={{
                                color: "#e5f4ff",
                                fontSize: "0.86rem",
                                fontWeight: 600,
                              }}
                            >
                              {s.server_name}
                            </span>
                            <span style={{ color: "#94a3b8", fontSize: "0.78rem" }}>
                              {s.broker_display_name}
                            </span>
                          </div>
                          <span
                            style={{
                              fontSize: "0.75rem",
                              color: s.environment === "demo" ? "#60a5fa" : "#4ade80",
                              border: "1px solid rgba(148,163,184,0.25)",
                              padding: "0.08rem 0.45rem",
                              borderRadius: 999,
                              height: 20,
                              alignSelf: "center",
                              display: "inline-flex",
                              alignItems: "center",
                            }}
                          >
                            {s.environment.toUpperCase()}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}

                {brokerSuggestError && (
                  <div
                    style={{
                      marginTop: 6,
                      fontSize: "0.8rem",
                      color: "#f87171",
                    }}
                  >
                    {brokerSuggestError}
                  </div>
                )}

                {!brokerSuggestLoading &&
                  !brokerSuggestError &&
                  brokerName.trim().length >= 2 &&
                  brokerSuggestions.length === 0 &&
                  (!selectedBrokerServer ||
                    brokerName.trim() !== selectedBrokerServer.server_name) && (
                    <div
                      style={{
                        marginTop: 6,
                        fontSize: "0.8rem",
                        color: "#94a3b8",
                      }}
                    >
                      No matching broker servers found.
                    </div>
                  )}

                {selectedBrokerServer && (
                  <div style={{ marginTop: 6, fontSize: "0.78rem", color: "#94a3b8" }}>
                    Selected:{" "}
                    <span style={{ color: "#e5f4ff" }}>
                      {selectedBrokerServer.broker_display_name}
                    </span>
                  </div>
                )}
              </div>

              <div>
                <label
                  htmlFor="account-number"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Account number / login
                  <HelpIcon text="This is the account number used to login via your broker's MetaTrader account." />
                </label>
                <input
                  id="account-number"
                  type="text"
                  required
                  value={accountNumber}
                  onChange={(e) => setAccountNumber(e.target.value)}
                  placeholder="e.g. 123456"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>

              <div>
                <label
                  htmlFor="platform-password"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Platform password
                  <HelpIcon text="This is the password for your broker's trading platform account (e.g. MetaTrader 5). It will be stored securely and used later to connect to your account." />
                </label>
                <input
                  id="platform-password"
                  type="password"
                  value={platformPassword}
                  onChange={(e) => setPlatformPassword(e.target.value)}
                  placeholder="Password used in MetaTrader / broker platform"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>

              <div>
                <span
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Account type
                </span>
                <label
                  style={{
                    fontSize: "0.85rem",
                    color: "#e5f4ff",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 8,
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={isDemo}
                    onChange={(e) => {
                      setIsDemo(e.target.checked);
                      setSelectedBrokerServer(null);
                      setBrokerSuggestions([]);
                    }}
                    style={{ cursor: "pointer" }}
                  />
                  Demo account
                </label>
              </div>
            </div>

            <div
              style={{
                marginTop: "1rem",
                display: "flex",
                justifyContent: "flex-end",
              }}
            >
              <Button type="submit" disabled={creating}>
                {creating ? "Creating…" : "Add account"}
              </Button>
            </div>
          </form>
        </Card>

        {/* Accounts list */}
        <Card title="Linked Accounts">
          {assignmentsError && (
            <Alert type="error">{assignmentsError}</Alert>
          )}
          {assignmentsLoading && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading strategy assignments…
            </p>
          )}
          {loading && <p>Loading accounts…</p>}

          {!loading && accounts.length === 0 && !error && (
            <p style={{ fontSize: "0.9rem" }}>
              No trading accounts linked yet. Use the form above to add one.
            </p>
          )}

          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: "0.75rem",
            }}
          >
            
            {accounts.map((acc) => (
              <div
                key={acc.id}
                style={{
                  border: "1px solid #222838",
                  borderRadius: 8,
                  padding: "0.75rem 1rem",
                  background: "rgba(7, 12, 30, 0.9)",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                    alignItems: "flex-start",
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                      <div style={{ fontWeight: 700, color: "#e5f4ff", fontSize: "1rem" }}>
                        {acc.name}
                      </div>
                      <Button
                        type="button"
                        variant={acc.is_active ? "primary" : "secondary"}
                        onClick={() => handleToggleActive(acc.id, !acc.is_active)}
                        disabled={accounts.filter((a) => a.is_active).length <= 1 && acc.is_active}
                        style={{ padding: "0.25rem 0.6rem", fontSize: "0.8rem" }}
                    >
                        {acc.is_active ? "Active (click to deactivate)" : "Inactive (click to activate)"}
                      </Button>
                    </div>

                    <div style={{ marginTop: 6, fontSize: "0.85rem", color: "#c9d7f2" }}>
                      <span style={labelStyle}>Account number:</span>{" "}
                      <span style={valueStyle}>{acc.account_number}</span>
                    </div>

                    <div style={{ marginTop: 4, fontSize: "0.85rem", color: "#c9d7f2" }}>
                      <span style={labelStyle}>Broker server:</span>{" "}
                      <span style={valueStyle}>
                        {(acc as any).server_name || acc.broker_name || ""}
                      </span>
                    </div>

                    <div style={{ marginTop: 4, fontSize: "0.78rem", color: "#7c8ca4" }}>
                      <span style={labelStyle}>Created:</span>{" "}
                      <span style={valueStyle}>{new Date(acc.created_at).toLocaleString()}</span>
                    </div>
                  </div>

                  <div
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "flex-end",
                      gap: 8,
                    }}
                  >
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => handleTestConnection(acc.id)}
                      disabled={testingId === acc.id}
                      style={{ padding: "0.25rem 0.6rem", fontSize: "0.8rem" }}
                    >
                      {testingId === acc.id ? "Testing…" : "Test MT5 connection"}
                    </Button>

                    {/* Option A toggle will go here next (cleanly) */}
                  </div>
                </div>
              </div>
            ))}

          </div>
        </Card>
      </div>
    </AppShell>
  );
}
