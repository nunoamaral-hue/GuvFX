"use client";

import type React from "react";
import { useEffect, useState } from "react";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";

type MeResponse = {
  id: number;
  email: string;
  username: string;
  first_name: string;
  last_name: string;
};

type ChangePasswordResponse = {
  detail: string;
};

type HostingRequest = {
  id: number;
  status: string;
  note: string;
  created_at: string;
};

type HostingPlan = {
  id: number;
  code: string | null;
  name: string;
  description: string;
  cpu_cores: number;
  memory_mb: number;
  disk_gb: number;
  monthly_price_usd: number;
  provider_plan_slug: string;
  hosting_mode: "SESSION_EPHEMERAL" | "DEDICATED" | "SHARED_POOL";
  max_mt5_instances: number;
  supports_autonomous_execution: boolean;
  reset_on_logout: boolean;
  is_shared: boolean;
};

type HostingMe = {
  current_plan_code: string | null;
  current_plan_name: string | null;
  subscription_status: string | null;
  total_mt5_instances: number;
  max_mt5_instances: number;
};

type UserConsole = {
  vps_id: number;
  vps_label: string;
  plan_code: string | null;
  plan_name: string;
  guac_url: string;
  status?: string;
};

export default function ProfilePage() {
  const [accessToken, setAccessToken] = useState<string>("");
  const [me, setMe] = useState<MeResponse | null>(null);
  const [loadingMe, setLoadingMe] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [oldPassword, setOldPassword] = useState("");
  const [newPassword1, setNewPassword1] = useState("");
  const [newPassword2, setNewPassword2] = useState("");
  const [pwLoading, setPwLoading] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwSuccess, setPwSuccess] = useState<string | null>(null);

  const [hostingMe, setHostingMe] = useState<HostingMe | null>(null);
  const [hostingPlans, setHostingPlans] = useState<HostingPlan[]>([]);
  const [hostingRequests, setHostingRequests] = useState<HostingRequest[]>([]);
  const [hostingLoading, setHostingLoading] = useState(false);
  const [hostingError, setHostingError] = useState<string | null>(null);
  const [hostingInfo, setHostingInfo] = useState<string | null>(null);
  const [selectedPlanCode, setSelectedPlanCode] = useState<string | null>(null);
  const [requestingPlan, setRequestingPlan] = useState(false);

  const [userConsoles, setUserConsoles] = useState<UserConsole[]>([]);
  const [consolesLoading, setConsolesLoading] = useState(false);
  const [consolesError, setConsolesError] = useState<string | null>(null);

  const labelStyle: React.CSSProperties = {
    color: "#8fa0b7",
    fontSize: "0.85rem",
    marginRight: 4,
  };

  const valueStyle: React.CSSProperties = {
    color: "#e9f4ff",
    fontSize: "0.9rem",
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

  // Fetch /me
  useEffect(() => {
    

    const fetchMe = async () => {
      setLoadingMe(true);
      setError(null);
      try {
        const data = await apiFetch<MeResponse>("/api/auth/me/", {});
        setMe(data);
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error ? err.message : "Failed to load profile.";
        setError(message);
      } finally {
        setLoadingMe(false);
      }
    };

    fetchMe();
  }, [accessToken]);

  // Fetch hosting info, plans, requests, consoles
  useEffect(() => {
    

    let cancelled = false;

    const run = async () => {
      setHostingLoading(true);
      setHostingError(null);
      setHostingInfo(null);
      setConsolesLoading(true);
      setConsolesError(null);

      try {
        const meData = await apiFetch<HostingMe>(
          "/api/hosting/me/",
          {});
        if (!cancelled) {
          setHostingMe(meData);
        }

        const plansData = await apiFetch<HostingPlan[]>(
          "/api/hosting/plans/",
          {});
        if (!cancelled) {
          const normalized = plansData.map((plan) => ({
            ...plan,
            monthly_price_usd:
              typeof plan.monthly_price_usd === "string"
                ? Number(plan.monthly_price_usd)
                : plan.monthly_price_usd,
          }));
          const sorted = [...normalized].sort(
            (a, b) => a.monthly_price_usd - b.monthly_price_usd
          );
          setHostingPlans(sorted);
        }

        const requestsData = await apiFetch<HostingRequest[]>(
          "/api/hosting/requests/",
          {});
        if (!cancelled) {
          setHostingRequests(requestsData);
        }

        const consolesData = await apiFetch<UserConsole[]>(
          "/api/hosting/my-consoles/",
          {});
        if (!cancelled) {
          setUserConsoles(consolesData);
        }
      } catch (err: unknown) {
        console.error("Failed to load hosting data:", err);
        if (!cancelled) {
          const message =
            err instanceof Error
              ? err.message
              : "Failed to load hosting information.";
          setHostingError(message);
          setConsolesError(message);
        }
      } finally {
        if (!cancelled) {
          setHostingLoading(false);
          setConsolesLoading(false);
        }
      }
    };

    run();

    return () => {
      cancelled = true;
    };
  }, [accessToken]);

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPwError(null);
    setPwSuccess(null);

    if (!newPassword1 || newPassword1.length < 8) {
      setPwError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword1 !== newPassword2) {
      setPwError("New passwords do not match.");
      return;
    }
    if (!accessToken) {
      setPwError("");
      return;
    }

    setPwLoading(true);
    try {
      const body = {
        old_password: oldPassword,
        new_password: newPassword1,
      };

      const res = await apiFetch<ChangePasswordResponse>(
        "/api/auth/change-password/",
        {
          method: "POST",
          body: JSON.stringify(body),
        }
);

      setPwSuccess(res.detail || "Password updated successfully.");
      setOldPassword("");
      setNewPassword1("");
      setNewPassword2("");
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error
          ? err.message
          : "Failed to change password. Please check your old password and try again.";
      setPwError(message);
    } finally {
      setPwLoading(false);
    }
  };

  const handleRequestPlan = async (plan: HostingPlan) => {
    if (!plan.code) {
      setHostingError("Selected plan lacks a code.");
      return;
    }
    if (!accessToken) {
      setHostingError("You need to be logged in to request hosting.");
      return;
    }

    setHostingError(null);
    setHostingInfo(null);
    setRequestingPlan(true);
    setSelectedPlanCode(plan.code);

    try {
      const note = `PLAN:${plan.code} – ${plan.name}`;

      await apiFetch<HostingRequest>(
        "/api/hosting/requests/",
        {
          method: "POST",
          body: JSON.stringify({ note }),
        }
);

      setHostingInfo(
        "Your hosting request has been submitted. An admin will review and activate it."
      );

      const requests = await apiFetch<HostingRequest[]>(
        "/api/hosting/requests/",
        {});
      setHostingRequests(requests);
    } catch (err: unknown) {
      console.error("Failed to request hosting plan:", err);
      setHostingError(
        err instanceof Error ? err.message : "Failed to submit hosting request."
      );
    } finally {
      setRequestingPlan(false);
      setSelectedPlanCode(null);
    }
  };

  return (
      <div style={{ maxWidth: 900, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Profile</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          View your GuvFX account details and update your password.
        </p>

        {error && <Alert type="error">{error}</Alert>}

        {/* Profile details */}
        <Card title="Account Details">
          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              
            </p>
          )}

          {loadingMe && <p>Loading profile…</p>}

          {me && (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                gap: "0.4rem 1.5rem",
              }}
            >
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>ID:</span>
                <span style={valueStyle}>{me.id}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>Email:</span>
                <span style={valueStyle}>{me.email}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>Username:</span>
                <span style={valueStyle}>{me.username}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>First name:</span>
                <span style={valueStyle}>{me.first_name || "—"}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>Last name:</span>
                <span style={valueStyle}>{me.last_name || "—"}</span>
              </p>
            </div>
          )}
        </Card>

        {/* Hosting */}
        <Card title="Hosting" subtitle="MT5 hosting plans and your current status">
          {hostingError && <Alert type="error">{hostingError}</Alert>}
          {hostingInfo && <Alert type="info">{hostingInfo}</Alert>}

          <div
            style={{
              marginBottom: "1rem",
              padding: "0.7rem 0.8rem",
              borderRadius: 8,
              border: "1px solid #111827",
              background: "rgba(7,12,30,0.9)",
              fontSize: "0.85rem",
            }}
          >
            <div style={{ marginBottom: "0.3rem", color: "#e5f4ff" }}>
              <strong>Current subscription:</strong>{" "}
              {hostingMe?.current_plan_name ?? "None"}
            </div>
            <div style={{ color: "#9ca3af" }}>
              Status:{" "}
              <span style={{ textTransform: "capitalize" }}>
                {hostingMe?.subscription_status?.toLowerCase() ?? "none"}
              </span>
              {typeof hostingMe?.total_mt5_instances === "number" &&
                typeof hostingMe?.max_mt5_instances === "number" && (
                  <>
                    {" "}
                    · MT5 instances: {hostingMe.total_mt5_instances}/
                    {hostingMe.max_mt5_instances}
                  </>
                )}
            </div>
          </div>

          <h3
            style={{
              fontSize: "0.9rem",
              color: "#cbd5f5",
              margin: "0 0 0.4rem",
            }}
          >
            Available plans
          </h3>

          {hostingLoading && hostingPlans.length === 0 ? (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading hosting plans…
            </p>
          ) : hostingPlans.length === 0 ? (
            <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
              No hosting plans available at the moment.
            </p>
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))",
                gap: "1rem 1.4rem",
                marginBottom: "1rem",
              }}
            >
              {hostingPlans.map((plan) => {
                const isCurrent = Boolean(
                  hostingMe?.current_plan_code &&
                    plan.code &&
                    hostingMe.current_plan_code === plan.code
                );
                const isSelected = selectedPlanCode === plan.code;
                const disabled = requestingPlan || isCurrent;

                let badgeLabel = "";
                void badgeLabel; // intentionally unused
                let hostingModeLabel = "";
                let planTone = "#38bdf8";
                let isRecommended = false;

                switch (plan.code) {
                  case "FREE_SESSION_MT5":
                    badgeLabel = "Free";
                    hostingModeLabel = "Ephemeral session-only MT5";
                    planTone = "#22c55e";
                    break;
                  case "STANDARD_DEDICATED_2":
                    badgeLabel = "Dedicated";
                    hostingModeLabel = "Dedicated VPS (2 MT5)";
                    planTone = "#38bdf8";
                    isRecommended = true;
                    break;
                  case "MANAGED_SHARED_10":
                    badgeLabel = "Managed shared";
                    hostingModeLabel = "Shared pool (up to 10 MT5)";
                    planTone = "#a855f7";
                    break;
                  default:
                    hostingModeLabel =
                      plan.hosting_mode === "SESSION_EPHEMERAL"
                        ? "Session-only"
                        : plan.hosting_mode === "DEDICATED"
                        ? "Dedicated"
                        : "Shared pool";
                    break;
                }

                void badgeLabel; // intentionally unused
                let pillLabel = plan.name;
                switch (plan.code) {
                  case "FREE_SESSION_MT5":
                    pillLabel = "Free";
                    break;
                  case "STANDARD_DEDICATED_2":
                    pillLabel = "Standard";
                    break;
                  case "MANAGED_SHARED_10":
                    pillLabel = "Managed";
                    break;
                  default:
                    break;
                }

                const primaryFeatures: string[] = [];
                if (plan.code === "FREE_SESSION_MT5") {
                  primaryFeatures.push(
                    "Ephemeral MT5 instance while you are logged in",
                    "No 24/7 automation",
                    "Good for testing and short sessions"
                  );
                } else if (plan.code === "STANDARD_DEDICATED_2") {
                  primaryFeatures.push(
                    "Dedicated VPS always running",
                    "Up to 2 MT5 instances (e.g. demo + live)",
                    "Designed for 24/7 strategy execution"
                  );
                } else if (plan.code === "MANAGED_SHARED_10") {
                  primaryFeatures.push(
                    "Managed shared VPS always running",
                    "Up to 10 MT5 instances per user",
                    "Ideal for portfolio / multiple strategies"
                  );
                } else {
                  primaryFeatures.push(plan.description);
                }

                const specs: string[] = [
                  `CPU: ${plan.cpu_cores} cores`,
                  `RAM: ${Math.round(plan.memory_mb / 1024)} GB`,
                  `Disk: ${plan.disk_gb} GB`,
                  `MT5 instances: ${plan.max_mt5_instances}`,
                  `24/7: ${
                    plan.supports_autonomous_execution ? "Yes" : "No"
                  }`,
                  `Shared: ${plan.is_shared ? "Yes" : "No"}`,
                ];

                return (
                  <div
                    key={plan.id}
                    style={{
                      borderRadius: 14,
                      border: "1px solid #1f2937",
                      padding: "0.9rem 1rem",
                      background:
                        "radial-gradient(circle at top left, rgba(56,189,248,0.15), rgba(15,23,42,0.95))",
                      boxShadow:
                        "0 12px 30px rgba(15,23,42,0.9), 0 0 0 1px rgba(15,23,42,0.6)",
                      display: "flex",
                      flexDirection: "column",
                      gap: "0.55rem",
                    }}
                  >
                    {/* Header row */}
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "flex-start",
                        gap: "0.6rem",
                      }}
                    >
                      <div>
                        <div
                          style={{
                            fontSize: "0.9rem",
                            color: planTone,
                            fontWeight: 700,
                            letterSpacing: "0.08em",
                            textTransform: "uppercase",
                          }}
                        >
                          {plan.code === "FREE_SESSION_MT5"
                            ? "Session"
                            : plan.code === "STANDARD_DEDICATED_2"
                            ? "Dedicated"
                            : plan.code === "MANAGED_SHARED_10"
                            ? "Managed shared"
                            : hostingModeLabel}
                        </div>
                      </div>
                      <span
                        style={{
                          fontSize: "0.78rem",
                          padding: "0.2rem 0.8rem",
                          borderRadius: 999,
                          border: `1px solid ${planTone}`,
                          color: "#e5f4ff",
                          backgroundColor: "rgba(15,23,42,0.9)",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {pillLabel}
                      </span>
                    </div>

                    {isRecommended && (
                      <span
                        style={{
                          fontSize: "0.78rem",
                          color: "#4ade80",
                          fontWeight: 600,
                          marginTop: "0.1rem",
                          whiteSpace: "nowrap",
                        }}
                      >
                        Recommended
                      </span>
                    )}

                    {/* Divider */}
                    <div
                      style={{
                        height: 1,
                        background:
                          "linear-gradient(90deg, rgba(56,189,248,0.4), rgba(15,23,42,0.2))",
                        margin: "0.7rem 0 0.8rem",
                      }}
                    />

                    {/* Content: features + specs */}
                    <div
                      style={{
                        flex: 1,
                        display: "flex",
                        flexDirection: "column",
                        gap: "0.55rem",
                      }}
                    >
                      <ul
                        style={{
                          listStyle: "none",
                          padding: 0,
                          margin: 0,
                          fontSize: "0.9rem",
                          color: "#e5f4ff",
                        }}
                      >
                        {primaryFeatures.map((line, idx) => (
                          <li
                            key={idx}
                            style={{
                              display: "flex",
                              alignItems: "flex-start",
                              gap: 6,
                              marginBottom: "0.2rem",
                            }}
                          >
                            <span
                              style={{
                                color: planTone,
                                marginTop: 1,
                              }}
                            >
                              ✓
                            </span>
                            <span>{line}</span>
                          </li>
                        ))}
                      </ul>

                      <div
                        style={{
                          marginTop: "0.5rem",
                          fontSize: "0.8rem",
                          color: "#9ca3af",
                        }}
                      >
                        {specs.map((spec, idx) => (
                          <div
                            key={idx}
                            style={{
                              display: "flex",
                              alignItems: "flex-start",
                              gap: 6,
                              marginBottom: "0.15rem",
                            }}
                          >
                            <span
                              style={{
                                color: planTone,
                                marginTop: 1,
                              }}
                            >
                              ✓
                            </span>
                            <span>{spec}</span>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Divider & bottom row */}
                    <div
                      style={{
                        height: 1,
                        background:
                          "linear-gradient(90deg, rgba(15,23,42,0.2), rgba(56,189,248,0.35))",
                        margin: "0.7rem 0 0.9rem",
                      }}
                    />

                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                      }}
                    >
                      <div
                        style={{
                          fontSize: "1rem",
                          color: "#e5f4ff",
                          fontWeight: 600,
                        }}
                      >
                        {plan.monthly_price_usd > 0 ? (
                          <>
                            ${plan.monthly_price_usd.toFixed(0)}{" "}
                            <span
                              style={{
                                fontSize: "0.8rem",
                                color: "#9ca3af",
                                fontWeight: 400,
                              }}
                            >
                              / month
                            </span>
                          </>
                        ) : (
                          "Free"
                        )}
                      </div>
                      <Button
                        type="button"
                        disabled={disabled}
                        onClick={() => handleRequestPlan(plan)}
                      >
                        {isCurrent
                          ? "Current plan"
                          : isSelected && requestingPlan
                          ? "Requesting…"
                          : "Request this plan"}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          <h3
            style={{
              fontSize: "0.9rem",
              color: "#cbd5f5",
              margin: "0.6rem 0 0.4rem",
            }}
          >
            Hosted MT5 consoles
          </h3>

          {consolesError && (
            <p style={{ fontSize: "0.85rem", color: "#fca5a5" }}>
              {consolesError}
            </p>
          )}

          {consolesLoading && userConsoles.length === 0 ? (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading consoles…
            </p>
          ) : userConsoles.length === 0 ? (
            <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
              You don’t have any hosted MT5 consoles yet. Choose a plan above
              or wait for your request to be approved.
            </p>
          ) : (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.5rem",
                marginBottom: "1rem",
              }}
            >
              {userConsoles.map((c) => (
                <div
                  key={c.vps_id}
                  style={{
                    borderRadius: 8,
                    border: "1px solid #111827",
                    padding: "0.5rem 0.7rem",
                    background: "rgba(7,12,30,0.9)",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: "0.75rem",
                    fontSize: "0.85rem",
                  }}
                >
                  <div>
                    <div style={{ color: "#e5f4ff", marginBottom: "0.1rem" }}>
                      {c.vps_label}
                    </div>
                    <div style={{ color: "#9ca3af", fontSize: "0.8rem" }}>
                      {c.plan_name}{" "}
                      {c.status && (
                        <span
                          style={{
                            marginLeft: 6,
                            textTransform: "capitalize",
                            color:
                              c.status.toUpperCase() === "RUNNING"
                                ? "#4ade80"
                                : "#e5e7eb",
                          }}
                        >
                          · {c.status.toLowerCase()}
                        </span>
                      )}
                    </div>
                  </div>
                  <Button
                    type="button"
                    onClick={() =>
                      window.open(c.guac_url, "_blank", "noopener,noreferrer")
                    }
                  >
                    Open console
                  </Button>
                </div>
              ))}
            </div>
          )}

          <h3
            style={{
              fontSize: "0.9rem",
              color: "#cbd5f5",
              margin: "0 0 0.4rem",
            }}
          >
            Recent hosting requests
          </h3>

          {hostingLoading && hostingRequests.length === 0 ? (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              Loading requests…
            </p>
          ) : hostingRequests.length === 0 ? (
            <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
              You have not submitted any hosting requests yet.
            </p>
          ) : (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.5rem",
              }}
            >
              {hostingRequests.map((req) => (
                <div
                  key={req.id}
                  style={{
                    borderRadius: 8,
                    border: "1px solid #111827",
                    padding: "0.45rem 0.6rem",
                    background: "rgba(7,12,30,0.9)",
                    fontSize: "0.82rem",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginBottom: "0.2rem",
                    }}
                  >
                    <span style={{ color: "#e5f4ff" }}>
                      {req.note || "Hosting request"}
                    </span>
                    <span
                      style={{
                        padding: "0.15rem 0.5rem",
                        borderRadius: 999,
                        fontSize: "0.75rem",
                        textTransform: "capitalize",
                        border: "1px solid #1f2937",
                        color:
                          req.status.toUpperCase() === "APPROVED"
                            ? "#4ade80"
                            : req.status.toUpperCase() === "REJECTED"
                            ? "#f97373"
                            : "#e5e7eb",
                      }}
                    >
                      {req.status.toLowerCase()}
                    </span>
                  </div>
                  <div style={{ color: "#9ca3af" }}>
                    {new Date(req.created_at).toLocaleString()}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* Password change */}
        <Card
          title="Change Password"
          subtitle="Update your GuvFX account password. You’ll need your current password."
        >
          {pwError && <Alert type="error">{pwError}</Alert>}
          {pwSuccess && <Alert type="info">{pwSuccess}</Alert>}

          <form onSubmit={handleChangePassword}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr",
                gap: "0.75rem",
              }}
            >
              <div>
                <label
                  htmlFor="old-password"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Current password
                </label>
                <input
                  id="old-password"
                  type="password"
                  required
                  value={oldPassword}
                  onChange={(e) => setOldPassword(e.target.value)}
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
                  htmlFor="new-password1"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  New password
                </label>
                <input
                  id="new-password1"
                  type="password"
                  required
                  value={newPassword1}
                  onChange={(e) => setNewPassword1(e.target.value)}
                  placeholder="At least 8 characters"
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
                  htmlFor="new-password2"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Confirm new password
                </label>
                <input
                  id="new-password2"
                  type="password"
                  required
                  value={newPassword2}
                  onChange={(e) => setNewPassword2(e.target.value)}
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
            </div>

            <div
              style={{
                marginTop: "0.9rem",
                display: "flex",
                justifyContent: "flex-end",
              }}
            >
              <Button type="submit" disabled={pwLoading || !accessToken}>
                {pwLoading ? "Updating password…" : "Update password"}
              </Button>
            </div>
          </form>
        </Card>
      </div>
  );
}
