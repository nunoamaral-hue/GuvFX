"use client";

import { useCallback, useEffect, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { HostingRequest, VPSPlan } from "@/types/hosting";

type HostingProvider = {
  id: number;
  name: string;
  api_type: string;
  api_base_url: string;
  is_active: boolean;
};

type VpsInstance = {
  id: number;
  provider: number;
  provider_name: string;
  plan: number;
  plan_name: string;
  external_id: string;
  hostname: string;
  public_ip: string | null;
  status: string;
  is_dedicated: boolean;
  current_mt5_count: number;
  provisioned_at: string | null;
  last_health_check_at: string | null;
};

export default function HostingAdminPage() {
  const [providers, setProviders] = useState<HostingProvider[]>([]);
  const [plans, setPlans] = useState<VPSPlan[]>([]);
  const [instances, setInstances] = useState<VpsInstance[]>([]);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [requests, setRequests] = useState<HostingRequest[]>([]);
  const [requestsLoading, setRequestsLoading] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [approvingId, setApprovingId] = useState<number | null>(null);
  const [rejectingId, setRejectingId] = useState<number | null>(null);
  const pendingRequests = requests.filter((req) => req.status === "PENDING");

  const toFriendlyAuthError = (err: unknown): string | null => {
    const msg = err instanceof Error ? err.message : String(err || "");
    if (msg.includes("401") || msg.toLowerCase().includes("unauthorized")) {
      return "You are not logged in (cookie auth). Please log in at /login and reload this page.";
    }
    if (msg.includes("403") || msg.toLowerCase().includes("forbidden")) {
      return "You do not have permission to view hosting admin. This page is staff-only.";
    }
    return null;
  };

  // Fetch hosting data
  const fetchRequests = useCallback(async () => {
    setRequestsLoading(true);
    setRequestError(null);
    try {
      const data = await apiFetch<HostingRequest[]>(
        "/api/hosting/requests/",
        {}
      );
      const list = Array.isArray(data)
        ? data
        : (data as { results?: HostingRequest[] }).results ?? [];
      setRequests(list);
    } catch (err: unknown) {
      console.error(err);
      const friendly = toFriendlyAuthError(err);
      setRequestError(
        friendly ||
          (err instanceof Error ? err.message : "Failed to load hosting requests.")
      );
    } finally {
      setRequestsLoading(false);
    }
  }, []);

  useEffect(() => {
    const fetchData = async () => {
      setLoading(true);
      setError(null);
      try {
        const [providersRes, plansRes, instancesRes] = await Promise.all([
          apiFetch<HostingProvider[]>("/api/hosting/providers/", {}),
          apiFetch<VPSPlan[]>("/api/hosting/plans/", {}),
          apiFetch<VpsInstance[]>("/api/hosting/vps/", {}),
        ]);
        setProviders(providersRes);
        setPlans(plansRes);
        setInstances(instancesRes);
      } catch (err: unknown) {
        console.error(err);
        const friendly = toFriendlyAuthError(err);
        const message =
          friendly || (err instanceof Error ? err.message : "Failed to load hosting data.");
        setError(message);
      } finally {
        setLoading(false);
      }
      fetchRequests();
    };
    fetchData();
  }, [fetchRequests]);

  const memoryLabel = (mb: number) =>
    mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`;

  return (
    <AppShell>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          Hosting overview
        </h1>
        <p
          style={{
            fontSize: "0.9rem",
            color: "#b7c5dd",
            marginBottom: "1rem",
          }}
        >
          Internal view of hosting providers, plans, and VPS instances used by GuvFX.
        </p>

        {error && <Alert type="error">{error}</Alert>}

        {loading && (
          <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
            Loading hosting data…
          </p>
        )}

        {!loading && !error && (
          <>
            {/* Providers */}
            <Card
              title="Hosting providers"
              subtitle="Configured hosting backends (currently OVH only)."
            >
              {providers.length === 0 ? (
                <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
                  No providers configured yet.
                </p>
              ) : (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "0.4rem",
                  }}
                >
                  {providers.map((p) => (
                    <div
                      key={p.id}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        padding: "0.4rem 0.5rem",
                        borderRadius: 8,
                        border: "1px solid #111827",
                        background: "rgba(7,12,30,0.9)",
                        fontSize: "0.85rem",
                      }}
                    >
                      <div>
                        <div style={{ color: "#e5f4ff" }}>{p.name}</div>
                        <div style={{ color: "#9ca3af", fontSize: "0.8rem" }}>
                          Type: {p.api_type}
                        </div>
                        {p.api_base_url && (
                          <div
                            style={{
                              color: "#6b7280",
                              fontSize: "0.75rem",
                              marginTop: "0.1rem",
                            }}
                          >
                            API base: {p.api_base_url}
                          </div>
                        )}
                      </div>
                      <Badge color={p.is_active ? "green" : "gray"}>
                        {p.is_active ? "Active" : "Disabled"}
                      </Badge>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            {/* Plans */}
            <Card
              title="VPS plans"
              subtitle="Logical plans users can subscribe to (Scalper, Pro, etc.)."
            >
              {plans.length === 0 ? (
                <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
                  No VPS plans defined yet.
                </p>
              ) : (
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns:
                      "repeat(auto-fit, minmax(260px, 1fr))",
                    gap: "0.75rem 1.0rem",
                  }}
                >
                  {plans.map((plan) => (
                    <div
                      key={plan.id}
                      style={{
                        borderRadius: 8,
                        border: "1px solid #111827",
                        padding: "0.6rem 0.8rem",
                        background: "rgba(7,12,30,0.9)",
                        fontSize: "0.85rem",
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
                          <div
                            style={{
                              color: "#e5f4ff",
                              fontWeight: 500,
                            }}
                          >
                            {plan.name}
                          </div>
                          <div
                            style={{ color: "#9ca3af", fontSize: "0.8rem" }}
                          >
                            {plan.provider_name}
                          </div>
                        </div>
                        <Badge color={plan.is_shared ? "blue" : "green"}>
                            {plan.is_shared ? "Shared" : "Dedicated"}
                        </Badge>
                      </div>

                      {plan.description && (
                        <p
                          style={{
                            fontSize: "0.8rem",
                            color: "#9ca3af",
                            marginBottom: "0.3rem",
                          }}
                        >
                          {plan.description}
                        </p>
                      )}

                      <div
                        style={{
                          display: "flex",
                          flexWrap: "wrap",
                          gap: "0.35rem 0.8rem",
                          fontSize: "0.8rem",
                          color: "#cbd5f5",
                        }}
                      >
                        <span>{plan.cpu_cores} vCPU</span>
                        <span>{memoryLabel(plan.memory_mb)} RAM</span>
                        <span>{plan.disk_gb} GB disk</span>
                        <span>
                          ${Number(plan.monthly_price_usd).toFixed(2)}/month
                        </span>
                        <span>
                          up to {plan.max_mt5_instances} MT5
                          {plan.max_mt5_instances === 1 ? "" : "s"}
                        </span>
                      </div>

                      {!plan.is_user_visible && (
                        <div
                          style={{
                            fontSize: "0.75rem",
                            color: "#facc15",
                            marginTop: "0.25rem",
                          }}
                        >
                          Hidden from user UI
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Card>

            {/* VPS instances */}
            <Card
              title="VPS instances"
              subtitle="Actual VPS nodes currently registered within GuvFX hosting."
            >
              {instances.length === 0 ? (
                <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
                  No VPS instances registered yet.
                </p>
              ) : (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "0.5rem",
                  }}
                >
                  {instances.map((vps) => {
                    const statusColor =
                      vps.status === "ACTIVE"
                        ? "#4ade80"
                        : vps.status === "ERROR"
                        ? "#f97373"
                        : "#e5e7eb";

                    return (
                      <div
                        key={vps.id}
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          padding: "0.45rem 0.6rem",
                          borderRadius: 8,
                          border: "1px solid #111827",
                          background: "rgba(7,12,30,0.9)",
                          fontSize: "0.8rem",
                        }}
                      >
                        <div
                          style={{
                            display: "flex",
                            justifyContent: "space-between",
                            alignItems: "center",
                            gap: "0.75rem",
                          }}
                        >
                          <div>
                            <div style={{ color: "#e5f4ff" }}>
                              {vps.hostname || vps.public_ip || `VPS #${vps.id}`}
                            </div>
                            <div
                              style={{
                                color: "#9ca3af",
                                fontSize: "0.78rem",
                              }}
                            >
                              {vps.provider_name} · {vps.plan_name}
                            </div>
                            {vps.public_ip && (
                              <div
                                style={{
                                  color: "#6b7280",
                                  fontSize: "0.75rem",
                                  marginTop: "0.1rem",
                                }}
                              >
                                IP: {vps.public_ip}
                              </div>
                            )}
                          </div>
                          <div style={{ textAlign: "right" }}>
                            <div
                              style={{
                                color: statusColor,
                                fontWeight: 600,
                                marginBottom: "0.15rem",
                              }}
                            >
                              {vps.status}
                            </div>
                            <div
                              style={{
                                color: "#9ca3af",
                                fontSize: "0.75rem",
                              }}
                            >
                              {vps.is_dedicated ? "Dedicated" : "Shared"} ·{" "}
                              {vps.current_mt5_count} MT5
                            </div>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>

            <Card
              title="Pending hosting requests"
              subtitle="Approve or reject user requests for hosted MT5."
            >
              {requestError && <Alert type="error">{requestError}</Alert>}
              {requestsLoading && (
                <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
                  Loading requests…
                </p>
              )}
              {!requestsLoading && pendingRequests.length === 0 && !requestError && (
                <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
                  No pending hosting requests at the moment.
                </p>
              )}
              {!requestsLoading && pendingRequests.length > 0 && (
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: "0.5rem",
                  }}
                >
                  {pendingRequests.map((req) => (
                    <div
                      key={req.id}
                      style={{
                        display: "grid",
                        gridTemplateColumns:
                          "minmax(0,2fr) minmax(0,1fr) minmax(0,1fr)",
                        gap: "0.5rem 1rem",
                        borderRadius: 8,
                        border: "1px solid #111827",
                        padding: "0.6rem 0.8rem",
                        background: "rgba(7,12,30,0.9)",
                        fontSize: "0.9rem",
                      }}
                    >
                      <div>
                        <div style={{ color: "#e5f4ff" }}>{req.owner_email}</div>
                        <div
                          style={{
                            fontSize: "0.8rem",
                            color: "#9ca3af",
                            marginTop: 2,
                          }}
                        >
                          Requested:{" "}
                          {new Date(req.created_at).toLocaleString()}
                        </div>
                        {req.note && (
                          <div
                            style={{
                              fontSize: "0.8rem",
                              color: "#9ca3af",
                              marginTop: 2,
                            }}
                          >
                            Note: {req.note}
                          </div>
                        )}
                      </div>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "flex-end",
                          gap: "0.5rem",
                        }}
                      >
                        <Button
                          variant="secondary"
                          disabled={rejectingId === req.id}
                          onClick={async () => {
                            setRejectingId(req.id);
                            setRequestError(null);
                            try {
                              await apiFetch(
                                `/api/hosting/requests/${req.id}/reject/`,
                                {
                                  method: "POST",
                                  body: JSON.stringify({}),
                                }
                              );
                              await fetchRequests();
                            } catch (err: unknown) {
                              console.error(err);
                              setRequestError(
                                err instanceof Error
                                  ? err.message
                                  : "Failed to reject request."
                              );
                            } finally {
                              setRejectingId(null);
                            }
                          }}
                        >
                          {rejectingId === req.id ? "Rejecting…" : "Reject"}
                        </Button>
                      </div>
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "flex-end",
                          gap: "0.5rem",
                        }}
                      >
                        <Button
                          disabled={approvingId === req.id}
                          onClick={async () => {
                            setApprovingId(req.id);
                            setRequestError(null);
                            try {
                              await apiFetch(
                                `/api/hosting/requests/${req.id}/approve/`,
                                {
                                  method: "POST",
                                  body: JSON.stringify({}),
                                }
                              );
                              await fetchRequests();
                            } catch (err: unknown) {
                              console.error(err);
                              setRequestError(
                                err instanceof Error
                                  ? err.message
                                  : "Failed to approve request."
                              );
                            } finally {
                              setApprovingId(null);
                            }
                          }}
                        >
                          {approvingId === req.id ? "Approving…" : "Approve"}
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </>
        )}
      </div>
    </AppShell>
  );
}
