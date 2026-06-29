"use client";

import { useEffect, useState, useCallback, useRef } from "react";
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
// MT5 credential status type (from GET /api/mt5/status/)
// ─────────────────────────────────────────────────────────────────────

type Mt5CredentialStatus = {
  login: string;
  server: string;
  last_status: string | null;
  last_verified_at: string | null;
  last_error: string | null;
  updated_at: string;
} | null;

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

// ─────────────────────────────────────────────────────────────────────
// PX-7A — Trading vs Viewer state separation (INCIDENT-001)
//
// CORE RULE: Viewer Availability ≠ Trading Availability.
// Trading is NEVER computed in the frontend — it is consumed verbatim from
// /api/reliability/trading-health/ and only mapped to a display bucket.
// ─────────────────────────────────────────────────────────────────────

type TradingHealth = {
  ok?: boolean;
  state: string; // HEALTHY | DEGRADED | IMPAIRED | DOWN | UNKNOWN
  can_trade: boolean;
  reasons: string[];
} | null;

type TradingBucket = "Healthy" | "Warning" | "Critical" | "Unknown";

// Map the reliability TradingState verbatim → display bucket (no calculation).
const tradingBucket = (h: TradingHealth): TradingBucket => {
  switch (h?.state) {
    case "HEALTHY":
      return "Healthy";
    case "DEGRADED":
    case "IMPAIRED":
      return "Warning";
    case "DOWN":
      return "Critical";
    default:
      return "Unknown";
  }
};

const tradingColor: Record<TradingBucket, "green" | "yellow" | "red" | "gray"> = {
  Healthy: "green",
  Warning: "yellow",
  Critical: "red",
  Unknown: "gray",
};

// Viewer = the Guacamole VNC tunnel lifecycle. Frontend-owned, fully
// independent of trading. Never surfaced as a generic "Unavailable".
type ViewerState =
  | "Connected"
  | "Connecting"
  | "Reconnecting"
  | "Disconnected"
  | "Error";

const viewerColor: Record<ViewerState, "green" | "blue" | "yellow" | "gray" | "red"> = {
  Connected: "green",
  Connecting: "blue",
  Reconnecting: "yellow",
  Disconnected: "gray",
  Error: "red",
};

// Binding availability is a separate axis from viewer/trading state.
// Per PX-7A: never render the generic word "Unavailable" on a binding.
const bindingActionLabel = (status: string): string => {
  switch (status) {
    case "available":
      return "Launch";
    case "launching":
    case "active":
      return "In Use";
    case "suspended":
      return "Suspended";
    case "maintenance":
      return "Maintenance";
    case "locked":
      return "Locked";
    default:
      return "Busy";
  }
};

// ─────────────────────────────────────────────────────────────────────
// Status header — two clearly separated badges: Trading and Viewer
// ─────────────────────────────────────────────────────────────────────

function StatusHeader({ trading, viewer }: { trading: TradingBucket; viewer: ViewerState }) {
  const pill: React.CSSProperties = {
    display: "flex",
    alignItems: "center",
    gap: "0.6rem",
    padding: "0.6rem 1rem",
    borderRadius: 12,
    border: "1px solid rgba(74, 179, 255, 0.12)",
    background: "rgba(255, 255, 255, 0.02)",
  };
  return (
    <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" as const, marginBottom: "1rem" }}>
      <div style={pill}>
        <span style={{ ...labelStyle, marginBottom: 0 }}>Trading</span>
        <Badge color={tradingColor[trading]}>{trading}</Badge>
      </div>
      <div style={pill}>
        <span style={{ ...labelStyle, marginBottom: 0 }}>Viewer</span>
        <Badge color={viewerColor[viewer]}>{viewer}</Badge>
      </div>
    </div>
  );
}

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
  terminating,
  trading,
  viewerState,
  viewerEpoch,
  onReconnect,
  onViewerLoad,
}: {
  session: InteractionSessionResponse;
  launchDescriptor: SafeLaunchDescriptor | null;
  onTerminate: () => void;
  terminating: boolean;
  trading: TradingBucket;
  viewerState: ViewerState;
  viewerEpoch: number;
  onReconnect: () => void;
  onViewerLoad: () => void;
}) {
  const isActive = session.state === "active" || session.state === "authorized";
  const isEnded = session.state === "ended";
  const mt5 = session.latest_mt5_session;
  const reconnecting = viewerState === "Reconnecting";
  // When trading itself is unavailable, viewer access is paused (PX-7B Task 4).
  const tradingUnavailable = trading === "Critical";
  const viewerEligible =
    isActive && !!mt5 && (mt5.state === "connected" || mt5.state === "launching");
  // Show the live iframe only when confidently connected/connecting with a
  // launch descriptor — never as a fallback for an uncertain state (Task 3).
  const showIframe =
    !tradingUnavailable &&
    !!launchDescriptor?.embed_url &&
    (viewerState === "Connected" || viewerState === "Connecting");
  const connecting = viewerState === "Connecting";

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

      {/* ── MT5 viewer — GuvFX-framed; trader sees MT5 or a GuvFX state, never raw Guacamole ── */}
      {viewerEligible && (
        <div
          style={{
            borderRadius: 12,
            border: "1px solid rgba(74, 179, 255, 0.15)",
            background: "rgba(0, 0, 0, 0.3)",
            overflow: "hidden",
            marginBottom: "1rem",
          }}
        >
          {/* GuvFX viewer header (no Guacamole terminology) */}
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
            <span style={{ fontSize: "0.8rem", color: "#94a3b8" }}>MT5 Terminal</span>
            <Badge color={viewerColor[viewerState]}>{viewerState}</Badge>
          </div>

          {tradingUnavailable ? (
            <ViewerPanel
              tone="error"
              title="Trading is currently unavailable"
              body="Viewer access is paused until trading health recovers."
            />
          ) : showIframe ? (
            <>
              {connecting && (
                <div style={{ padding: "0.5rem 1rem", fontSize: "0.8rem", color: "#93c5fd", background: "rgba(59,130,246,.08)" }}>
                  Opening MT5 viewer…
                </div>
              )}
              <iframe
                key={`mt5-viewer-${viewerEpoch}`}
                src={launchDescriptor!.embed_url}
                title="MT5 Terminal"
                onLoad={onViewerLoad}
                style={{ width: "100%", height: "600px", border: "none", display: "block" }}
                sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
              />
            </>
          ) : reconnecting ? (
            <ViewerPanel
              tone="reconnecting"
              title="Reconnecting terminal viewer…"
              body="Re-establishing the MT5 viewer session. Trading is unaffected."
            />
          ) : viewerState === "Error" ? (
            <ViewerPanel
              tone="error"
              title="MT5 viewer could not be opened"
              body="Trading status is checked separately. Try reconnecting the viewer."
              onReconnect={onReconnect}
            />
          ) : (
            <ViewerPanel
              tone="muted"
              title="Viewer session disconnected"
              body={
                trading === "Healthy"
                  ? "Trading remains healthy and broker is connected. Reconnect viewer to continue viewing MT5."
                  : "This is a viewer-only disconnection. Trading status is shown separately above and is unaffected by the viewer."
              }
              onReconnect={onReconnect}
            />
          )}
        </div>
      )}

      {/* Action buttons */}
      <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" as const, alignItems: "center" }}>
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
          <span style={{ fontSize: "0.8rem", color: "#8fa0b7", alignSelf: "center" }}>
            This session has ended. Launch a terminal below to start a new one.
          </span>
        )}
      </div>

      {/* ── Technical details (collapsed; not trader-facing priority) ── */}
      <details style={{ marginTop: "1rem" }}>
        <summary style={{ ...labelStyle, marginBottom: 0, cursor: "pointer", userSelect: "none" as const }}>
          Session details
        </summary>
        <div style={{ display: "flex", flexWrap: "wrap" as const, gap: "0.75rem 2rem", marginTop: "0.75rem" }}>
          <DetailRow label="Session ID" value={`#${session.id}`} />
          <DetailRow label="Account" value={session.terminal_identifier} />
          <DetailRow label="Started" value={session.started_at ? fmtDateTime(session.started_at) : null} />
          <DetailRow label="Expires" value={session.expires_at ? fmtDateTime(session.expires_at) : null} />
          <DetailRow label="Last activity" value={session.last_activity_at ? fmtDateTime(session.last_activity_at) : null} />
          {mt5 && <DetailRow label="Connected" value={mt5.connected_at ? fmtDateTime(mt5.connected_at) : null} />}
          {isEnded && <DetailRow label="Ended" value={session.ended_at ? fmtDateTime(session.ended_at) : null} />}
          {isEnded && session.terminated_reason && (
            <DetailRow label="Termination reason" value={session.terminated_reason} />
          )}
          {mt5 && mt5.state === "failed" && mt5.failure_reason && (
            <DetailRow label="Failure reason" value={mt5.failure_reason} />
          )}
        </div>
      </details>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// GuvFX viewer-state panel (replaces any raw Guacamole fallback UI)
// ─────────────────────────────────────────────────────────────────────

function ViewerPanel({
  tone,
  title,
  body,
  onReconnect,
}: {
  tone: "muted" | "reconnecting" | "error";
  title: string;
  body: string;
  onReconnect?: () => void;
}) {
  const toneColor =
    tone === "reconnecting" ? "#fcd34d" : tone === "error" ? "#fca5a5" : "#e9f4ff";
  return (
    <div style={{ padding: "2rem 1.75rem", textAlign: "center" as const, background: "rgba(255,255,255,0.01)" }}>
      <div style={{ fontSize: "0.95rem", color: toneColor, fontWeight: 600, marginBottom: "0.4rem" }}>
        {title}
      </div>
      <div style={{ fontSize: "0.85rem", color: "#b7c5dd", lineHeight: 1.6, marginBottom: onReconnect ? "1rem" : 0, maxWidth: 520, marginLeft: "auto", marginRight: "auto" }}>
        {body}
      </div>
      {onReconnect && <Button onClick={onReconnect}>Reconnect viewer</Button>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// TX-RDP3 — Guacamole clean-auth before embed
//
// The Guacamole webapp persists its session (GUAC_AUTH / GUAC_HISTORY /
// GUAC_PREFERENCES) in localStorage on the IFRAME ORIGIN. A stale session
// there — a prior json-auth session that ended up with no connections, or a
// PostgreSQL admin (Gadmin) login — SHADOWS the fresh per-launch ?data=
// token: the webapp reuses the stale session, so the deep-link to
// mt5-terminal/json resolves against a connectionless session and Guacamole
// reports "The requested connection does not exist" (TX-RDP2).
//
// Fix (two parts, both required):
//   1. Pin the embed to THIS app's origin. The backend hard-codes
//      www.guvfx.com; when the user is on guvfx.com that forces the iframe
//      cross-origin, so the parent can neither share nor clear its storage.
//      Rewriting the origin to window.location.origin makes the iframe
//      same-origin → its localStorage IS this document's localStorage.
//   2. Clear the stale Guacamole keys immediately before (re)mounting, so the
//      fresh ?data= token is the first and only active auth state.
// Protocol-agnostic: only touches auth/session storage + the embed origin, so
// it is safe for the legacy VNC path and the future dedicated RDP path alike.
// No token/secret is read or logged — keys are removed by name only.
// ─────────────────────────────────────────────────────────────────────

const GUAC_STORAGE_KEYS = ["GUAC_AUTH", "GUAC_HISTORY", "GUAC_PREFERENCES"];

// Rewrite the embed URL's origin to this app's origin (string-swap the origin
// prefix only, so the #/client/<id>?data=<token> fragment — and its
// percent-encoding — is preserved exactly).
function pinEmbedToAppOrigin(embedUrl: string): string {
  if (typeof window === "undefined") return embedUrl;
  try {
    const embedOrigin = new URL(embedUrl).origin;
    if (
      embedOrigin &&
      embedOrigin !== window.location.origin &&
      embedUrl.startsWith(embedOrigin)
    ) {
      return window.location.origin + embedUrl.slice(embedOrigin.length);
    }
  } catch {
    /* malformed URL — fall back to the original */
  }
  return embedUrl;
}

// Drop any stale Guacamole session on this origin (best-effort; storage may be
// unavailable in private mode).
function clearStaleGuacSession(): void {
  if (typeof window === "undefined") return;
  try {
    for (const key of GUAC_STORAGE_KEYS) window.localStorage.removeItem(key);
  } catch {
    /* storage blocked — nothing else we can safely do client-side */
  }
}

// Returns a descriptor whose iframe is guaranteed to boot from a clean auth
// state: the embed is pinned to this app's origin and any stale Guacamole
// session on that origin is cleared. Apply at every point an embed_url enters
// viewer state (initial launch AND reconnect/resume).
function withCleanGuacAuth(descriptor: SafeLaunchDescriptor): SafeLaunchDescriptor {
  if (!descriptor?.embed_url) return descriptor;
  const embed_url = pinEmbedToAppOrigin(descriptor.embed_url);
  // Clear targets this document's origin, which (after pinning) is exactly the
  // origin the iframe will load from — so the stale session it would otherwise
  // reuse is gone before the ?data= token boots.
  clearStaleGuacSession();
  return { ...descriptor, embed_url };
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

  // ── PX-7A: Trading (source of truth = reliability) + Viewer (frontend-owned) ──
  const [trading, setTrading] = useState<TradingHealth>(null);
  const [viewerState, setViewerState] = useState<ViewerState>("Disconnected");
  const [viewerEpoch, setViewerEpoch] = useState(0); // bump → iframe remount with fresh creds
  const activeSessionRef = useRef<InteractionSessionResponse | null>(null);
  const wasHiddenRef = useRef(false); // tab was backgrounded (tunnel likely dropped)
  const autoReconnectedRef = useRef<number | null>(null); // session id auto-reconnected once

  // ── MT5 credential status state ──
  const [credStatus, setCredStatus] = useState<Mt5CredentialStatus>(null);
  const [credLoading, setCredLoading] = useState(true);
  const [desktopLaunching, setDesktopLaunching] = useState(false);
  const [desktopUrl, setDesktopUrl] = useState<string | null>(null);

  // ── Notice state ──
  const [notice, setNotice] = useState<{ type: "info" | "warning" | "error"; message: string } | null>(null);

  // ── Polling interval for session status ──
  const [pollInterval, setPollInterval] = useState<ReturnType<typeof setInterval> | null>(null);

  // ── Fetch MT5 credential status ──
  const fetchCredStatus = useCallback(async () => {
    setCredLoading(true);
    try {
      const data = await apiFetch<{ credential: Mt5CredentialStatus }>(
        "/api/mt5/status/",
        {}
      );
      setCredStatus(data.credential);
    } catch {
      // Non-blocking; credential card simply won't render
      setCredStatus(null);
    } finally {
      setCredLoading(false);
    }
  }, []);

  // ── Fetch trading health (SOURCE OF TRUTH — never computed here) ──
  const fetchTradingHealth = useCallback(async (): Promise<TradingHealth> => {
    try {
      const data = await apiFetch<TradingHealth>("/api/reliability/trading-health/", {});
      setTrading(data);
      return data;
    } catch {
      // Endpoint unreachable → Unknown (do NOT infer trading from viewer).
      setTrading(null);
      return null;
    }
  }, []);

  // ── Re-discover the user's current resumable session (PX-7A endpoint) ──
  const fetchActiveSession = useCallback(async (): Promise<InteractionSessionResponse | null> => {
    try {
      const data = await apiFetch<{ active_session: InteractionSessionResponse | null }>(
        "/api/mt5-interaction/sessions/active/",
        {}
      );
      return data.active_session;
    } catch {
      return null;
    }
  }, []);

  // ── Launch desktop link ──
  const handleDesktopLaunch = useCallback(async () => {
    setDesktopLaunching(true);
    setNotice(null);
    setDesktopUrl(null);
    try {
      const data = await apiFetch<{ url: string }>(
        "/api/mt5/desktop-link/",
        { method: "POST" }
      );
      setDesktopUrl(data.url);
      setNotice({ type: "info", message: "Desktop link generated. Opening MT5 terminal..." });
      // Open in new tab
      window.open(data.url, "_blank", "noopener,noreferrer");
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Failed to launch MT5 desktop.";
      setNotice({ type: "error", message });
    } finally {
      setDesktopLaunching(false);
    }
  }, []);

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
    setLaunchDescriptor(null);
    setViewerState("Connecting");
    autoReconnectedRef.current = null;
    try {
      const data = await apiFetch<InteractionSessionResponse & { launch_descriptor?: SafeLaunchDescriptor }>(
        "/api/mt5-interaction/sessions/",
        {
          method: "POST",
          body: JSON.stringify({ terminal_binding_id: bindingId }),
        }
      );
      setActiveSession(data);
      if (data.launch_descriptor?.embed_url) {
        // TX-RDP3: clean stale Guacamole auth + pin origin before mounting.
        setLaunchDescriptor(withCleanGuacAuth(data.launch_descriptor));
        setViewerState("Connecting"); // iframe onLoad → Connected
      } else {
        setViewerState("Disconnected"); // no embed yet; Reconnect viewer available
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
    setViewerState("Disconnected");
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

  // ── Reconnect viewer (PX-7A core) ──
  // Resumes the live Guacamole tunnel for an ACTIVE session by fetching a
  // fresh embed_url. NO logout/login, NO new MT5Session, NO lifecycle change
  // (backend resolve_resumable forbids mutation). Viewer-only operation.
  const reconnectViewer = useCallback(
    async (sessionId: number): Promise<boolean> => {
      setViewerState("Reconnecting");
      setNotice(null);
      try {
        const data = await apiFetch<ResumableContextResponse>(
          `/api/mt5-interaction/sessions/${sessionId}/resume/`,
          { method: "POST" }
        );
        if (data.interaction_session) {
          setActiveSession(data.interaction_session);
        }
        if (data.launch_descriptor?.embed_url) {
          // TX-RDP3: clean stale Guacamole auth + pin origin before remounting.
          setLaunchDescriptor(withCleanGuacAuth(data.launch_descriptor));
          setViewerEpoch((e) => e + 1); // force iframe remount with fresh credentials
          setViewerState("Connecting"); // iframe onLoad → Connected
          return true;
        }
        // Resumable but adapter returned no embed — offer manual retry.
        setViewerState("Disconnected");
        return false;
      } catch (err: unknown) {
        // Session no longer resumable (expired/ended/occupancy lost) → Error.
        const message = err instanceof Error ? err.message : "Failed to reconnect viewer.";
        setViewerState("Error");
        setNotice({ type: "warning", message: `Viewer reconnect failed: ${message}` });
        return false;
      }
    },
    []
  );

  // ── Poll active session status + trading health (keeps both badges live) ──
  useEffect(() => {
    if (!activeSession) return;
    if (activeSession.state === "ended") return;

    const interval = setInterval(() => {
      fetchSessionStatus(activeSession.id);
      fetchTradingHealth();
    }, 10000); // Poll every 10 seconds

    setPollInterval(interval);
    return () => {
      clearInterval(interval);
      setPollInterval(null);
    };
  }, [activeSession?.id, activeSession?.state, fetchSessionStatus, fetchTradingHealth]);

  // ── Keep a ref of the active session for event handlers ──
  useEffect(() => {
    activeSessionRef.current = activeSession;
  }, [activeSession]);

  // ── Bootstrap on page load (TASK 3) ──
  useEffect(() => {
    let cancelled = false;
    (async () => {
      fetchBindings();
      fetchCredStatus();
      const [th, session] = await Promise.all([fetchTradingHealth(), fetchActiveSession()]);
      if (cancelled) return;
      if (!session) {
        setViewerState("Disconnected");
        return;
      }
      setActiveSession(session);
      const mt5 = session.latest_mt5_session;
      const viewerEligible =
        (session.state === "active" || session.state === "authorized") &&
        !!mt5 &&
        (mt5.state === "connected" || mt5.state === "launching");
      if (!viewerEligible) {
        setViewerState("Disconnected");
        return;
      }
      // After a reload/navigation the live tunnel is almost always gone, even
      // though the backend session is still active. Reconnect once if trading
      // is healthy; otherwise present the Reconnect viewer button.
      setViewerState("Disconnected");
      if (tradingBucket(th) === "Healthy" && autoReconnectedRef.current !== session.id) {
        autoReconnectedRef.current = session.id;
        await reconnectViewer(session.id);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Tab visibility change (TASK 4) ──
  useEffect(() => {
    const onVisibility = async () => {
      if (typeof document === "undefined") return;
      if (document.hidden) {
        wasHiddenRef.current = true;
        // Tunnel drops while hidden — reflect that the viewer is no longer live.
        // Trading status is intentionally left untouched.
        setViewerState((s) => (s === "Connected" || s === "Connecting" ? "Disconnected" : s));
        return;
      }
      // Became visible again: refresh BOTH trading and viewer/session state.
      const th = await fetchTradingHealth();
      const session = activeSessionRef.current;
      if (!session) return;
      const refreshed = await fetchSessionStatus(session.id);
      const s = refreshed || session;
      const mt5 = s.latest_mt5_session;
      const viewerEligible =
        (s.state === "active" || s.state === "authorized") &&
        !!mt5 &&
        (mt5.state === "connected" || mt5.state === "launching");
      if (wasHiddenRef.current && viewerEligible) {
        wasHiddenRef.current = false;
        if (tradingBucket(th) === "Healthy") {
          await reconnectViewer(s.id); // attempt reconnect once on return
        } else {
          setViewerState("Disconnected"); // viewer-only; trading shown separately
        }
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [fetchTradingHealth, fetchActiveSession, fetchSessionStatus, reconnectViewer, fetchBindings, fetchCredStatus]);

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

      {/* ── PX-7A: separated Trading vs Viewer status ── */}
      <StatusHeader trading={tradingBucket(trading)} viewer={viewerState} />

      {/* ── Safety message ── */}
      <StateNotice type="info" message="This session is restricted to MT5 interaction only." />

      {/* ── MT5 Runtime Status card ── */}
      {!credLoading && credStatus && (
        <div style={{ ...glassCard, marginBottom: "1rem" }}>
          <div style={sectionHeader}>MT5 Runtime Status</div>
          <div style={{ display: "flex", flexWrap: "wrap" as const, gap: "1rem 2rem", alignItems: "center", marginBottom: "1rem" }}>
            <DetailRow label="Login" value={credStatus.login} />
            <DetailRow label="Server" value={credStatus.server} />
            <div style={{ minWidth: 180 }}>
              <div style={labelStyle}>Status</div>
              <Badge
                color={
                  credStatus.last_status === "SUCCESS"
                    ? "green"
                    : credStatus.last_status === "FAILED"
                      ? "red"
                      : credStatus.last_status === "PENDING"
                        ? "yellow"
                        : "gray"
                }
              >
                {credStatus.last_status ?? "Unknown"}
              </Badge>
            </div>
            {credStatus.last_error && (
              <DetailRow label="Last Error" value={credStatus.last_error} />
            )}
            {credStatus.last_verified_at && (
              <DetailRow label="Verified" value={fmtDateTime(credStatus.last_verified_at)} />
            )}
          </div>
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
            <Button
              onClick={handleDesktopLaunch}
              disabled={desktopLaunching || credStatus.last_status !== "SUCCESS"}
            >
              {desktopLaunching ? "Launching..." : "Launch MT5 Desktop"}
            </Button>
            {credStatus.last_status !== "SUCCESS" && (
              <span style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
                Validate credentials before launching.
              </span>
            )}
          </div>
        </div>
      )}

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
          terminating={terminating}
          trading={tradingBucket(trading)}
          viewerState={viewerState}
          viewerEpoch={viewerEpoch}
          onReconnect={() => activeSession && reconnectViewer(activeSession.id)}
          onViewerLoad={() => setViewerState((s) => (s === "Connecting" ? "Connected" : s))}
        />
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
                        Current session
                      </Button>
                    ) : (
                      <Button
                        variant={isAvailable ? "primary" : "secondary"}
                        disabled={!isAvailable || launching}
                        onClick={() => handleLaunch(binding.id)}
                        style={{ fontSize: "0.8rem", padding: "0.35rem 0.8rem" }}
                      >
                        {isLaunching ? "Launching..." : bindingActionLabel(binding.status)}
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
