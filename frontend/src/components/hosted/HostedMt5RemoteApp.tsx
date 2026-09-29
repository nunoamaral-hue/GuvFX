"use client";

// ─────────────────────────────────────────────────────────────────────
// ADR-0034 Hosted MT5 Workspace — portable RemoteApp (the customer path)
//
// EXTRACTED (AJ#4) from trading/terminal-access/page.tsx — the exact same
// implementation, now a shared component so it can be embedded both in
// Terminal Access (the advanced page) AND inside the hosted onboarding journey
// (so the customer never leaves onboarding to open MetaTrader). No new
// transport, authentication, delivery, or lifecycle: identical behaviour.
//
// Owner-scoped and fully server-derived: the browser sends ONLY its own
// account_id (intent). The backend mints the signed Guacamole RemoteApp
// descriptor — host, Windows identity, RemoteApp program, args and the
// credential are all resolved server-side; the Windows password rides only
// inside the encrypted token and is never returned here. This opens the
// portable MT5 RemoteApp (a single MT5 window), NOT a full desktop.
//
// DARK / bounded: the delivery-state probe 404s unless the delivery flags are
// ON *and* the signed-in user owns a hosted workspace, so this whole card is
// invisible (and reports inactive) for everyone else.
// ─────────────────────────────────────────────────────────────────────

import { useEffect, useState, useCallback, useRef } from "react";
import { apiFetch } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import type { SafeLaunchDescriptor } from "@/types/mt5-interaction";
import { withCleanGuacAuth } from "@/lib/guac-embed";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";

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

const sectionHeader: React.CSSProperties = {
  fontSize: "0.8rem",
  color: "#94a3b8",
  textTransform: "uppercase" as const,
  letterSpacing: "0.06em",
  fontWeight: 600,
  marginBottom: "0.75rem",
};

type HostedAccount = { id: number; label: string };

// `onActiveChange` is OPTIONAL — Terminal Access uses it to suppress its legacy
// customer experience once a hosted workspace is detected; the onboarding embed
// doesn't need it (it already knows the customer is hosted).
// AJ#5.1: `onConnected` is an OPTIONAL diagnostic hook fired once when the terminal descriptor is minted (the
// terminal is launched). It carries no data and does no I/O — onboarding uses it only to record a local
// "MT5 launched" timestamp for future timing investigations. Terminal Access omits it (no behaviour change).
export function HostedMt5RemoteApp({ onActiveChange, onConnected, accountId }: {
  onActiveChange?: (active: boolean) => void;
  onConnected?: () => void;
  /** Phase 9 (multi-account) — when provided, this card binds to EXACTLY this owned TradingAccount.id (e.g.
   * "Open MT5" for a specific Broker Account) instead of auto-detecting the user's single hosted account. The
   * ownership check is preserved (delivery-state is_owner), and connect/mint is owner-only regardless. */
  accountId?: number;
}) {
  const lang = useLang();
  const [account, setAccount] = useState<HostedAccount | null>(null);
  const [detecting, setDetecting] = useState(true);
  const [descriptor, setDescriptor] = useState<SafeLaunchDescriptor | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [notReady, setNotReady] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [slow, setSlow] = useState(false);
  const [maximized, setMaximized] = useState(false);
  const shellRef = useRef<HTMLDivElement | null>(null);
  const browserFullscreenRef = useRef(false);
  // First-launch broker-discovery guidance. GuvFX CANNOT observe MT5's "search for your broker" step (the observer
  // only reads post-login account_info), so this is honest member GUIDANCE, never a progress claim. Show/suppress
  // is gated on a DURABLE, server-derived signal - never browser-local state - so it appears on any device during
  // the pre-connection broker search + login and stops for good ONCE GuvFX has authoritatively observed the broker
  // connected to the CORRECT account. The signal is read from the SAME owner-scoped delivery-state endpoint the
  // detection effect already probes (never /api/trading/accounts/, which the account-explicit isolation guard
  // forbids here): its allow-listed, secret-free projection carries the member's own expected broker/server plus
  // `broker_ever_matched` (the set-once latch: True the first time proj_connected AND proj_account_match; never
  // cleared; read-model only, never order authority). Because it is a latch, a later disconnect/mismatch of an
  // ESTABLISHED account never re-shows the first-launch wizard copy - only a genuine first launch does. A light
  // poll self-suppresses on the first correct connect without a manual refresh (real backend state, not a timer).
  const [acctInfo, setAcctInfo] = useState<{ broker: string; server: string; complete: boolean } | null>(null);
  useEffect(() => {
    if (!account) { setAcctInfo(null); return; }
    let cancelled = false;
    const load = async (): Promise<boolean> => {
      try {
        const a = await apiFetch<{ broker_display_name?: string; server_name?: string; broker_ever_matched?: boolean }>(
          `/api/hosted-workspace/delivery-state/?account_id=${account.id}`, {});
        if (cancelled) return false;
        // `broker_ever_matched` true => this account has connected to the CORRECT account at least once (first
        // launch is complete) -> suppress for good. Anything else (never correctly connected, or the field absent)
        // keeps the first-launch guidance.
        const complete = a?.broker_ever_matched === true;
        setAcctInfo({ broker: a?.broker_display_name || "", server: a?.server_name || "", complete });
        return complete;
      } catch {
        // Can't confirm connected -> default to SHOWING guidance (safe: guidance is never harmful; the identity
        // pin remains the authoritative boundary regardless).
        if (!cancelled) setAcctInfo({ broker: "", server: "", complete: false });
        return false;
      }
    };
    let timer: ReturnType<typeof setInterval> | undefined;
    void load().then((complete) => {
      if (cancelled || complete) return;
      timer = setInterval(() => { void load().then((done) => { if (done && timer) { clearInterval(timer); timer = undefined; } }); }, 20000);
    });
    return () => { cancelled = true; if (timer) clearInterval(timer); };
  }, [account]);
  // Only once we have a definitive backend answer AND it is not-yet-connected. Established accounts (complete) never
  // show it; the brief pre-answer window shows nothing (no flash).
  const showDiscovery = !!acctInfo && acctInfo.complete === false;
  // Keyboard-focus management for the embedded Guacamole RemoteApp. Guacamole's key handler listens on the
  // iframe's OWN document, so keystrokes only reach MT5 while the iframe holds DOM focus (mouse works without
  // focus, keyboard does not — the "mouse works / keyboard dead" symptom). We give the iframe an explicit ref
  // and focus it (a) once it finishes loading and (b) whenever the user points/clicks anywhere on the terminal
  // card. This never synthesises keys, never reads key events, never remounts the iframe, and never steals
  // focus on a timer — it only forwards the user's own focus intent to where Guacamole is listening.
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const focusTerminal = useCallback(() => {
    try {
      // preventScroll: forwarding keyboard focus must never yank the page's scroll position (e.g. if the
      // embedded client ever reloads its own document and re-fires onLoad while the user has scrolled away).
      iframeRef.current?.focus({ preventScroll: true });
    } catch {
      /* focus may throw in exotic states; never fatal */
    }
  }, []);

  // Full-screen is a presentation-only state around the SAME iframe. The descriptor, iframe key and src stay
  // unchanged, so expanding/collapsing cannot mint a second Guacamole session or restart RemoteApp/MT5.
  // Browser fullscreen is requested as progressive enhancement; denial leaves the in-app viewport-maximized
  // fallback active. ESC from browser fullscreen collapses the in-app state as customers expect.
  const enterFullScreen = useCallback(async () => {
    setMaximized(true);
    try {
      await shellRef.current?.requestFullscreen?.();
    } catch {
      // Permission denied or unsupported: the in-app maximized fallback remains active.
    }
  }, []);

  const exitFullScreen = useCallback(async () => {
    setMaximized(false);
    try {
      if (document.fullscreenElement === shellRef.current) await document.exitFullscreen?.();
    } catch {
      // The in-app state is already restored; browser API failure is non-fatal.
    }
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onFullscreenChange = () => {
      if (document.fullscreenElement === shellRef.current) {
        browserFullscreenRef.current = true;
      } else if (browserFullscreenRef.current) {
        browserFullscreenRef.current = false;
        setMaximized(false);
      }
    };
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, []);

  useEffect(() => {
    if (!maximized || typeof document === "undefined") return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, [maximized]);

  // AJ#4 keyboard hardening: when the browser tab/window regains focus, re-forward focus to the terminal so the
  // very next keystroke reaches Guacamole. Guacamole's key handler listens on the iframe's document, and a tab
  // switch leaves DOM focus on the parent — the classic "came back to the tab, keyboard is dead" symptom. This
  // only fires on a genuine window-focus event (never on a timer, never on the 5s poll re-render), forwards the
  // user's own return intent, never synthesises or reads keys, and never remounts the iframe.
  //
  // AJ#5.1 evidence calibration (Objective 3): guacd logs during acceptance testing show repeated RDP
  // client creation, "Disconnected by other connection", and "User is not responding". THE EVIDENCE STRONGLY
  // SUGGESTS that RDP session reconnection contributes to keyboard focus loss, BUT IT HAS NOT YET BEEN
  // CONCLUSIVELY PROVEN TO BE THE SOLE CAUSE — a live, instrumented single-flow reproduction is still needed to
  // isolate it from other possible contributors (e.g. remote-side RemoteApp modal-dialog focus routing). Keyboard
  // *translation* itself shows no errors (server-layout=en-us-qwerty is correctly pinned). These focus handlers
  // are a safe mitigation for the DOM-focus contributor, not a claimed complete fix.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onWinFocus = () => { if (iframeRef.current) focusTerminal(); };
    window.addEventListener("focus", onWinFocus);
    return () => window.removeEventListener("focus", onWinFocus);
  }, [focusTerminal]);

  // Detect a hosted workspace among the signed-in user's OWN accounts. Any
  // error / 404 (dark, or no hosted workspace) => stay invisible + inactive.
  useEffect(() => {
    let cancelled = false;
    let settled = false;
    // Resolve the hosted question exactly ONCE and ONLY on a DEFINITIVE answer: an owner is found (true), or
    // the account list was fetched and none is an owned hosted workspace (false). We deliberately FAIL CLOSED
    // on any AMBIGUOUS outcome (an accounts/delivery-state request that errors or hangs): we do NOT resolve to
    // "not hosted", because that would expose the legacy full-desktop path to a possibly-hosted owner. While
    // unresolved the card shows a neutral "preparing" state and the legacy UI stays suppressed (gated on
    // `hostedResolved` = this having fired). A genuinely hung /accounts is a whole-app failure; the safe
    // direction here is never a legacy desktop, only a "preparing" message.
    const settle = (active: boolean) => {
      if (settled || cancelled) return;
      settled = true;
      setDetecting(false);
      onActiveChange?.(active);
    };
    const timer = setTimeout(() => { if (!settled && !cancelled) setSlow(true); }, 10000); // message only; no settle
    // Bounded fetch: reject after `ms` so a hung request (apiFetch has no timeout) cannot wedge detection.
    const withTimeout = <T,>(p: Promise<T>, ms: number): Promise<T> =>
      Promise.race([p, new Promise<T>((_, reject) => setTimeout(() => reject(new Error("timeout")), ms))]);
    // apiFetch puts the numeric HTTP status on err.status / err.httpStatus (the message is the DRF detail
    // string, e.g. "Not found." — it does NOT contain "404"). A delivery-state 404 (dark / no workspace /
    // not-owner) is a DEFINITIVE not-owned answer; classify on the status code, never the message string.
    const is404 = (e: unknown) => {
      const s = e as { status?: number; httpStatus?: number } | null;
      return s?.status === 404 || s?.httpStatus === 404;
    };
    // Bounded retry for BOTH probes: a definitive 404 is re-thrown immediately (never retried — it is an
    // answer, not a failure); any other error/timeout is retried up to `attempts` times (1.5s backoff) so a
    // TRANSIENT blip does not strand a legacy user, whose legacy UI is gated on hostedResolved. Only a
    // PERSISTENT non-404 failure re-throws to the caller, which then fails closed (never expose legacy).
    const fetchRetry = async <T,>(fn: () => Promise<T>, attempts = 3): Promise<T> => {
      let lastErr: unknown;
      for (let i = 0; i < attempts; i++) {
        if (cancelled || settled) throw new Error("aborted");
        try {
          return await withTimeout(fn(), 5000);
        } catch (e) {
          if (is404(e)) throw e; // definitive answer — do not retry
          lastErr = e;
          if (i < attempts - 1) await new Promise((r) => setTimeout(r, 1500));
        }
      }
      throw lastErr;
    };
    // If an attempt cannot reach a DEFINITIVE answer (a persistent non-404 error/timeout on /accounts or on a
    // delivery-state probe), we FAIL CLOSED (never resolve to legacy for a possibly-hosted owner) AND auto-
    // retry the WHOLE detection every 15s. So a legacy user whose backend probe is transiently/partially
    // degraded recovers automatically once a definitive answer arrives — no manual refresh, no permanent stall.
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const scheduleRetry = () => {
      if (cancelled || settled) return;
      setSlow(true); // show the neutral "preparing… please refresh if it persists" message meanwhile
      retryTimer = setTimeout(() => { void attemptDetection(); }, 15000);
    };
    const attemptDetection = async () => {
      if (cancelled || settled) return;
      // Multi-account: bind to EXACTLY the requested account (owner-checked). Skip the auto-detect scan.
      if (accountId != null) {
        try {
          const state = await fetchRetry(() =>
            apiFetch<{ is_owner?: boolean }>(`/api/hosted-workspace/delivery-state/?account_id=${accountId}`, {}));
          if (cancelled || settled) return;
          if (state?.is_owner) { setAccount({ id: accountId, label: `#${accountId}` }); settle(true); }
          else settle(false); // definitive not-owned / not a hosted workspace for this account
        } catch (e) {
          if (is404(e)) settle(false); else scheduleRetry();
        } finally {
          clearTimeout(timer);
        }
        return;
      }
      try {
        let accounts: Array<{ id: number; name?: string; account_number?: string; is_active?: boolean }>;
        try {
          accounts = await fetchRetry(() =>
            apiFetch<Array<{ id: number; name?: string; account_number?: string; is_active?: boolean }>>(
              "/api/trading/accounts/", {}));
        } catch {
          scheduleRetry(); // persistent failure -> fail closed + auto-retry; never expose legacy
          return;
        }
        if (cancelled || settled) return;
        const ordered = [...accounts].sort(
          (a, b) => Number(!!b.is_active) - Number(!!a.is_active)
        );
        // Resolve to "not hosted" ONLY if EVERY account gave a DEFINITIVE answer (200 not-owner, or a 404 =
        // dark / no workspace / not-owner). A persistent AMBIGUOUS probe (non-404 error/timeout that survived
        // retries) means an account MIGHT be an owned workspace we could not confirm -> fail closed + retry.
        let ambiguous = false;
        for (const a of ordered) {
          if (cancelled || settled) return;
          try {
            const state = await fetchRetry(() =>
              apiFetch<{ is_owner?: boolean }>(`/api/hosted-workspace/delivery-state/?account_id=${a.id}`, {}));
            if (cancelled || settled) return;
            // Activate ONLY for an account the caller actually owns. `is_owner` is false when the state
            // endpoint answered via its staff read-bypass, so a staff viewer never binds this card to
            // another customer's workspace (and connect/mint is owner-only regardless).
            if (state?.is_owner) {
              setAccount({ id: a.id, label: a.name || a.account_number || String(a.id) });
              settle(true);
              return;
            }
            // else: definitive not-owner for this account — keep looking.
          } catch (e) {
            if (!is404(e)) ambiguous = true; // 404 = definitive not-owned; persistent non-404 = ambiguous
          }
        }
        if (cancelled || settled) return;
        if (ambiguous) { scheduleRetry(); return; } // could not confirm every account -> fail closed + auto-retry
        settle(false); // DEFINITIVE: accounts fetched, all accounts confirmed not-owned => not a hosted owner
      } catch {
        scheduleRetry(); // unexpected -> fail closed + auto-retry, never expose legacy
      } finally {
        clearTimeout(timer);
      }
    };
    void attemptDetection();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      clearTimeout(retryTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId]);

  const openTerminal = useCallback(async () => {
    if (!account) return;
    setConnecting(true);
    setError(null);
    setNotReady(null);
    setDescriptor(null);
    try {
      const d = await apiFetch<{
        transport_type: string;
        embed_url: string;
        session_token: string;
        expiry: number | null;
      }>("/api/hosted-workspace/delivery-connect/", {
        method: "POST",
        body: JSON.stringify({ account_id: account.id }),
      });
      const safe: SafeLaunchDescriptor = {
        transport_type: d.transport_type,
        embed_url: d.embed_url,
        session_token: d.session_token ?? "",
        expiry: d.expiry != null ? String(d.expiry) : null,
      };
      // Same origin-pinning + stale-session clear as the legacy viewer path.
      setDescriptor(withCleanGuacAuth(safe));
      setEpoch((e) => e + 1);
      onConnected?.();   // AJ#5.1 diagnostic: record the "MT5 launched" moment (no data, no I/O)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : t(lang, "terminal.openError");
      if (message.includes("409")) {
        setNotReady(t(lang, "terminal.notReady"));
      } else {
        setError(t(lang, "terminal.openError"));
      }
    } finally {
      setConnecting(false);
    }
  }, [account, lang, onConnected]);

  // Prominent, accessible first-launch broker-discovery warning shown ABOVE the MT5 window during the pre-connection
  // window. Amber house treatment + role="alert" + a warning glyph (never colour-alone). Concise: the primary
  // instruction and the MetaQuotes-first caveat are visible without expanding anything.
  const expLabel: React.CSSProperties = { fontSize: "0.65rem", textTransform: "uppercase", letterSpacing: "0.05em",
    color: "#94a3b8", fontWeight: 700 };
  const expVal: React.CSSProperties = { fontSize: "0.85rem", color: "#e9f4ff", fontWeight: 600 };
  const renderDiscovery = () => (
    <div role="alert" className="guvfx-discovery-warn" style={{
      display: "flex", gap: "0.7rem", padding: "0.85rem 1rem", borderRadius: 12,
      border: "1px solid rgba(251,191,36,0.45)", background: "rgba(251,191,36,0.09)",
      marginBottom: "1rem", lineHeight: 1.55 }}>
      <span aria-hidden style={{ fontSize: "1.1rem", lineHeight: 1.3 }}>⚠️</span>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.05em",
                      color: "#fbbf24", fontWeight: 700, marginBottom: 4 }}>
          {t(lang, "terminal.discoveryTitle")}
        </div>
        <p style={{ margin: "0 0 6px", color: "#fde68a", fontWeight: 700, fontSize: "0.92rem" }}>
          {t(lang, "terminal.discoveryPrimary")}
        </p>
        <p style={{ margin: "0 0 6px", color: "#e9d8a6", fontSize: "0.82rem" }}>{t(lang, "terminal.discoveryBody")}</p>
        <p style={{ margin: "0 0 8px", color: "#e9d8a6", fontSize: "0.82rem" }}>{t(lang, "terminal.discoveryMetaquotes")}</p>
        {(acctInfo?.broker || acctInfo?.server) && (
          <div style={{ display: "flex", gap: "1.5rem", flexWrap: "wrap" as const, margin: "0 0 8px" }}>
            {acctInfo?.broker && (
              <div><div style={expLabel}>{t(lang, "terminal.discoveryExpectedBroker")}</div>
                   <div style={expVal} data-testid="discovery-expected-broker">{acctInfo.broker}</div></div>)}
            {acctInfo?.server && (
              <div><div style={expLabel}>{t(lang, "terminal.discoveryExpectedServer")}</div>
                   <div style={expVal} data-testid="discovery-expected-server">{acctInfo.server}</div></div>)}
          </div>
        )}
        <ol style={{ margin: "0 0 4px", paddingLeft: "1.1rem", color: "#cbb78a", fontSize: "0.8rem" }}>
          <li>{t(lang, "terminal.discoveryStep1")}</li>
          <li>{t(lang, "terminal.discoveryStep2")}</li>
          <li>{t(lang, "terminal.discoveryStep3")}</li>
          <li>{t(lang, "terminal.discoveryStep4")}</li>
          <li>{t(lang, "terminal.discoveryStep5")}</li>
        </ol>
        <div style={{ fontSize: "0.75rem", color: "#94a3b8" }}>{t(lang, "terminal.discoveryWaiting")}</div>
      </div>
    </div>
  );

  if (!account) {
    // Still resolving hosted-ownership: show a neutral "preparing" message only if detection is slow (so a
    // normal fast probe does not flash for a non-hosted user). Once resolved-not-hosted this renders nothing.
    if (detecting && slow) {
      return (
        <div style={{ ...glassCard, marginBottom: "1rem" }}>
          <div style={sectionHeader}>{t(lang, "terminal.title")}</div>
          <p style={{ fontSize: "0.85rem", color: "#b7c5dd", margin: 0 }}>
            {t(lang, "terminal.preparingRefresh")}
          </p>
        </div>
      );
    }
    return null; // invisible until a hosted workspace is confirmed (or fast-resolved as non-hosted)
  }

  return (
    <div
      ref={shellRef}
      className="hosted-terminal-shell"
      data-terminal-maximized={maximized ? "true" : "false"}
      style={{
        ...glassCard,
        marginBottom: maximized ? 0 : "1rem",
        ...(maximized ? {
          position: "fixed" as const, inset: 0, zIndex: 1000, width: "100vw", height: "100dvh",
          padding: 0, border: "none", borderRadius: 0, background: "#050816",
        } : {}),
      }}
    >
      <div style={{ ...sectionHeader, display: maximized ? "none" : undefined }}>{t(lang, "terminal.title")}</div>
      <p
        style={{
          fontSize: "0.85rem",
          color: "#b7c5dd",
          marginTop: 0,
          marginBottom: "1rem",
          lineHeight: 1.6,
          display: maximized ? "none" : undefined,
        }}
      >
        {t(lang, "terminal.description", { account: account.label })}
      </p>

      {/* First-launch broker-discovery safeguard — sits ABOVE the MT5 window (both the launched iframe and the
          pre-open Open button). Durable: driven by the server-derived `broker_ever_matched` latch, not browser-local
          state, so it survives refresh, shows only for a genuine first launch, and never re-appears once the account
          has correctly connected even once. Hidden in full-screen (the description is hidden there too). */}
      {showDiscovery && !maximized && renderDiscovery()}

      {descriptor?.embed_url ? (
        // Forward the user's pointer intent to keyboard focus: a pointer-down anywhere on the terminal card
        // focuses the iframe so the very next keystroke reaches Guacamole (guacd forwards mouse without focus,
        // but keyboard needs the iframe focused). Capture phase so it runs even though the inner cross-frame
        // content also consumes the event.
        <div
          onPointerDownCapture={focusTerminal}
          style={{
            borderRadius: maximized ? 0 : 12,
            border: "1px solid rgba(74,179,255,0.15)",
            background: "rgba(0,0,0,0.3)",
            overflow: "hidden",
            flex: maximized ? 1 : undefined,
          }}
        >
          <div
            className="hosted-terminal-header"
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "0.5rem 1rem",
              background: "rgba(10,15,40,0.9)",
              borderBottom: "1px solid rgba(74,179,255,0.1)",
            }}
          >
            <span className="hosted-terminal-focus-hint" style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
              {t(lang, "terminal.focusHint")}
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexShrink: 0 }}>
              <Badge color="green">{t(lang, "terminal.connected")}</Badge>
              <button
                type="button"
                onClick={maximized ? exitFullScreen : enterFullScreen}
                aria-label={t(lang, maximized ? "terminal.exitFullScreen" : "terminal.fullScreen")}
                style={{
                  border: "1px solid rgba(74,179,255,0.35)", borderRadius: 8,
                  background: "rgba(74,179,255,0.08)", color: "#dbeafe", cursor: "pointer",
                  padding: "0.35rem 0.65rem", fontSize: "0.78rem", fontWeight: 600,
                }}
              >
                {t(lang, maximized ? "terminal.exitFullScreen" : "terminal.fullScreen")}
              </button>
            </div>
          </div>
          <iframe
            ref={iframeRef}
            key={`hosted-mt5-${epoch}`}
            src={descriptor.embed_url}
            title={t(lang, "terminal.iframeTitle")}
            // iframes are focusable by default; make it explicit for keyboard robustness.
            tabIndex={0}
            // Focus once the RemoteApp finishes loading so keystrokes reach MT5 without needing a first click.
            onLoad={focusTerminal}
            // Delegate clipboard Permissions-Policy to the (same-origin) Guacamole client so browser->MT5
            // PASTE works: Guacamole reads the local clipboard via the async Clipboard API, which is blocked in
            // an iframe unless clipboard-read/-write are granted here. Pairs with the server-side
            // disable-paste=false (browser->MT5 only); MT5->browser copy stays disabled server-side. This does
            // NOT widen the sandbox or enable drive/file/printer.
            allow="clipboard-read; clipboard-write"
            // AJ#4 polish: MT5 should feel like a normal desktop app. The RemoteApp desktop resizes to match the
            // iframe (guac `resize-method=display-update`), so a taller/wider iframe gives MT5 a real larger
            // desktop — crisp, not scaled. clamp() keeps a stable size that only changes on a genuine viewport
            // resize (never on the 5s onboarding poll), so it does NOT churn display-update / drop keyboard focus.
            style={{
              width: "100%",
              height: maximized ? "calc(100dvh - 48px)" : "clamp(750px, 80vh, 900px)",
              border: "none", display: "block",
            }}
            sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
          />
          <style>{`
            @media (max-width: 720px) {
              .hosted-terminal-shell { min-width: 0; }
              .hosted-terminal-header { padding: 0.45rem 0.6rem !important; }
              .hosted-terminal-focus-hint { display: none; }
            }
          `}</style>
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", flexWrap: "wrap" as const }}>
            <Button onClick={openTerminal} disabled={connecting}>
              {connecting ? t(lang, "terminal.opening") : t(lang, "terminal.open")}
            </Button>
            {connecting && (
              <span style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "0.8rem", color: "#94a3b8" }}>
                <span className="animate-spin" aria-hidden style={{ display: "inline-block", width: 14, height: 14,
                      border: "2px solid rgba(74,179,255,0.25)", borderTopColor: "#4ab3ff", borderRadius: "50%" }} />
                {t(lang, "terminal.preparing")}
              </span>
            )}
            {notReady && <span style={{ fontSize: "0.8rem", color: "#fbbf24", lineHeight: 1.5 }}>{notReady}</span>}
            {error && <span style={{ fontSize: "0.8rem", color: "#f87171" }}>{error}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
