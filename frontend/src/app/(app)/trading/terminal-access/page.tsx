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
import { withCleanGuacAuth } from "@/lib/guac-embed";
import { HostedMt5RemoteApp } from "@/components/hosted/HostedMt5RemoteApp";
import { useLang } from "@/components/AppShell";
import { localeFor, t, type Lang } from "@/lib/i18n";
import { LocalizedBetaSurface } from "@/components/i18n/LocalizedBetaSurface";
import { localizeActiveBetaCopy, localizeBackendCustomerText, localizeControlledEnum } from "@/lib/active-beta-i18n";

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

const fmtDateTime = (lang: Lang, iso: string) =>
  new Date(iso).toLocaleDateString(localeFor(lang), {
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
const bindingActionLabel = (lang: Lang, status: string): string => {
  switch (status) {
    case "available":
      return t(lang, "terminalAccess.launch");
    case "launching":
    case "active":
      return t(lang, "terminalAccess.inUse");
    case "suspended":
      return t(lang, "terminalAccess.suspended");
    case "maintenance":
      return t(lang, "terminalAccess.maintenance");
    case "locked":
      return t(lang, "terminalAccess.locked");
    default:
      return t(lang, "terminalAccess.busy");
  }
};

const credentialStatusLabel = (lang: Lang, status: string | null): string => {
  switch (status) {
    case "SUCCESS":
      return t(lang, "terminalAccess.credential.success");
    case "FAILED":
      return t(lang, "terminalAccess.credential.failed");
    case "PENDING":
      return t(lang, "terminalAccess.credential.pending");
    case "NEVER":
      return t(lang, "terminalAccess.credential.never");
    case "TIMEOUT":
      return t(lang, "terminalAccess.credential.timeout");
    default:
      return t(lang, "terminalAccess.credential.unknown");
  }
};

// ─────────────────────────────────────────────────────────────────────
// Status header — two clearly separated badges: Trading and Viewer
// ─────────────────────────────────────────────────────────────────────

function StatusHeader({ trading, viewer }: { trading: TradingBucket; viewer: ViewerState }) {
  const lang = useLang();
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
        <span style={{ ...labelStyle, marginBottom: 0 }}>{t(lang, "terminalAccess.trading")}</span>
        <Badge color={tradingColor[trading]}>{t(lang, `terminalAccess.trading.${trading.toLowerCase()}`)}</Badge>
      </div>
      <div style={pill}>
        <span style={{ ...labelStyle, marginBottom: 0 }}>{t(lang, "terminalAccess.viewer")}</span>
        <Badge color={viewerColor[viewer]}>{t(lang, `terminalAccess.viewer.${viewer.toLowerCase()}`)}</Badge>
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
  const lang = useLang();
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
    <LocalizedBetaSurface lang={lang}>
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
          {localizeControlledEnum(lang, "status", session.state)}
        </Badge>
        <Badge color={bindingStatusColor[session.environment_type] ?? "gray"}>
          {localizeControlledEnum(lang, "status", session.environment_type)}
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
          <DetailRow label="Started" value={session.started_at ? fmtDateTime(lang, session.started_at) : null} />
          <DetailRow label="Expires" value={session.expires_at ? fmtDateTime(lang, session.expires_at) : null} />
          <DetailRow label="Last activity" value={session.last_activity_at ? fmtDateTime(lang, session.last_activity_at) : null} />
          {mt5 && <DetailRow label="Connected" value={mt5.connected_at ? fmtDateTime(lang, mt5.connected_at) : null} />}
          {isEnded && <DetailRow label="Ended" value={session.ended_at ? fmtDateTime(lang, session.ended_at) : null} />}
          {isEnded && session.terminated_reason && (
            <DetailRow label="Termination reason" value={localizeBackendCustomerText(lang, session.terminated_reason, "account-detail")} />
          )}
          {mt5 && mt5.state === "failed" && mt5.failure_reason && (
            <DetailRow label="Failure reason" value={localizeBackendCustomerText(lang, mt5.failure_reason, "error")} />
          )}
        </div>
      </details>
    </div>
    </LocalizedBetaSurface>
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
  const lang = useLang();
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
      {onReconnect && <Button onClick={onReconnect}>{localizeActiveBetaCopy(lang, "Reconnect viewer")}</Button>}
    </div>
  );
}


// ─────────────────────────────────────────────────────────────────────
// Main page component
// ─────────────────────────────────────────────────────────────────────

export default function TerminalAccessPage() {
  const lang = useLang();
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

  // ── ADR-0034 Hosted MT5 Workspace active? (portable RemoteApp is the SOLE customer path) ──
  // When an owned hosted workspace is detected, the customer's MT5 opens via the RemoteApp card above and the
  // ENTIRE legacy customer experience (active-session iframe, auto-reconnect, terminal list, desktop launch)
  // is suppressed — the legacy full-desktop path is retained ONLY as separate operator recovery, never shown
  // to the hosted customer. ``hostedResolved`` gates the legacy bootstrap so the legacy session is never even
  // discovered/reconnected for a hosted owner (fixes the full-desktop exposure defect).
  const [hostedActive, setHostedActive] = useState(false);
  const [hostedResolved, setHostedResolved] = useState(false);
  const hostedActiveRef = useRef(false);
  const onHostedResolved = useCallback((active: boolean) => {
    hostedActiveRef.current = active;
    setHostedActive(active);
    setHostedResolved(true);
  }, []);

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
    // Defence-in-depth: a hosted owner must never open the legacy full MT5 desktop, even if a stale button
    // were somehow clicked during the detection window.
    if (hostedActiveRef.current) return;
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
    // Defence-in-depth: a hosted owner must never launch the legacy full-desktop session, even if a stale
    // bindings button were somehow clicked during the detection window.
    if (hostedActiveRef.current) return;
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

  // ── Bootstrap prep on page load (harmless for hosted + legacy alike) ──
  useEffect(() => {
    fetchBindings();
    fetchCredStatus();
    fetchTradingHealth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Legacy active-session discovery + auto-reconnect (LEGACY customers ONLY) ──
  // Deferred until the hosted-workspace probe resolves. For a hosted owner this NEVER runs — the legacy
  // Administrator/full-desktop session is not discovered, set, or reconnected; the RemoteApp card is the only
  // customer experience. This is the fix for the full-desktop exposure defect (§10 of the corrective packet).
  useEffect(() => {
    if (!hostedResolved) return;      // wait until we know whether this is a hosted owner
    if (hostedActive) {               // hosted owner -> suppress the entire legacy customer experience
      setViewerState("Disconnected");
      return;
    }
    let cancelled = false;
    (async () => {
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
  }, [hostedResolved, hostedActive]);

  // ── Tab visibility change (TASK 4) ──
  useEffect(() => {
    const onVisibility = async () => {
      if (typeof document === "undefined") return;
      // Hosted owner: the legacy viewer is never used, so never reconnect it on tab return.
      if (hostedActiveRef.current) return;
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
    <LocalizedBetaSurface lang={lang}>
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>{t(lang, "terminalAccess.title")}</h1>
      <p
        style={{
          fontSize: "0.9rem",
          color: "#b7c5dd",
          marginBottom: "1.5rem",
        }}
      >
        {t(lang, "terminalAccess.subtitle")}
      </p>

      {/* ── PX-7A: separated Trading vs Viewer status ── */}
      <StatusHeader trading={tradingBucket(trading)} viewer={viewerState} />

      {/* ── Safety message ── */}
      <StateNotice type="info" message={t(lang, "terminalAccess.restricted")} />

      {/* ── ADR-0034 Hosted MT5 Workspace — portable RemoteApp (customer path; invisible unless owned) ── */}
      <HostedMt5RemoteApp onActiveChange={onHostedResolved} />

      {/* ── MT5 Runtime Status card ── */}
      {!credLoading && credStatus && (
        <div style={{ ...glassCard, marginBottom: "1rem" }}>
          <div style={sectionHeader}>{t(lang, "terminalAccess.runtimeStatus")}</div>
          <div style={{ display: "flex", flexWrap: "wrap" as const, gap: "1rem 2rem", alignItems: "center", marginBottom: "1rem" }}>
            <DetailRow label={t(lang, "terminalAccess.login")} value={credStatus.login} />
            <DetailRow label={t(lang, "terminalAccess.server")} value={credStatus.server} />
            <div style={{ minWidth: 180 }}>
              <div style={labelStyle}>{t(lang, "terminalAccess.status")}</div>
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
                {credentialStatusLabel(lang, credStatus.last_status)}
              </Badge>
            </div>
            {credStatus.last_error && (
              <DetailRow label={t(lang, "terminalAccess.lastError")} value={t(lang, "terminalAccess.credentialError")} />
            )}
            {credStatus.last_verified_at && (
              <DetailRow label={t(lang, "terminalAccess.verified")} value={fmtDateTime(lang, credStatus.last_verified_at)} />
            )}
          </div>
          {/* Full-desktop launch is the legacy customer path. It is shown ONLY once the hosted probe has
              resolved AND the user is confirmed non-hosted — never during detection (so a hosted owner can
              never open the full desktop in the pre-resolution window) and never for a hosted owner (whose
              MT5 opens via the RemoteApp card above). Retained solely as operator recovery. */}
          {!hostedResolved ? (
            <div style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
              {t(lang, "terminal.preparing")}
            </div>
          ) : hostedActive ? (
            <div style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
              {t(lang, "terminalAccess.opensAbove")}
            </div>
          ) : (
            <div style={{ display: "flex", gap: "0.75rem", alignItems: "center" }}>
              <Button
                onClick={handleDesktopLaunch}
                disabled={desktopLaunching || credStatus.last_status !== "SUCCESS"}
              >
                {desktopLaunching ? t(lang, "terminalAccess.launching") : t(lang, "terminalAccess.launchDesktop")}
              </Button>
              {credStatus.last_status !== "SUCCESS" && (
                <span style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
                  {t(lang, "terminalAccess.validateFirst")}
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── State notices ── */}
      {notice && <StateNotice type={notice.type} message={localizeBackendCustomerText(lang, notice.message, notice.type === "info" ? "notice" : notice.type)} />}

      {/* ── Active session card (shown when a session exists) ── */}
      {sessionLoading && (
        <div style={{ ...glassCard, marginBottom: "1rem" }}>
          <div style={{ fontSize: "0.9rem", color: "#8fa0b7" }}>{t(lang, "terminalAccess.loadingSession")}</div>
        </div>
      )}

      {!sessionLoading && sessionError && !activeSession && (
        <StateNotice type="error" message={sessionError} />
      )}

      {/* Legacy active-session card (full-desktop viewer) — NEVER shown to a hosted owner, and not shown
          until the hosted probe resolves (no pre-resolution flash/race). The hosted customer's MT5 opens
          only via the RemoteApp card above; the legacy desktop path is operator recovery. */}
      {activeSession && hostedResolved && !hostedActive && (
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

      {/* ── Terminal bindings list (legacy launch path) — hidden from the hosted customer AND during the
          hosted probe, so the Launch button is never interactive before hosted-ownership is known. ── */}
      {hostedResolved && !hostedActive && (
      <div style={{ ...glassCard, marginBottom: "1.5rem" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: "1rem",
          }}
        >
          <div style={sectionHeader}>{t(lang, "terminalAccess.availableTerminals")}</div>
          <Button
            variant="secondary"
            onClick={fetchBindings}
            disabled={bindingsLoading}
            style={{ fontSize: "0.8rem", padding: "0.35rem 0.8rem" }}
          >
            {bindingsLoading ? t(lang, "common.loading") : t(lang, "terminalAccess.refresh")}
          </Button>
        </div>

        {/* Loading */}
        {bindingsLoading && bindings.length === 0 && (
          <div style={{ fontSize: "0.9rem", color: "#8fa0b7", textAlign: "center" as const, padding: "2rem 0" }}>
            {t(lang, "terminalAccess.loadingTerminals")}
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
              {t(lang, "terminalAccess.noTerminals")}
            </div>
            <div style={{ fontSize: "0.8rem", color: "#64748b" }}>
              {t(lang, "terminalAccess.noTerminalsBody")}
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
                      <Badge color="gray">{t(lang, "terminalAccess.sharedView")}</Badge>
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
                        {t(lang, "terminalAccess.currentSession")}
                      </Button>
                    ) : (
                      <Button
                        variant={isAvailable ? "primary" : "secondary"}
                        disabled={!isAvailable || launching}
                        onClick={() => handleLaunch(binding.id)}
                        style={{ fontSize: "0.8rem", padding: "0.35rem 0.8rem" }}
                      >
                        {isLaunching ? t(lang, "terminalAccess.launching") : bindingActionLabel(lang, binding.status)}
                      </Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
      )}
    </div>
    </LocalizedBetaSurface>
  );
}
