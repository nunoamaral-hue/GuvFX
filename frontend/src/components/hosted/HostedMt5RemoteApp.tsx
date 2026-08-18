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
export function HostedMt5RemoteApp({ onActiveChange, onConnected }: {
  onActiveChange?: (active: boolean) => void;
  onConnected?: () => void;
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
  // First-launch guidance (PP5): the first time MT5 opens in a fresh workspace it downloads the broker's full
  // instrument catalogue, which can take minutes. Show the one-time warning until this browser has opened it
  // once (no first-launch flag exists on delivery-state, so we use a per-account localStorage marker).
  const [hasLaunched, setHasLaunched] = useState(true);
  const firstLaunchSessionRef = useRef(false);
  useEffect(() => {
    if (!account) return;
    let launched = true;
    try { launched = localStorage.getItem(`guvfx_mt5_launched_${account.id}`) === "1"; } catch { launched = false; }
    setHasLaunched(launched);
    firstLaunchSessionRef.current = !launched;
  }, [account]);
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
              setAccount({ id: a.id, label: a.name || a.account_number || `Account ${a.id}` });
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
  }, []);

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
      try { localStorage.setItem(`guvfx_mt5_launched_${account.id}`, "1"); } catch { /* non-fatal */ }
      setHasLaunched(true);
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
    <div style={{ ...glassCard, marginBottom: "1rem" }}>
      <div style={sectionHeader}>{t(lang, "terminal.title")}</div>
      <p
        style={{
          fontSize: "0.85rem",
          color: "#b7c5dd",
          marginTop: 0,
          marginBottom: "1rem",
          lineHeight: 1.6,
        }}
      >
        {t(lang, "terminal.description", { account: account.label })}
      </p>

      {descriptor?.embed_url ? (
        // Forward the user's pointer intent to keyboard focus: a pointer-down anywhere on the terminal card
        // focuses the iframe so the very next keystroke reaches Guacamole (guacd forwards mouse without focus,
        // but keyboard needs the iframe focused). Capture phase so it runs even though the inner cross-frame
        // content also consumes the event.
        <div
          onPointerDownCapture={focusTerminal}
          style={{
            borderRadius: 12,
            border: "1px solid rgba(74,179,255,0.15)",
            background: "rgba(0,0,0,0.3)",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "0.5rem 1rem",
              background: "rgba(10,15,40,0.9)",
              borderBottom: "1px solid rgba(74,179,255,0.1)",
            }}
          >
            <span style={{ fontSize: "0.8rem", color: "#94a3b8" }}>
              {t(lang, "terminal.focusHint")}
            </span>
            <Badge color="green">{t(lang, "terminal.connected")}</Badge>
          </div>
          {firstLaunchSessionRef.current && (
            <div style={{ padding: "0.5rem 1rem", background: "rgba(74,179,255,0.05)",
                          borderBottom: "1px solid rgba(74,179,255,0.1)", fontSize: "0.78rem", color: "#94a3b8",
                          lineHeight: 1.5 }}>
              {t(lang, "terminal.firstLaunchShort")}
            </div>
          )}
          <iframe
            ref={iframeRef}
            key={`hosted-mt5-${epoch}`}
            src={descriptor.embed_url}
            title="MT5 Terminal"
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
            style={{ width: "100%", height: "clamp(750px, 80vh, 900px)", border: "none", display: "block" }}
            sandbox="allow-same-origin allow-scripts allow-forms allow-popups"
          />
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          {!hasLaunched && (
            <div style={{ display: "flex", gap: "0.6rem", padding: "0.75rem 0.9rem", borderRadius: 10,
                          border: "1px solid rgba(74,179,255,0.2)", background: "rgba(74,179,255,0.06)" }}>
              <span aria-hidden style={{ fontSize: "0.95rem", lineHeight: 1.4 }}>ℹ️</span>
              <p style={{ fontSize: "0.82rem", color: "#b7c5dd", margin: 0, lineHeight: 1.6 }}>
                <strong style={{ color: "#e9f4ff" }}>{t(lang, "terminal.firstLaunchTitle")}</strong>{" "}
                {t(lang, "terminal.firstLaunchBody")}
              </p>
            </div>
          )}
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
