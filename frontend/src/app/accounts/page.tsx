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
  const [accessToken, setAccessToken] = useState<string>("");
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [assignments, setAssignments] = useState<StrategyAssignment[]>([]);
  const [assignmentsLoading, setAssignmentsLoading] = useState(false);
  const [assignmentsError, setAssignmentsError] = useState<string | null>(null);
  const [strategies, setStrategies] = useState<StrategySummary[]>([]);
  const router = useRouter();
  const [testingId, setTestingId] = useState<number | null>(null);
  const [testMessage, setTestMessage] = useState<string | null>(null);

  const [jobsByAccount, setJobsByAccount] = useState<
    Record<number, ExecutionJob[]>
  >({});
  const [jobsLoading, setJobsLoading] = useState(false);
  const [jobsError, setJobsError] = useState<string | null>(null);

  // New account form
  const [name, setName] = useState("");
  const [brokerName, setBrokerName] = useState(""); // user-typed query OR selected server_name
  const [selectedBrokerServer, setSelectedBrokerServer] =
    useState<BrokerServerSuggestion | null>(null);
  const [brokerSuggestions, setBrokerSuggestions] = useState<
    BrokerServerSuggestion[]
  >([]);
  const [activeIdx, setActiveIdx] = useState<number>(-1);
  const visibleBrokerSuggestions = brokerSuggestions.slice(0, 8);
  const [brokerSuggestLoading, setBrokerSuggestLoading] = useState(false);
  const [brokerSuggestError, setBrokerSuggestError] = useState<string | null>(
    null
  );
  const [accountNumber, setAccountNumber] = useState("");
  const [platformPassword, setPlatformPassword] = useState("");
  const [isDemo, setIsDemo] = useState(true);
  const [creating, setCreating] = useState(false);

  const assignmentsByAccount = useMemo(() => {
    const map = new Map<number, StrategyAssignment[]>();
    assignments.forEach((assignment) => {
      const existing = map.get(assignment.account) ?? [];
      existing.push(assignment);
      map.set(assignment.account, existing);
    });
    return map;
  }, [assignments]);

  const strategyLookup = useMemo(() => {
    const map = new Map<number, StrategySummary>();
    strategies.forEach((strategy) => {
      map.set(strategy.id, strategy);
    });
    return map;
  }, [strategies]);

  const labelStyle: React.CSSProperties = {
    color: "#8fa0b7",
    fontSize: "0.84rem",
    marginRight: 4,
  };

  const valueStyle: React.CSSProperties = {
    color: "#e9f4ff",
    fontSize: "0.86rem",
  };

  // Helper to group jobs by account, sorted by created_at descending, max 5 per account
  const buildJobsByAccount = (jobs: ExecutionJob[]): Record<number, ExecutionJob[]> => {
    const grouped: Record<number, ExecutionJob[]> = {};
    const sorted = [...jobs].sort((a, b) => {
      const aTime = Date.parse(a.created_at);
      const bTime = Date.parse(b.created_at);
      return bTime - aTime; // newest first
    });

    for (const job of sorted) {
      const accId = job.account;
      if (!grouped[accId]) {
        grouped[accId] = [];
      }
      if (grouped[accId].length < 5) {
        grouped[accId].push(job);
      }
    }

    return grouped;
  };

  // Load token
  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      }
    }
  }, []);

  // Fetch accounts
  useEffect(() => {
    if (!accessToken) return;

    const fetchAccounts = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await apiFetch<TradingAccount[]>(
          "/api/trading/accounts/",
          {},
          accessToken
        );
        setAccounts(data);
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error
            ? err.message
            : "Failed to load trading accounts.";
        setError(message);
      } finally {
        setLoading(false);
      }
    };

    fetchAccounts();
  }, [accessToken]);

  useEffect(() => {
    if (!accessToken) return;

    const fetchAssignments = async () => {
      setAssignmentsLoading(true);
      setAssignmentsError(null);
      try {
        const [assigns, strategyList] = await Promise.all([
          apiFetch<StrategyAssignment[]>(
            "/api/strategies/assignments/",
            {},
            accessToken
          ),
          apiFetch<StrategySummary[]>(
            "/api/strategies/strategies/",
            {},
            accessToken
          ),
        ]);
        setAssignments(assigns);
        setStrategies(strategyList);
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error
            ? err.message
            : "Failed to load strategy assignments.";
        setAssignmentsError(message);
      } finally {
        setAssignmentsLoading(false);
      }
    };

    fetchAssignments();
  }, [accessToken]);

  useEffect(() => {
    if (!accessToken) return;

    const fetchJobs = async () => {
      setJobsLoading(true);
      setJobsError(null);
      try {
        const jobs = await apiFetch<ExecutionJob[]>(
          "/api/execution/jobs/",
          {},
          accessToken
        );
        setJobsByAccount(buildJobsByAccount(jobs));
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error
            ? err.message
            : "Failed to load execution jobs.";
        setJobsError(message);
      } finally {
        setJobsLoading(false);
      }
    };

    fetchJobs();
  }, [accessToken]);

  // Poll for job updates every 5 seconds, but do not toggle jobsLoading.
  useEffect(() => {
    if (!accessToken) return;

    const intervalId = setInterval(async () => {
      try {
        const jobs = await apiFetch<ExecutionJob[]>(
          "/api/execution/jobs/",
          {},
          accessToken
        );
        setJobsByAccount(buildJobsByAccount(jobs));
      } catch (err) {
        console.error("Failed to refresh execution jobs:", err);
      }
    }, 5000);

    return () => clearInterval(intervalId);
  }, [accessToken]);
  // Broker server autocomplete (suggest endpoint)
  useEffect(() => {
    if (!accessToken) return;

    const q = brokerName.trim();
    if (selectedBrokerServer && q === selectedBrokerServer.server_name) return;

    if (q.length < 2) {
      setBrokerSuggestions([]);
      setBrokerSuggestError(null);
      return;
    }

    setBrokerSuggestError(null);

    const controller = new AbortController();
    const timeoutId = setTimeout(async () => {
      setBrokerSuggestLoading(true);
      try {
        const demoParam = isDemo ? "true" : "false";
        const url = `/api/trading/broker-servers/suggest/?q=${encodeURIComponent(
          q
        )}&demo=${demoParam}`;
        const data = await apiFetch<BrokerServerSuggestion[]>(
          url,
          { signal: controller.signal },
          accessToken
        );
        setBrokerSuggestions(data);
      } catch (err) {
        if ((err as { name?: string }).name === "AbortError") {
          return;
        }
        console.error("Failed to fetch broker suggestions:", err);
        setBrokerSuggestions([]);
        setBrokerSuggestError("Unable to load broker servers.");
      } finally {
        setBrokerSuggestLoading(false);
      }
    }, 300);

    return () => {
      clearTimeout(timeoutId);
      controller.abort();
    };
  }, [accessToken, brokerName, isDemo, selectedBrokerServer]);

  useEffect(() => {
    setActiveIdx(-1);
  }, [brokerName, brokerSuggestions]);

  const handleSelectBrokerSuggestion = (suggestion: BrokerServerSuggestion) => {
    setSelectedBrokerServer(suggestion);
    setBrokerName(suggestion.server_name);
    setBrokerSuggestions([]);
    setActiveIdx(-1);
    setBrokerSuggestError(null);
  };

  const handleBrokerInputKeyDown = (
    e: React.KeyboardEvent<HTMLInputElement>
  ) => {
    const suggestionsCount = visibleBrokerSuggestions.length;

    if (e.key === "ArrowDown") {
      if (suggestionsCount > 0) {
        e.preventDefault();
        setActiveIdx((prev) => Math.min(suggestionsCount - 1, prev + 1));
      }
      return;
    }

    if (e.key === "ArrowUp") {
      if (suggestionsCount > 0) {
        e.preventDefault();
        setActiveIdx((prev) => Math.max(-1, prev - 1));
      }
      return;
    }

    if (e.key === "Enter") {
      if (activeIdx >= 0 && activeIdx < suggestionsCount) {
        e.preventDefault();
        handleSelectBrokerSuggestion(visibleBrokerSuggestions[activeIdx]);
      }
      return;
    }

    if (e.key === "Escape") {
      setBrokerSuggestions([]);
      setActiveIdx(-1);
    }
  };

  const handleCreateAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setInfo(null);

    if (!name || !accountNumber) {
      setError("Please fill in account name and account number.");
      return;
    }
    
    // Require either a selected broker server (recommended) or a manual broker/server name.
    if (!selectedBrokerServer && !brokerName.trim()) {
      setError("Please select a broker server (recommended) or type the broker server name.");
      return;
    }

    // Safety: if a broker server is selected, ensure it matches the chosen account type.
    if (selectedBrokerServer) {
      const expectedEnv = isDemo ? "demo" : "live";
      if (selectedBrokerServer.environment !== expectedEnv) {
        setError(
          `Selected broker server is ${selectedBrokerServer.environment.toUpperCase()} but account type is ${expectedEnv.toUpperCase()}. Please pick a matching server.`
        );
        return;
      }
    }
    if (!accessToken) {
      setError("No token found. Please log in again.");
      return;
    }

    setCreating(true);
    try {
      const body: Record<string, unknown> = {
        name,
        account_number: accountNumber,
        is_demo: isDemo,
        is_active: true,
      };
      
      if (selectedBrokerServer) {
        body.broker_server = selectedBrokerServer.id;
      } else {
        body.broker_name = brokerName.trim();
      }
      
      // Password is write-only; backend stores encrypted.
      if (platformPassword) {
        body.password = platformPassword;
      }

      await apiFetch<TradingAccount>(
        "/api/trading/accounts/",
        {
          method: "POST",
          body: JSON.stringify(body),
        },
        accessToken
      );

      setInfo("Trading account created successfully.");
      setName("");
      setBrokerName("");
      setSelectedBrokerServer(null);
      setBrokerSuggestions([]);
      setAccountNumber("");
      setPlatformPassword("");
      setIsDemo(true);

      // Refresh the list
      const data = await apiFetch<TradingAccount[]>(
        "/api/trading/accounts/",
        {},
        accessToken
      );
      setAccounts(data);
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error
          ? err.message
          : "Failed to create trading account.";
      setError(message);
    } finally {
      setCreating(false);
    }
  };

  const handleTestConnection = async (accountId: number) => {
    setError(null);
    setTestMessage(null);

    if (!accessToken) {
      setError("No token found. Please log in again.");
      return;
    }

    setTestingId(accountId);
    try {
      const body = {
        job_type: "TEST_CONNECTION",
        account: accountId,
        payload: {},
      };

      const job = await apiFetch<ExecutionJob>(
        "/api/execution/jobs/",
        {
          method: "POST",
          body: JSON.stringify(body),
        },
        accessToken
      );

      setTestMessage(
        `Connection test job #${job.id} queued for this account.`
      );

      // Refresh recent execution jobs so the new job appears without a full page reload.
      try {
        const jobs = await apiFetch<ExecutionJob[]>(
          "/api/execution/jobs/",
          {},
          accessToken
        );
        setJobsByAccount(buildJobsByAccount(jobs));
      } catch (refreshErr) {
        console.error(
          "Failed to refresh execution jobs after test:",
          refreshErr
        );
      }
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error
          ? err.message
          : "Failed to queue MT5 connection test.";
      setError(message);
    } finally {
      setTestingId(null);
    }
  };

  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
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
          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              No token found. Please log in again.
            </p>
          )}

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
              <Button type="submit" disabled={creating || !accessToken}>
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

          {!loading && accounts.length === 0 && accessToken && !error && (
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
            {accounts.map((acc) => {
              const accountAssignments =
                assignmentsByAccount.get(acc.id) ?? [];
              const serverName =
                ((acc as { server_name?: string }).server_name ||
                  acc.broker_name);
              return (
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
                      alignItems: "center",
                      marginBottom: "0.3rem",
                    }}
                  >
                    <div>
                      <h3
                        style={{
                          fontSize: "1rem",
                          margin: 0,
                          color: "#f1f5ff",
                        }}
                      >
                        {acc.name}{" "}
                        <span
                          style={{
                            fontSize: "0.8rem",
                            fontWeight: 400,
                            color: "#8897b2",
                            marginLeft: 8,
                          }}
                        >
                          #{acc.id}
                        </span>
                      </h3>
                      <p
                        style={{
                          margin: 0,
                          fontSize: "0.8rem",
                          color: "#8fa0b7",
                        }}
                      >
                        <span style={labelStyle}>Broker server:</span>
                        <span style={valueStyle}>{serverName}</span>
                      </p>
                    </div>
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        alignItems: "flex-end",
                        gap: 4,
                      }}
                    >
                      <div style={{ display: "flex", gap: 8 }}>
                        <Badge color={acc.is_demo ? "blue" : "green"}>
                          {acc.is_demo ? "Demo" : "Live"}
                        </Badge>
                        <Badge color={acc.is_active ? "green" : "gray"}>
                          {acc.is_active ? "Active" : "Inactive"}
                        </Badge>
                      </div>
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => handleTestConnection(acc.id)}
                        disabled={testingId === acc.id || !accessToken}
                        style={{ padding: "0.25rem 0.6rem", fontSize: "0.8rem" }}
                      >
                        {testingId === acc.id ? "Testing…" : "Test MT5 connection"}
                      </Button>
                    </div>
                  </div>

                  <p
                    style={{
                      margin: 0,
                      fontSize: "0.85rem",
                      color: "#c9d7f2",
                    }}
                  >
                    <span style={labelStyle}>Account number:</span>
                    <span style={valueStyle}>{acc.account_number}</span>
                  </p>

                  <p
                    style={{
                      margin: "0.2rem 0 0",
                      fontSize: "0.78rem",
                      color: "#7c8ca4",
                    }}
                  >
                    <span style={labelStyle}>Created:</span>
                    <span style={valueStyle}>
                      {new Date(acc.created_at).toLocaleString()}
                    </span>
                  </p>

                  {(() => {
                    if (accountAssignments.length === 0) {
                      return null;
                    }
                    return (
                      <div
                        style={{
                          marginTop: "0.4rem",
                          display: "flex",
                          flexWrap: "wrap",
                          gap: "0.4rem",
                        }}
                      >
                        {accountAssignments.map((a) => {
                          const linkedStrategy = strategyLookup.get(a.strategy);
                          const label =
                            linkedStrategy?.name ?? `Strategy #${a.strategy}`;

                          return (
                            <button
                              key={a.id}
                              type="button"
                              onClick={() => router.push(`/strategies/${a.strategy}`)}
                              style={{
                                borderRadius: 999,
                                border: "1px solid rgba(148,163,184,0.45)",
                                background: a.is_active
                                  ? "rgba(34,197,94,0.10)"
                                  : "rgba(15,23,42,0.90)",
                                color: a.is_active ? "#4ade80" : "#9ca3af",
                                fontSize: "0.78rem",
                                padding: "0.18rem 0.6rem",
                                display: "inline-flex",
                                alignItems: "center",
                                gap: 6,
                                cursor: "pointer",
                              }}
                            >
                              <span
                                style={{
                                  fontSize: "0.7rem",
                                }}
                              >
                                ●
                              </span>
                              <span>{label}</span>
                              {!a.is_active && (
                                <span style={{ fontSize: "0.7rem", opacity: 0.8 }}>
                                  paused
                                </span>
                              )}
                            </button>
                          );
                        })}
                      </div>
                    );
                  })()}

                  <div
                    style={{
                      marginTop: "0.6rem",
                      fontSize: "0.8rem",
                      color: "#7c8ca4",
                    }}
                  >
                    <div
                      style={{
                        fontSize: "0.78rem",
                        marginBottom: 4,
                        color: "#94a3b8",
                      }}
                    >
                      Assigned strategies
                    </div>
                    {assignmentsLoading && (
                      <p
                        style={{
                          margin: 0,
                          fontSize: "0.75rem",
                          color: "#94a3b8",
                        }}
                      >
                        Loading assignments…
                      </p>
                    )}
                    {!assignmentsLoading &&
                      accountAssignments.length === 0 && (
                        <p
                          style={{
                            margin: 0,
                            fontSize: "0.75rem",
                            color: "#94a3b8",
                          }}
                        >
                          No strategies linked yet.
                        </p>
                      )}
                    {accountAssignments.length > 0 && (
                      <div
                        style={{
                          display: "flex",
                          flexWrap: "wrap",
                          gap: "0.35rem",
                        }}
                      >
                        {accountAssignments.map((assignment) => {
                          const linkedStrategy =
                            strategyLookup.get(assignment.strategy);

                          return (
                            <div
                              key={assignment.id}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                gap: "0.35rem",
                                padding: "0.25rem 0.4rem",
                                borderRadius: 6,
                                background: "rgba(15, 23, 42, 0.9)",
                                border: "1px solid #1f2a44",
                              }}
                            >
                              <Link
                                href={`/strategies/${assignment.strategy}`}
                                style={{
                                  fontSize: "0.8rem",
                                  color: "#e5f4ff",
                                  textDecoration: "none",
                                  fontWeight: 600,
                                }}
                              >
                                {linkedStrategy?.name ??
                                  `Strategy #${assignment.strategy}`}
                              </Link>
                              <Badge
                                color={assignment.is_active ? "green" : "gray"}
                              >
                                {assignment.is_active ? "Active" : "Inactive"}
                              </Badge>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  {/* Recent execution jobs */}
                  <div
                    style={{
                      marginTop: "0.6rem",
                      fontSize: "0.8rem",
                      color: "#7c8ca4",
                    }}
                  >
                    <div
                      style={{
                        fontSize: "0.78rem",
                        marginBottom: 4,
                        color: "#94a3b8",
                      }}
                    >
                      Recent execution jobs
                    </div>

                    {jobsLoading && (
                      <p
                        style={{
                          margin: 0,
                          fontSize: "0.75rem",
                          color: "#94a3b8",
                        }}
                      >
                        Loading jobs…
                      </p>
                    )}

                    {jobsError && !jobsLoading && (
                      <p
                        style={{
                          margin: 0,
                          fontSize: "0.75rem",
                          color: "#f97373",
                        }}
                      >
                        {jobsError}
                      </p>
                    )}

                    {!jobsLoading &&
                      !jobsError &&
                      (jobsByAccount[acc.id]?.length ?? 0) === 0 && (
                        <p
                          style={{
                            margin: 0,
                            fontSize: "0.75rem",
                            color: "#94a3b8",
                          }}
                        >
                          No execution jobs for this account yet.
                        </p>
                      )}

                    {!jobsLoading &&
                      !jobsError &&
                      (jobsByAccount[acc.id]?.length ?? 0) > 0 && (
                        <div
                          style={{
                            display: "flex",
                            flexDirection: "column",
                            gap: "0.3rem",
                            marginTop: "0.2rem",
                          }}
                        >
                          {jobsByAccount[acc.id]!.map((job) => {
                            const statusColor =
                              job.status === "SUCCESS"
                                ? "#4ade80"
                                : job.status === "FAILED"
                                ? "#f97373"
                                : "#e5e7eb";

                            let message: string | undefined;
                            if (
                              job.result &&
                              typeof job.result === "object" &&
                              "message" in job.result
                            ) {
                              const maybeMessage = (job.result as Record<string, unknown>).message;
                              if (typeof maybeMessage === "string") {
                                message = maybeMessage;
                              }
                            }

                            let title = job.job_type;
                            let titleColor = "#e5f4ff";

                            if (
                              job.job_type === "OPEN_TRADE" &&
                              job.payload &&
                              typeof job.payload === "object"
                            ) {
                              const payload = job.payload as Record<string, unknown>;
                              const symbolVal = payload["symbol"];
                              const directionVal = payload["direction"];
                              const symbol =
                                typeof symbolVal === "string" && symbolVal.trim() !== ""
                                  ? symbolVal.trim()
                                  : undefined;
                              const directionRaw =
                                typeof directionVal === "string" && directionVal.trim() !== ""
                                  ? directionVal.trim().toUpperCase()
                                  : undefined;

                              if (symbol || directionRaw) {
                                title = [directionRaw, symbol].filter(Boolean).join(" ");
                              } else {
                                title = "Open trade";
                              }

                              if (directionRaw === "BUY") {
                                titleColor = "#4ade80"; // green for BUY
                              } else if (directionRaw === "SELL") {
                                titleColor = "#f97373"; // red for SELL
                              }
                            }

                            return (
                              <div
                                key={job.id}
                                style={{
                                  display: "flex",
                                  flexDirection: "column",
                                  padding: "0.25rem 0.4rem",
                                  borderRadius: 6,
                                  border: "1px solid #1f2937",
                                  background: "rgba(15,23,42,0.9)",
                                }}
                              >
                                <div
                                  style={{
                                    display: "flex",
                                    justifyContent: "space-between",
                                    alignItems: "center",
                                  }}
                                >
                                  <span
                                    style={{
                                      fontSize: "0.78rem",
                                      color: titleColor,
                                    }}
                                  >
                                    {title}
                                  </span>
                                  <span
                                    style={{
                                      fontSize: "0.78rem",
                                      color: statusColor,
                                      fontWeight: 600,
                                    }}
                                  >
                                    {job.status}
                                  </span>
                                </div>
                                <div
                                  style={{
                                    fontSize: "0.72rem",
                                    color: "#9ca3af",
                                    marginTop: "0.15rem",
                                  }}
                                >
                                  {new Date(job.created_at).toLocaleString()}
                                </div>
                                {message && (
                                  <div
                                    style={{
                                      fontSize: "0.72rem",
                                      color: "#cbd5f5",
                                      marginTop: "0.15rem",
                                    }}
                                  >
                                    {message}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                  </div>
                </div>
              );
            })}
          </div>
        </Card>
      </div>
    </AppShell>
  );
}
