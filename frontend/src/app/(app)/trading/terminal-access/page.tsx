"use client";

import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import type {
  TerminalBinding,
  InteractionSessionResponse,
  ResumableContextResponse,
  SafeLaunchDescriptor,
} from "@/types/mt5-interaction";

// ─────────────────────────────────────────────────────────────────────
// Display helpers
// ─────────────────────────────────────────────────────────────────────

const humanize = (s: string) =>
  s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const fmtDateTime = (iso: string) =>
  new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

// ─────────────────────────────────────────────────────────────────────
// Binding status → Badge color
// ─────────────────────────────────────────────────────────────────────

const bindingStatusColor: Record<string, "green" | "gray" | "blue" | "red" | "yellow"> = {
  available: "green",
  launching: "blue",
  active: "blue",
  suspended: "yellow",
  maintenance: "yellow",
  locked: "red",
};

// ─────────────────────────────────────────────────────────────────────
// InteractionSession state → Badge color
// ─────────────────────────────────────────────────────────────────────

const sessionStateColor: Record<string, "green" | "gray" | "blue" | "red" | "yellow"> = {
  requested: "blue",
  authorized: "blue",
  active: "green",
  ended: "gray",
};

// ─────────────────────────────────────────────────────────────────────
// MT5Session state → Badge color
// ─────────────────────────────────────────────────────────────────────

const mt5StateColor: Record<string, "green" | "gray" | "blue" | "red" | "yellow"> = {
  launching: "blue",
  connected: "green",
  suspended: "yellow",
  ended: "gray",
  failed: "red",
};

// ─────────────────────────────────────────────────────────────────────
// Shared styles (matches existing GuvFX glass card pattern)
// ─────────────────────────────────────────────────────────────────────

const glassCard: React.CSSProperties = {
  borderRadius: 16,
  border: "1px solid rgba(74, 179, 255, 0.12)",
  background:
    "linear-gradient(135deg, rgba(10, 15, 40, 0.95) 0%, rgba(5, 8, 22, 0.98) 100%)",
  boxShadow:
    "0 8px 32px rgba(0, 0, 0, 0.4), 0 0 60px rgba(30, 111, 255, 0.04)",
  padding: "1.5rem",
  display: "flex",
  flexDirection: "column" as const,
};

const labelStyle: React.CSSProperties = {
  fontSize: "0.8rem",
  color: "#94a3b8",
  marginBottom: 2,
};

const valueStyle: React.CSSProperties = {
  fontSize: "0.9rem",
  color: "#e9f4ff",
};

const sectionHeader: React.CSSProperties = {
  fontSize: "0.8rem",
  color: "#94a3b8",
  textTransform: "uppercase" as const,
  letterSpacing: "0.06em",
  fontWeight: 600,
  marginBottom: "0.75rem",
};

// ─────────────────────────────────────────────────────────────────────
// State notice banners
// ─────────────────────────────────────────────────────────────────────

function StateNotice({ type, message }: { type: "info" | "warning" | "error"; message: string }) {
  const colors = {
    info: { border: "rgba(96, 165, 250, 0.2)", bg: "rgba(12, 16, 38, 0.95)", text: "#60a5fa" },
    warning: { border: "rgba(251, 191, 36, 0.2)", bg: "rgba(18, 15, 10, 0.95)", text: "#fbbf24" },
    error: { border: "rgba(248, 113, 113, 0.2)", bg: "rgba(20, 10, 10, 0.95)", text: "#f87171" },
  };
  const c = colors[type];
  return (
    <div
      style={{
        ...glassCard,
        borderColor: c.border,
        background: `linear-gradient(135deg, ${c.bg} 0%, rgba(5, 8, 22, 0.98) 100%)`,
        padding: "1rem 1.25rem",
        marginBottom: "1rem",
      }}
    >
      <p style={{ fontSize: "0.85rem", color: c.text, margin: 0, lineHeight: 1.5 }}>
        {message}
      </p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Detail row helper
// ─────────────────────────────────────────────────────────────────────

function DetailRow({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null;
  return (
    <div style={{ minWidth: 180 }}>
      <div style={labelStyle}>{label}</div>
      <div style={valueStyle}>{value}</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Session status card
// ─────────────────────────────────────────────────────────────────────

function SessionStatusCard({
  session,
  launchDescriptor,
  onTerminate,
  onResume,
  terminating,
  resuming,
}: {
  session: InteractionSessionResponse;
  launchDescriptor: SafeLaunchDescriptor | null;
  onTerminate: () => void;
  onResume: () => void;
  terminating: boolean;
  resuming: boolean;
}) {
  const isActive = session.state === "active" || session.state === "authorized";
  const isEnded = session.state === "ended";
  const mt5 = session.latest_mt5_session;

  return (
    <div style={{ ...glassCard, marginBottom: "1rem" }}>
      <div style={sectionHeader}>Active Session</div>

      {/* Session header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          marginBottom: "1rem",
          flexWrap: "wrap" as const,
        }}
      >
        <span style={{ fontSize: "1.05rem", fontWeight: 600, color: "#e9f4ff" }}>
          {session.terminal_label || session.terminal_identifier}
        </span>
        <Badge color={sessionStateColor[session.state] ?? "gray"}>
          {humanize(session.state)}
        </Badge>
        <Badge color={bindingStatusColor[session.environment_type] ?? "gray"}>
          {humanize(session.environment_type)}
        </Badge>
      </div>

      {/* Session detail fields (only non-null) */}
      <div style={{ display: "flex", flexWrap: "wrap" as const, gap: "1rem 2rem", marginBottom: "1rem" }}>
        <DetailRow label="Session ID" value={`#${session.id}`} />
        <DetailRow label="Binding" value={session.terminal_identifier} />
        <DetailRow label="Requested" value={session.requested_at ? fmtDateTime(session.requested_at) : null} />
        <DetailRow label="Authorized" value={session.authorized_at ? fmtDateTime(session.authorized_at) : null} />
        <DetailRow label="Started" value={session.started_at ? fmtDateTime(session.started_at) : null} />
        <DetailRow label="Expires" value={session.expires_at ? fmtDateTime(session.expires_at) : null} />
        <DetailRow label="Last activity" value={session.last_activity_at ? fmtDateTime(session.last_activity_at) : null} />
        {isEnded && <DetailRow label="Ended" value={session.ended_at ? fmtDateTime(session.ended_at) : null} />}
        {isEnded && session.terminated_reason && (
          <DetailRow label="Termination reason" value={session.terminated_reason} />
        )}
      </div>

      {/* MT5Session sub-card */}
      {mt5 && (
        <div
          style={{
            borderRadius: 12,
            border: "1px solid rgba(74, 179, 255, 0.08)",
            background: "rgba(255, 255, 255, 0.02)",
            padding: "1rem",
            marginBottom: "1rem",
          }}
        >
          <div style={{ ...sectionHeader, marginBottom: "0.5rem" }}>MT5 Session</div>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginBottom: "0.75rem", flexWrap: "wrap" as const }}>
            <Badge color={mt5StateColor[mt5.state] ?? "gray"}>
              {humanize(mt5.state)}
            </Badge>
            <span style={{ fontSize: "0.8rem", color: "#8fa0b7" }}>
              {mt5.adapter_type}
            </span>
          </div>
          <div style={{ display: "flex", flexWrap: "wrap" as const, gap: "0.75rem 2rem" }}>
            <DetailRow label="Launch issued" value={mt5.launch_issued_at ? fmtDateTime(mt5.launch_issued_at) : null} />
            <DetailRow label="Connected" value={mt5.connected_at ? fmtDateTime(mt5.connected_at) : null} />
            <DetailRow label="Last heartbeat" value={mt5.last_heartbeat_at ? fmtDateTime(mt5.last_heartbeat_at) : null} />
            {mt5.state === "failed" && mt5.failure_reason && (
              <DetailRow label="Failure reason" value={mt5.failure_reason} />
            )}
          </div>
        </div>
      )}

      {/* Embedded terminal panel — GuvFX controls ABOVE, MT5 panel BELOW */}
      {isActive && mt5 && (mt5.state === "connected" || mt5.state === "launching") && (
        launchDescriptor && launchDescriptor.embed_url ? (
          <div
            style={{
              borderRadius: 12,
              border: "1px solid rgba(74, 179, 255, 0.15)",
              background: "rgba(0, 0, 0, 0.3)",
              overflow: "hidden",
              marginBottom: "1rem",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "0.5rem 1rem",
                background: "rgba(10, 15, 40, 0.9)",
                borderBottom: "1px solid rgba(74, 179, 255, 0.1)",
              }}
            >
              <span style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
                MT5 Terminal — {launchDescriptor.transport_type}
              </span>
              {launchDescriptor.expiry && (
                <span style={{ fontSize: "0.75rem", color: "#64748b" }}>
                  Expires: {fmtDateTime(launchDescriptor.expiry)}
                </span>
              )}
            </div>
            <iframe
              src={launchDescriptor.embed_url}
              title="MT5 Terminal"
              style={{
                width: "100%",
                height: "600px",
                border: "none",
                display: "block",
              }}
              sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
            />
          </div>
        ) : (
          <div
            style={{
              borderRadius: 12,
              border: "1px dashed rgba(74, 179, 255, 0.2)",
              background: "rgba(255, 255, 255, 0.01)",
              padding: "2rem",
              textAlign: "center" as const,
              marginBottom: "1rem",
            }}
          >
            <div style={{ fontSize: "0.9rem", color: "#8fa0b7", marginBottom: "0.25rem" }}>
              {mt5.state === "launching" ? "Connecting to terminal..." : "Terminal Panel"}
            </div>
            <div style={{ fontSize: "0.8rem", color: "#64748b" }}>
              {mt5.state === "launching"
                ? "The terminal session is being established. This may take a moment."
                : "Embed URL not available. The adapter may not have returned connection details."}
            </div>
          </div>
        )
      )}

      {/* Action buttons */}
      <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" as const }}>
        {isActive && (
          <Button
            variant="secondary"
            onClick={onTerminate}
            disabled={terminating}
            style={{ borderColor: "rgba(248, 113, 113, 0.4)", color: "#fca5a5" }}
          >
            {terminating ? "Terminating..." : "Terminate Session"}
          </Button>
        )}
        {isEnded && (
          <Button variant="secondary" onClick={onResume} disabled={resuming}>
            {resuming ? "Checking..." : "Check Resumability"}
          </Button>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Main page component
// ─────────────────────────────────────────────────────────────────────

export default function TerminalAccessPage() {
  // ── Bindings list state ──
  const [bindings, setBindings] = useState<TerminalBinding[]>([]);
  const [bindingsLoading, setBindingsLoading] = useState(true);
  const [bindingsError, setBindingsError] = useState<string | null>(null);

  // ── Active session state ──
  const [activeSession, setActiveSession] = useState<InteractionSessionResponse | null>(null);
  const [launchDescriptor, setLaunchDescriptor] = useState<SafeLaunchDescriptor | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState<string | null>(null);

  // ── Action states ──
  const [launching, setLaunching] = useState(false);
  const [launchBindingId, setLaunchBindingId] = useState<number | null>(null);
  const [terminating, setTerminating] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [resumeResult, setResumeResult] = useState<ResumableContextResponse | null>(null);

  // ── Notice state ──
  const [notice, setNotice] = useState<{ type: "info" | "warning" | "error"; message: string } | null>(null);

  // ── Polling interval for session status ──
  const [pollInterval, setPollInterval] = useState<ReturnType<typeof setInterval> | null>(null);

  // ── Fetch terminal bindings ──
  const fetchBindings = useCallback(async () => {
    setBindingsLoading(true);
    setBindingsError(null);
    try {
      const data = await apiFetch<TerminalBinding[]>(
        "/api/mt5-interaction/terminal-bindings/",
        {}
      );
      setBindings(data);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to load terminal bindings.";
      setBindingsError(message);
    } finally {
      setBindingsLoading(false);
    }
  }, []);

  // ── Fetch session status by ID ──
  const fetchSessionStatus = useCallback(async (sessionId: number) => {
    try {
      const data = await apiFetch<InteractionSessionResponse>(
        `/api/mt5-interaction/sessions/${sessionId}/`,
        {}
      );
      setActiveSession(data);

      // Auto-stop polling when session ends
      if (data.state === "ended") {
        setNotice({ type: "info", message: "Session has ended." });
      }

      return data;
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to fetch session status.";
      setSessionError(message);
      return null;
    }
  }, []);

  // ── Launch session ──
  const handleLaunch = useCallback(async (bindingId: number) => {
    setLaunching(true);
    setLaunchBindingId(bindingId);
    setSessionError(null);
    setNotice(null);
    setResumeResult(null);
    setLaunchDescriptor(null);
    try {
      const data = await apiFetch<InteractionSessionResponse & { launch_descriptor?: SafeLaunchDescriptor }>(
        "/api/mt5-interaction/sessions/",
        {
          method: "POST",
          body: JSON.stringify({ terminal_binding_id: bindingId }),
        }
      );
      setActiveSession(data);
      if (data.launch_descriptor) {
        setLaunchDescriptor(data.launch_descriptor);
      }
      setNotice({ type: "info", message: "Session launched. Waiting for terminal connection..." });
      // Refresh bindings to reflect occupancy change
      fetchBindings();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to launch session.";
      setSessionError(message);

      // Map known error patterns to user-friendly notices
      if (message.includes("409") || message.toLowerCase().includes("occupancy") || message.toLowerCase().includes("conflict")) {
        setNotice({ type: "warning", message: "This terminal is currently occupied by another session." });
      } else if (message.includes("403") || message.toLowerCase().includes("denied") || message.toLowerCase().includes("authorization")) {
        setNotice({ type: "error", message: "You are not authorized to access this terminal." });
      } else if (message.includes("404")) {
        setNotice({ type: "error", message: "Terminal binding not found or no longer available." });
      } else {
        setNotice({ type: "error", message });
      }
    } finally {
      setLaunching(false);
      setLaunchBindingId(null);
    }
  }, [fetchBindings]);

  // ── Terminate session ──
  const handleTerminate = useCallback(async () => {
    if (!activeSession) return;
    setTerminating(true);
    setNotice(null);
    setLaunchDescriptor(null);
    try {
      const data = await apiFetch<InteractionSessionResponse>(
        `/api/mt5-interaction/sessions/${activeSession.id}/terminate/`,
        {
          method: "POST",
          body: JSON.stringify({ reason: "User-initiated termination" }),
        }
      );
      setActiveSession(data);
      setNotice({ type: "info", message: "Session terminated successfully." });
      // Refresh bindings to reflect released occupancy
      fetchBindings();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to terminate session.";
      setNotice({ type: "error", message });
    } finally {
      setTerminating(false);
    }
  }, [activeSession, fetchBindings]);

  // ── Resume check ──
  const handleResume = useCallback(async () => {
    if (!activeSession) return;
    setResuming(true);
    setNotice(null);
    setResumeResult(null);
    try {
      const data = await apiFetch<ResumableContextResponse>(
        `/api/mt5-interaction/sessions/${activeSession.id}/resume/`,
        { method: "POST" }
      );
      setResumeResult(data);
      if (data.can_resume) {
        setActiveSession(data.interaction_session);
        if (data.launch_descriptor) {
          setLaunchDescriptor(data.launch_descriptor);
        }
        setNotice({ type: "info", message: "Session is resumable. You may continue working." });
      } else {
        setNotice({ type: "warning", message: "Session cannot be resumed at this time." });
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to check resumability.";
      setNotice({ type: "error", message });
    } finally {
      setResuming(false);
    }
  }, [activeSession]);

  // ── Poll active session status ──
  useEffect(() => {
    if (!activeSession) return;
    if (activeSession.state === "ended") return;

    const interval = setInterval(() => {
      fetchSessionStatus(activeSession.id);
    }, 10000); // Poll every 10 seconds

    setPollInterval(interval);
    return () => {
      clearInterval(interval);
      setPollInterval(null);
    };
  }, [activeSession?.id, activeSession?.state, fetchSessionStatus]);

  // ── Initial fetch ──
  useEffect(() => {
    fetchBindings();
  }, [fetchBindings]);

  // Cleanup polling on unmount
  useEffect(() => {
    return () => {
      if (pollInterval) clearInterval(pollInterval);
    };
  }, [pollInterval]);

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Terminal Access</h1>
      <p
        style={{
          fontSize: "0.9rem",
          color: "#b7c5dd",
          marginBottom: "1.5rem",
        }}
      >
        Launch, monitor, and manage MT5 terminal sessions.
      </p>

      {/* ── State notices ── */}
      {notice && <StateNotice type={notice.type} message={notice.message} />}

      {/* ── Active session card (shown when a session exists) ── */}
      {sessionLoading && (
        <div style={{ ...glassCard, marginBottom: "1rem" }}>
          <div style={{ fontSize: "0.9rem", color: "#8fa0b7" }}>Loading session...</div>
        </div>
      )}

      {!sessionLoading && sessionError && !activeSession && (
        <StateNotice type="error" message={sessionError} />
      )}

      {activeSession && (
        <SessionStatusCard
          session={activeSession}
          launchDescriptor={launchDescriptor}
          onTerminate={handleTerminate}
          onResume={handleResume}
          terminating={terminating}
          resuming={resuming}
        />
      )}

      {/* ── Resume result card ── */}
      {resumeResult && (
        <div style={{ ...glassCard, marginBottom: "1rem" }}>
          <div style={sectionHeader}>Resume Check Result</div>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" as const }}>
            <Badge color={resumeResult.can_resume ? "green" : "red"}>
              {resumeResult.can_resume ? "Resumable" : "Not Resumable"}
            </Badge>
            {resumeResult.access_mode && (
              <span style={{ fontSize: "0.85rem", color: "#8fa0b7" }}>
                Access mode: {humanize(resumeResult.access_mode)}
              </span>
            )}
          </div>
        </div>
      )}

      {/* ── Terminal bindings list ── */}
      <div style={{ ...glassCard, marginBottom: "1.5rem" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: "1rem",
          }}
        >
          <div style={sectionHeader}>Available Terminals</div>
          <Button
            variant="secondary"
            onClick={fetchBindings}
            disabled={bindingsLoading}
            style={{ fontSize: "0.8rem", padding: "0.35rem 0.8rem" }}
          >
            {bindingsLoading ? "Loading..." : "Refresh"}
          </Button>
        </div>

        {/* Loading */}
        {bindingsLoading && bindings.length === 0 && (
          <div style={{ fontSize: "0.9rem", color: "#8fa0b7", textAlign: "center" as const, padding: "2rem 0" }}>
            Loading terminal bindings...
          </div>
        )}

        {/* Error */}
        {!bindingsLoading && bindingsError && (
          <div style={{ fontSize: "0.9rem", color: "#f87171", textAlign: "center" as const, padding: "2rem 0" }}>
            {bindingsError}
          </div>
        )}

        {/* Empty */}
        {!bindingsLoading && !bindingsError && bindings.length === 0 && (
          <div style={{ textAlign: "center" as const, padding: "2rem 0" }}>
            <div style={{ fontSize: "0.9rem", color: "#8fa0b7", marginBottom: "0.25rem" }}>
              No terminal bindings available.
            </div>
            <div style={{ fontSize: "0.8rem", color: "#64748b" }}>
              You may not have active terminal authorizations, or no terminals are currently configured.
            </div>
          </div>
        )}

        {/* Binding rows */}
        {bindings.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column" as const, gap: "0.5rem" }}>
            {bindings.map((binding) => {
              const isAvailable = binding.status === "available";
              const isLaunching = launching && launchBindingId === binding.id;
              const hasActiveSession =
                activeSession &&
                activeSession.terminal_binding_id === binding.id &&
                activeSession.state !== "ended";

              return (
                <div
                  key={binding.id}
                  style={{
                    borderRadius: 12,
                    border: "1px solid rgba(74, 179, 255, 0.08)",
                    background: "rgba(255, 255, 255, 0.02)",
                    padding: "1rem 1.25rem",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "1rem",
                    flexWrap: "wrap" as const,
                  }}
                >
                  {/* Left: binding info */}
                  <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flex: 1, minWidth: 200 }}>
                    <div>
                      <div style={{ fontSize: "0.95rem", fontWeight: 600, color: "#e9f4ff" }}>
                        {binding.terminal_label || binding.terminal_identifier}
                      </div>
                      <div style={{ fontSize: "0.8rem", color: "#8fa0b7", marginTop: 2 }}>
                        {binding.terminal_identifier}
                        {binding.terminal_node_hostname && (
                          <span style={{ color: "#64748b" }}> · {binding.terminal_node_hostname}</span>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Center: badges */}
                  <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" as const }}>
                    <Badge color={bindingStatusColor[binding.status] ?? "gray"}>
                      {humanize(binding.status)}
                    </Badge>
                    <Badge color={binding.environment_type === "live" ? "green" : "blue"}>
                      {humanize(binding.environment_type)}
                    </Badge>
                    {binding.supports_shared_view && (
                      <Badge color="gray">Shared View</Badge>
                    )}
                  </div>

                  {/* Right: launch button */}
                  <div>
                    {hasActiveSession ? (
                      <Button
                        variant="secondary"
                        disabled
                        style={{ fontSize: "0.8rem", padding: "0.35rem 0.8rem" }}
                      >
                        In Use
                      </Button>
                    ) : (
                      <Button
                        variant={isAvailable ? "primary" : "secondary"}
                        disabled={!isAvailable || launching}
                        onClick={() => handleLaunch(binding.id)}
                        style={{ fontSize: "0.8rem", padding: "0.35rem 0.8rem" }}
                      >
                        {isLaunching ? "Launching..." : isAvailable ? "Launch" : "Unavailable"}
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
