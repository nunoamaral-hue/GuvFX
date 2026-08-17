"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { EnableStrategyModal } from "@/components/strategy/EnableStrategyModal";
import {
  BETA_CONFIG_NOTE,
  configContract,
  disableStrategy,
  enableStrategy,
  fetchSignalCopyStatus,
  getStrategy,
  isAutomated,
  mpDisplayName,
  priceFor,
  priceLabel,
  type ConfigRow,
  type SignalCopyStatus,
} from "@/lib/strategy-journey";
import { fetchJourney, type HostedJourney } from "@/lib/hosted-journey";

type TradingAccount = {
  id: number;
  name: string;
  account_number?: string;
  is_demo?: boolean;
  is_active?: boolean;
};

const cardStyle: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.10)",
  borderRadius: 14,
  background: "linear-gradient(180deg, rgba(10,16,35,0.72), rgba(6,10,25,0.85))",
  boxShadow: "0 10px 30px rgba(0,0,0,0.45)",
  padding: "1.4rem",
};

const freeBadge: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "0.15rem 0.55rem",
  borderRadius: 999,
  border: "1px solid rgba(34,197,94,0.35)",
  background: "rgba(34,197,94,0.12)",
  color: "#86efac",
  fontSize: "0.72rem",
  fontWeight: 700,
};

function accountLabelOf(a: TradingAccount | undefined, fallbackId: number | null): string {
  if (a) return a.account_number ? `${a.name} (${a.account_number})` : a.name;
  return fallbackId ? `Account #${fallbackId}` : "—";
}

// ─────────────────────────────────────────────────────────────────────
function ConfigureInner() {
  const router = useRouter();
  const params = useSearchParams();
  const mp = params.get("mp") || "";
  const accountParam = params.get("account");
  const paramAccountId = accountParam && /^\d+$/.test(accountParam) ? Number(accountParam) : null;

  const automated = isAutomated(mp);
  const strategyName = mpDisplayName(mp);

  const [authChecked, setAuthChecked] = useState(false);
  const [isAuthed, setIsAuthed] = useState(false);
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [status, setStatus] = useState<SignalCopyStatus | null>(null);
  const [journey, setJourney] = useState<HostedJourney | null>(null);
  // AJ#7.2 — the hosted journey could not be loaded at all (endpoint unavailable / not entitled). Distinct
  // from "still preparing": here the workspace will NOT self-heal, so we must not promise auto-update.
  const [journeyUnavailable, setJourneyUnavailable] = useState(false);
  const [loading, setLoading] = useState(true);

  const [modalOpen, setModalOpen] = useState(false);
  const [enabling, setEnabling] = useState(false);
  const [enableError, setEnableError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);           // Get / Disable in-flight
  const [alert, setAlert] = useState<{ msg: string; type: "info" | "error" | "success" } | null>(null);

  // Auth
  useEffect(() => {
    (async () => {
      try {
        await apiFetch("/api/auth/me/", { method: "GET" });
        setIsAuthed(true);
      } catch {
        setIsAuthed(false);
      } finally {
        setAuthChecked(true);
      }
    })();
  }, []);

  const loadStatus = useCallback(async () => {
    if (!automated || !mp) return null;
    try {
      const st = await fetchSignalCopyStatus(mp);
      setStatus(st);
      return st;
    } catch {
      setStatus({ armed: false, enabled: false });
      return null;
    }
  }, [automated, mp]);

  // AJ#7.2 — while the customer waits for the workspace to become ready-to-enable, refresh readiness in place.
  // FAIL-SAFE: a transient fetch miss (null journey / thrown status) must NEVER downgrade a good UI — we only
  // apply a fresh non-null result, so the page moves forward to "Ready to enable" and never flickers backward.
  const pollReadiness = useCallback(async () => {
    if (!automated || !mp) return;
    const [jr, st] = await Promise.all([
      fetchJourney().then((r) => (r.ok ? r.journey : null)).catch(() => null),
      fetchSignalCopyStatus(mp).catch(() => null),
    ]);
    if (jr) { setJourney(jr); setJourneyUnavailable(false); }   // a fresh journey means it is reachable again
    if (st) setStatus(st);
  }, [automated, mp]);

  // Data load (accounts + signal-copy status + hosted journey)
  useEffect(() => {
    if (!authChecked) return;
    if (!isAuthed) { setLoading(false); return; }
    let cancelled = false;
    (async () => {
      setLoading(true);
      const accountsP = apiFetch<TradingAccount[]>("/api/trading/accounts/")
        .catch(() => apiFetch<TradingAccount[]>("/api/trading/trading-accounts/").catch(() => [] as TradingAccount[]));
      const statusP = automated ? fetchSignalCopyStatus(mp).catch(() => ({ armed: false, enabled: false } as SignalCopyStatus)) : Promise.resolve(null);
      // Keep the full load result so we can tell "still preparing" (ok, not-yet-ready) apart from
      // "unavailable" (endpoint failed / not entitled) — the latter must never promise self-healing.
      const journeyP = automated
        ? fetchJourney().catch(() => ({ ok: false as const, unavailable: true }))
        : Promise.resolve(null);
      const [accs, st, jl] = await Promise.all([accountsP, statusP, journeyP]);
      if (cancelled) return;
      setAccounts(accs || []);
      setStatus(st);
      setJourney(jl && "ok" in jl && jl.ok ? jl.journey : null);
      setJourneyUnavailable(!!(jl && "ok" in jl && !jl.ok));
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [authChecked, isAuthed, automated, mp]);

  // Resolve the account this configuration is for: the query param, else the owned assignment's account.
  const resolvedAccountId = paramAccountId ?? (status?.account_id ?? null);
  const account = useMemo(
    () => accounts.find((a) => a.id === resolvedAccountId),
    [accounts, resolvedAccountId],
  );
  const accountLabel = accountLabelOf(account, resolvedAccountId);

  const owned = !!status?.armed;
  const enabled = !!status?.enabled;
  const ambiguous = !!status?.ambiguous;
  // The workspace-authorization gate (ADR-0047): Enable can proceed only when the workspace is EXECUTION_READY
  // (already authorized, or ready to be authorized on the explicit confirm). Otherwise Enable degrades to a
  // "getting ready" state — never an error, never a bounce.
  const canEnable = !!journey && (journey.execution_authorized === true || journey.can_enable_automated_trading === true);
  // A workspace that will NOT become ready on its own: the journey couldn't load (not entitled / endpoint
  // down) OR it reports the terminal error phase. We must NOT show the auto-updating "getting ready" panel
  // here — that would be a false promise — so we surface an honest "needs attention" + support route instead.
  const workspaceUnavailable = journeyUnavailable || journey?.phase === "WORKSPACE_UNAVAILABLE";

  // Poll ONLY while the customer is in the "getting ready" state (owned, not enabled, not yet enable-able) AND
  // the workspace is actually progressing (not unavailable). Stops the moment it becomes Ready to enable
  // (canEnable), gets enabled, goes unavailable, or the customer leaves — transitioning in place, no bounce.
  const gettingReady = owned && !enabled && !canEnable && !ambiguous && !workspaceUnavailable;
  useEffect(() => {
    if (!isAuthed || !automated || !gettingReady) return;
    const id = setInterval(() => { void pollReadiness(); }, 5000);
    return () => clearInterval(id);
  }, [isAuthed, automated, gettingReady, pollReadiness]);

  const doEnableConfirm = async () => {
    if (!resolvedAccountId) return;
    setEnabling(true);
    setEnableError(null);
    const res = await enableStrategy(mp, resolvedAccountId);
    setEnabling(false);
    if (res.ok) {
      setModalOpen(false);
      router.push("/strategies?enabled=1");
      return;
    }
    // Partial/failed: keep the modal open, show the retryable message, backend state stays truthful.
    setEnableError(res.message);
  };

  const doGet = async () => {
    if (!resolvedAccountId) return;
    setBusy(true);
    try {
      await getStrategy(mp, resolvedAccountId);
      await loadStatus();
      setAlert({ msg: "Strategy added. You can enable it below.", type: "success" });
    } catch (e) {
      const err = e as { httpStatus?: number; body?: { status?: string } };
      const slug = err?.body?.status;
      setAlert({
        msg: slug === "account_not_ready" ? "This account must be a demo account and active."
          : slug === "not_pilot_approved" ? "This strategy isn't available for your account yet. Please contact support."
          : "We couldn't add this strategy just now. Please try again.",
        type: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const doDisable = async () => {
    if (!resolvedAccountId) return;
    setBusy(true);
    try {
      await disableStrategy(mp, resolvedAccountId);
      await loadStatus();
      setAlert({ msg: "Automated trading paused for this account.", type: "info" });
    } catch {
      setAlert({ msg: "We couldn't pause the strategy just now. Please try again.", type: "error" });
    } finally {
      setBusy(false);
    }
  };

  // ── Guards ──
  if (!mp) {
    return (
      <Shell>
        <div style={cardStyle}>
          <h1 style={{ fontSize: "1.4rem", margin: "0 0 0.5rem" }}>Choose a strategy</h1>
          <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>
            Pick a strategy from the marketplace to configure it.
          </p>
          <div style={{ marginTop: "1rem" }}>
            <Link href="/strategies/marketplace"><Button variant="primary">Browse strategies</Button></Link>
          </div>
        </div>
      </Shell>
    );
  }

  if (authChecked && !isAuthed) {
    return (
      <Shell>
        <div style={cardStyle}>
          <h1 style={{ fontSize: "1.4rem", margin: "0 0 0.5rem" }}>{strategyName}</h1>
          <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>Please sign in to configure this strategy.</p>
          <div style={{ marginTop: "1rem" }}>
            <Link href="/login?reason=unauthenticated"><Button variant="primary">Go to sign in</Button></Link>
          </div>
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      {/* Header */}
      <div style={{ marginBottom: "1rem" }}>
        <Link href="/strategies/marketplace" style={{ fontSize: "0.8rem", color: "#7c9cff", textDecoration: "none" }}>
          ← Back to marketplace
        </Link>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap", marginTop: "0.4rem" }}>
          <h1 style={{ fontSize: "1.8rem", margin: 0 }}>Configure {strategyName}</h1>
          <span style={freeBadge}>{priceLabel(priceFor(mp))}</span>
        </div>
        <p style={{ fontSize: "0.85rem", color: "#94a3b8", margin: "0.4rem 0 0" }}>
          Review the settings for this strategy, then enable it when you&rsquo;re ready.
        </p>
      </div>

      {alert && (
        <div
          role={alert.type === "error" ? "alert" : undefined}
          style={{
            marginBottom: "1rem", padding: "0.7rem 0.9rem", borderRadius: 8, fontSize: "0.85rem",
            border: `1px solid ${alert.type === "error" ? "rgba(239,68,68,0.4)" : alert.type === "success" ? "rgba(34,197,94,0.4)" : "rgba(59,130,246,0.4)"}`,
            background: alert.type === "error" ? "rgba(239,68,68,0.1)" : alert.type === "success" ? "rgba(34,197,94,0.1)" : "rgba(59,130,246,0.1)",
            color: alert.type === "error" ? "#fca5a5" : alert.type === "success" ? "#86efac" : "#93c5fd",
          }}
        >
          {alert.msg}
        </div>
      )}

      {loading ? (
        <div style={cardStyle}><p style={{ color: "#94a3b8", margin: 0 }}>Loading…</p></div>
      ) : automated ? (
        <AutomatedConfig
          mp={mp}
          strategyName={strategyName}
          accountLabel={accountLabel}
          accountMissing={resolvedAccountId != null && !account}
          hasAccount={!!resolvedAccountId}
          owned={owned}
          enabled={enabled}
          ambiguous={ambiguous}
          canEnable={canEnable}
          workspaceUnavailable={workspaceUnavailable}
          busy={busy}
          onGet={doGet}
          onDisable={doDisable}
          onOpenEnable={() => { setEnableError(null); setModalOpen(true); }}
        />
      ) : (
        <GenericConfig strategyName={strategyName} accountLabel={account ? accountLabel : null} />
      )}

      {automated && (
        <EnableStrategyModal
          open={modalOpen}
          accountLabel={accountLabel}
          strategyName={strategyName}
          busy={enabling}
          error={enableError}
          onConfirm={doEnableConfirm}
          onCancel={() => { if (!enabling) { setModalOpen(false); setEnableError(null); } }}
        />
      )}
    </Shell>
  );
}

// ── Automated (signal-copy) configuration ──
function AutomatedConfig(props: {
  mp: string;
  strategyName: string;
  accountLabel: string;
  accountMissing: boolean;
  hasAccount: boolean;
  owned: boolean;
  enabled: boolean;
  ambiguous: boolean;
  canEnable: boolean;
  workspaceUnavailable: boolean;
  busy: boolean;
  onGet: () => void;
  onDisable: () => void;
  onOpenEnable: () => void;
}) {
  const rows: ConfigRow[] = configContract(props.mp, props.accountLabel);
  return (
    <div style={{ display: "grid", gap: "1rem" }}>
      {/* Honest contract */}
      <div style={cardStyle}>
        <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.75rem", color: "#e8f0ff" }}>Strategy settings</h2>
        <div style={{ display: "grid", gap: "0.1rem" }}>
          {rows.map((r) => (
            <div
              key={r.key}
              style={{
                display: "grid", gridTemplateColumns: "minmax(140px, 40%) 1fr", gap: "0.75rem",
                padding: "0.6rem 0", borderBottom: "1px solid rgba(255,255,255,0.06)",
              }}
            >
              <div style={{ color: "#8fa0b7", fontSize: "0.82rem" }}>{r.label}</div>
              <div>
                <div style={{ color: "#e2e8f0", fontSize: "0.86rem", fontWeight: 600 }}>
                  {r.value}
                  {r.kind === "managed" && (
                    <span style={{
                      marginLeft: 8, fontSize: "0.66rem", fontWeight: 700, color: "#94a3b8",
                      border: "1px solid rgba(148,163,184,0.35)", borderRadius: 999, padding: "0.05rem 0.4rem",
                    }}>
                      Managed
                    </span>
                  )}
                </div>
                {r.help && <div style={{ color: "#6d7a92", fontSize: "0.74rem", marginTop: 2 }}>{r.help}</div>}
              </div>
            </div>
          ))}
        </div>
        <p style={{ color: "#8fa0b7", fontSize: "0.78rem", lineHeight: 1.5, margin: "0.9rem 0 0" }}>
          {BETA_CONFIG_NOTE}
        </p>
      </div>

      {/* State-driven action */}
      <div style={cardStyle}>
        {props.ambiguous ? (
          <ActionPanel
            tone="attention"
            title="This strategy needs attention"
            body="Something about this strategy's setup needs a closer look. Please contact support and we'll sort it out."
            action={<Link href="/support"><Button variant="secondary">Contact support</Button></Link>}
          />
        ) : props.accountMissing ? (
          <ActionPanel
            tone="attention"
            title="We couldn't find that account"
            body="The account for this strategy isn't available. Choose an account from the marketplace to continue."
            action={<Link href="/strategies/marketplace"><Button variant="secondary">Back to marketplace</Button></Link>}
          />
        ) : !props.hasAccount ? (
          <ActionPanel
            tone="neutral"
            title="Choose an account"
            body="Pick the account you want to use this strategy on from the marketplace."
            action={<Link href="/strategies/marketplace"><Button variant="primary">Back to marketplace</Button></Link>}
          />
        ) : !props.owned ? (
          <ActionPanel
            tone="neutral"
            title="Add this strategy"
            body={`This strategy isn't added to ${props.accountLabel} yet. Add it to continue.`}
            action={<Button variant="primary" onClick={props.onGet} disabled={props.busy}>{props.busy ? "Adding…" : "Get Strategy"}</Button>}
          />
        ) : props.enabled ? (
          <ActionPanel
            tone="ready"
            title="Automated trading is enabled"
            body={`${props.strategyName} is running on ${props.accountLabel}. It will place trades automatically until you pause it.`}
            action={
              <div style={{ display: "flex", gap: "0.6rem", flexWrap: "wrap" }}>
                <Button variant="secondary" onClick={props.onDisable} disabled={props.busy}>
                  {props.busy ? "Working…" : "Disable Strategy"}
                </Button>
                <Link href="/strategies"><Button variant="primary">Go to My Strategies</Button></Link>
              </div>
            }
          />
        ) : props.canEnable ? (
          <ActionPanel
            tone="action"
            title="Ready to enable"
            body={`${props.strategyName} is added to ${props.accountLabel}. Enable it to let GuvFX trade this strategy automatically on your account.`}
            action={<Button variant="primary" onClick={props.onOpenEnable} disabled={props.busy}>Enable Strategy</Button>}
          />
        ) : props.workspaceUnavailable ? (
          /* AJ#7.2 (adversarial fix): the workspace will NOT become ready on its own (journey unavailable or a
             terminal error). Do NOT show the auto-updating "getting ready" panel — that promises a self-heal
             that never comes. Surface an honest attention state with a route to support. */
          <ActionPanel
            tone="attention"
            title="This strategy needs attention"
            body="We couldn't get your trading workspace ready for this strategy. This usually needs a hand from our team — please contact support and we'll sort it out."
            action={<Link href="/support"><Button variant="secondary">Contact support</Button></Link>}
          />
        ) : (
          <ActionPanel
            tone="neutral"
            title="Your workspace is getting ready"
            body="We're finishing setting up your trading workspace. If you haven't already, open your MetaTrader terminal and log in — you'll be able to enable this strategy as soon as it's connected and ready."
            /* AJ#7.1 (adversarial fix): the forward action opens the customer's MetaTrader terminal (a stable
               page). It must NOT link back to /onboarding/hosted, which at WORKSPACE_READY bounces to the
               marketplace → owned card → Configure, re-forming the AJ#6.5-class navigation loop. */
            action={<Link href="/trading/terminal-access"><Button variant="secondary">Open MetaTrader</Button></Link>}
            footnote="This page updates automatically — you don't need to refresh. As soon as your workspace is ready, the Enable button will appear here."
          />
        )}
      </div>
    </div>
  );
}

// ── Generic (research/template) configuration — honest: NEVER implies automated execution ──
function GenericConfig({ strategyName, accountLabel }: { strategyName: string; accountLabel: string | null }) {
  return (
    <div style={cardStyle}>
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", marginBottom: "0.6rem" }}>
        <h2 style={{ fontSize: "1.05rem", margin: 0, color: "#e8f0ff" }}>Research strategy</h2>
        <span style={{
          fontSize: "0.66rem", fontWeight: 700, color: "#94a3b8",
          border: "1px solid rgba(148,163,184,0.35)", borderRadius: 999, padding: "0.05rem 0.45rem",
        }}>
          Template
        </span>
      </div>
      <p style={{ color: "#c7d2e8", fontSize: "0.9rem", lineHeight: 1.55, margin: "0 0 0.5rem" }}>
        <strong>{strategyName}</strong> has been added to your strategies{accountLabel ? ` for ${accountLabel}` : ""}.
      </p>
      <p style={{ color: "#94a3b8", fontSize: "0.84rem", lineHeight: 1.55, margin: "0 0 1rem" }}>
        This is a research template. It does <strong>not</strong> place trades automatically — open it to
        review the rules, edit the settings and run a backtest. Automated trading is available on our
        signal-copy strategies.
      </p>
      <div style={{ display: "flex", gap: "0.6rem", flexWrap: "wrap" }}>
        <Link href="/strategies"><Button variant="primary">Go to My Strategies</Button></Link>
        <Link href="/strategies/marketplace"><Button variant="secondary">Browse more strategies</Button></Link>
      </div>
    </div>
  );
}

function ActionPanel({ tone, title, body, action, footnote }: {
  tone: "ready" | "action" | "attention" | "neutral";
  title: string; body: string; action: React.ReactNode; footnote?: string;
}) {
  const color = { ready: "#86efac", action: "#93c5fd", attention: "#fcd34d", neutral: "#c7d2e8" }[tone];
  return (
    <div>
      <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.4rem", color }}>{title}</h2>
      <p style={{ color: "#94a3b8", fontSize: "0.86rem", lineHeight: 1.55, margin: "0 0 1rem" }}>{body}</p>
      {action}
      {footnote && (
        <p style={{ color: "#6d7a92", fontSize: "0.74rem", margin: "0.8rem 0 0" }}>{footnote}</p>
      )}
    </div>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return <div style={{ maxWidth: 780, margin: "0 auto" }}>{children}</div>;
}

export default function ConfigureStrategyPage() {
  return (
    <Suspense fallback={<Shell><div style={cardStyle}><p style={{ color: "#94a3b8", margin: 0 }}>Loading…</p></div></Shell>}>
      <ConfigureInner />
    </Suspense>
  );
}
