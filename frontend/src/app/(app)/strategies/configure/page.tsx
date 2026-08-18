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
import { useLang } from "@/components/AppShell";
import { t, type Lang } from "@/lib/i18n";

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
  const lang = useLang();
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
  // AJ#7.2 — the signal-copy status fetch failed (threw), so we can't yet tell whether the customer owns this
  // product. Distinct from a real "not owned" (armed:false) result: on failure we must NOT show "Get Strategy"
  // (they may already own it) — we show a "checking" state and let the poll retry and self-heal.
  const [statusUnavailable, setStatusUnavailable] = useState(false);
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
      setStatus(st); setStatusUnavailable(false);
      return st;
    } catch {
      // A thrown status fetch is a TRANSIENT failure — do NOT assert "not owned" (which would offer
      // "Get Strategy" for a product they may already own); mark it unavailable so the poll retries.
      setStatusUnavailable(true);
      return null;
    }
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
      // Track a THROWN status fetch as unavailable (transient) rather than collapsing it to "not owned".
      const statusP = automated
        ? fetchSignalCopyStatus(mp).then((st) => ({ st, failed: false })).catch(() => ({ st: null as SignalCopyStatus | null, failed: true }))
        : Promise.resolve({ st: null as SignalCopyStatus | null, failed: false });
      // Distinguish "genuinely unavailable" from "transient": fetchJourney returns {ok:false} ONLY for a 404
      // (feature dark / not entitled) and RE-THROWS 5xx/network so callers can retry. So a 404 → sticky
      // "needs attention", but a thrown (transient) error → journey null + NOT unavailable, i.e. the
      // getting-ready state that the poll retries — never a sticky dead-end on a momentary blip.
      const journeyP = automated
        ? fetchJourney()
            .then((r) => (r.ok ? { journey: r.journey, unavailable: false } : { journey: null, unavailable: true }))
            .catch(() => ({ journey: null, unavailable: false }))
        : Promise.resolve({ journey: null, unavailable: false });
      const [accs, sl, jl] = await Promise.all([accountsP, statusP, journeyP]);
      if (cancelled) return;
      setAccounts(accs || []);
      setStatus(sl.st);
      setStatusUnavailable(sl.failed);
      setJourney(jl.journey);
      setJourneyUnavailable(jl.unavailable);
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
  // Is the workspace advancing toward enable-able ON ITS OWN (no customer action needed)? Two autonomous cases:
  //  • next_action "wait" — still preparing the workspace.
  //  • phase WORKSPACE_READY — onboarding is COMPLETE (next_action becomes "assign_strategy"), but ADR-0047
  //    execution-authorization (canEnable) rides a strictly-higher, host-observed EXECUTION_READY tier that
  //    lags WORKSPACE_READY (AJ#3 decouple). That warm-up window is NOT a customer step — polling advances it.
  // ONLY these are honestly "getting ready"; other not-ready phases (NO_WORKSPACE, AWAITING_BROKER_LOGIN,
  // ACCOUNT_CONFIRMATION_REQUIRED …) need the customer to complete a step, so we send them to finish setup.
  // CRITICAL: WORKSPACE_READY must be excluded from needsSetup — routing it to /onboarding/hosted re-forms the
  // AJ#6.5/7.1 loop (onboarding at WORKSPACE_READY → "Choose Strategy" → marketplace → Configure → …).
  const journeyProgressing = journey?.next_action === "wait" || journey?.phase === "WORKSPACE_READY";
  const needsSetup = !!journey && !journeyProgressing && !canEnable && !workspaceUnavailable;

  // Poll ONLY while the customer is in the "getting ready" state: owned, has a resolvable account, not enabled,
  // not yet enable-able, not ambiguous, and the workspace is actually progressing (not unavailable). Stops the
  // moment it becomes Ready to enable, gets enabled, goes unavailable, or the customer leaves — in place.
  const gettingReady = owned && !!account && !enabled && !canEnable && !ambiguous && !workspaceUnavailable;
  useEffect(() => {
    // Poll while getting-ready OR while the status is unavailable (transient fetch failure) — the latter so an
    // owned product misread as "not owned" self-heals instead of stranding the customer on "Get Strategy".
    if (!isAuthed || !automated || !mp || (!gettingReady && !statusUnavailable)) return;
    // SINGLE-FLIGHT: schedule the next refresh only AFTER the current one resolves (recursive setTimeout, not
    // setInterval) so two polls can never overlap and an out-of-order response can never revert a fresh state.
    // FAIL-SAFE: only a fresh non-null result is applied, so a transient miss never downgrades a good UI.
    // CANCELLABLE: an in-flight response is dropped once the effect tears down (ready / unmounted).
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const [jr, st] = await Promise.all([
        fetchJourney().then((r) => (r.ok ? r.journey : null)).catch(() => null),
        fetchSignalCopyStatus(mp).catch(() => null),
      ]);
      if (cancelled) return;
      if (jr) { setJourney(jr); setJourneyUnavailable(false); }   // a fresh journey means it is reachable again
      if (st) { setStatus(st); setStatusUnavailable(false); }     // a fresh status means it is reachable again
      if (!cancelled) timer = setTimeout(tick, 5000);
    };
    timer = setTimeout(tick, 5000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [isAuthed, automated, mp, gettingReady, statusUnavailable]);

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
    const preparingCodes = new Set([
      "runtime_not_ready", "broker_not_connected", "workspace_execution_not_authorized",
      "workspace_execution_disabled",
    ]);
    setEnableError(t(lang, res.code && preparingCodes.has(res.code)
      ? "configure.enablePreparing"
      : "configure.enableError"));
  };

  const doGet = async () => {
    if (!resolvedAccountId) return;
    setBusy(true);
    try {
      await getStrategy(mp, resolvedAccountId);
      await loadStatus();
      setAlert({ msg: t(lang, "configure.addSuccess"), type: "success" });
    } catch (e) {
      const err = e as { httpStatus?: number; body?: { status?: string } };
      const slug = err?.body?.status;
      setAlert({
        msg: slug === "account_not_ready" ? t(lang, "configure.addAccountError")
          : slug === "not_pilot_approved" ? t(lang, "configure.addUnavailable")
          : t(lang, "configure.addError"),
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
      setAlert({ msg: t(lang, "configure.pauseSuccess"), type: "info" });
    } catch {
      setAlert({ msg: t(lang, "configure.pauseError"), type: "error" });
    } finally {
      setBusy(false);
    }
  };

  // ── Guards ──
  if (!mp) {
    return (
      <Shell>
        <div style={cardStyle}>
          <h1 style={{ fontSize: "1.4rem", margin: "0 0 0.5rem" }}>{t(lang, "configure.chooseTitle")}</h1>
          <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>
            {t(lang, "configure.chooseBody")}
          </p>
          <div style={{ marginTop: "1rem" }}>
            <Link href="/strategies/marketplace"><Button variant="primary">{t(lang, "configure.browse")}</Button></Link>
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
          <p style={{ color: "#94a3b8", fontSize: "0.9rem" }}>{t(lang, "configure.signIn")}</p>
          <div style={{ marginTop: "1rem" }}>
            <Link href="/login?reason=unauthenticated"><Button variant="primary">{t(lang, "configure.goSignIn")}</Button></Link>
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
          {t(lang, "configure.backMarketplace")}
        </Link>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap", marginTop: "0.4rem" }}>
          <h1 style={{ fontSize: "1.8rem", margin: 0 }}>{t(lang, "configure.title", { strategy: strategyName })}</h1>
          <span style={freeBadge}>{priceFor(mp).kind === "free" ? t(lang, "configure.free") : priceLabel(priceFor(mp))}</span>
        </div>
        <p style={{ fontSize: "0.85rem", color: "#94a3b8", margin: "0.4rem 0 0" }}>
          {t(lang, "configure.subtitle")}
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
        <div style={cardStyle}><p style={{ color: "#94a3b8", margin: 0 }}>{t(lang, "configure.loading")}</p></div>
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
          preparing={journeyProgressing}
          needsSetup={needsSetup}
          statusUnavailable={statusUnavailable}
          busy={busy}
          onGet={doGet}
          onDisable={doDisable}
          onOpenEnable={() => { setEnableError(null); setModalOpen(true); }}
          lang={lang}
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
  preparing: boolean;
  needsSetup: boolean;
  statusUnavailable: boolean;
  busy: boolean;
  onGet: () => void;
  onDisable: () => void;
  onOpenEnable: () => void;
  lang: Lang;
}) {
  const rows: ConfigRow[] = configContract(props.mp, props.accountLabel);
  const localizeRow = (row: ConfigRow) => ({
    label: t(props.lang, `configure.row.${row.key}.label`),
    value: row.key === "execution" || row.kind === "managed"
      ? t(props.lang, `configure.row.${row.key}.value`)
      : row.value,
    help: row.help ? t(props.lang, `configure.row.${row.key}.help`) : undefined,
  });
  return (
    <div style={{ display: "grid", gap: "1rem" }}>
      {/* Honest contract */}
      <div style={cardStyle}>
        <h2 style={{ fontSize: "1.05rem", margin: "0 0 0.75rem", color: "#e8f0ff" }}>{t(props.lang, "configure.settings")}</h2>
        <div style={{ display: "grid", gap: "0.1rem" }}>
          {rows.map((r) => {
            const copy = localizeRow(r);
            return (
            <div
              key={r.key}
              style={{
                display: "grid", gridTemplateColumns: "minmax(140px, 40%) 1fr", gap: "0.75rem",
                padding: "0.6rem 0", borderBottom: "1px solid rgba(255,255,255,0.06)",
              }}
            >
              <div style={{ color: "#8fa0b7", fontSize: "0.82rem" }}>{copy.label}</div>
              <div>
                <div style={{ color: "#e2e8f0", fontSize: "0.86rem", fontWeight: 600 }}>
                  {copy.value}
                  {r.kind === "managed" && (
                    <span style={{
                      marginLeft: 8, fontSize: "0.66rem", fontWeight: 700, color: "#94a3b8",
                      border: "1px solid rgba(148,163,184,0.35)", borderRadius: 999, padding: "0.05rem 0.4rem",
                    }}>
                      {t(props.lang, "configure.managed")}
                    </span>
                  )}
                </div>
                {copy.help && <div style={{ color: "#6d7a92", fontSize: "0.74rem", marginTop: 2 }}>{copy.help}</div>}
              </div>
            </div>
          )})}
        </div>
        <p style={{ color: "#8fa0b7", fontSize: "0.78rem", lineHeight: 1.5, margin: "0.9rem 0 0" }}>
          {props.lang === "ja" ? t(props.lang, "configure.betaNote") : BETA_CONFIG_NOTE}
        </p>
      </div>

      {/* State-driven action */}
      <div style={cardStyle}>
        {props.ambiguous ? (
          <ActionPanel
            tone="attention"
            title={t(props.lang, "configure.attentionTitle")}
            body={t(props.lang, "configure.attentionBody")}
            action={<Link href="/support"><Button variant="secondary">{t(props.lang, "configure.contactSupport")}</Button></Link>}
          />
        ) : props.statusUnavailable ? (
          /* AJ#7.2 (adversarial fix): checked BEFORE the account branches. The owner path reaches Configure with
             NO account param and relies on status.account_id to resolve the account — so a failed status fetch
             leaves resolvedAccountId null and would otherwise fall into "Choose an account", masking this
             checking state. The poll retries and self-heals into the correct panel. */
          <ActionPanel
            tone="neutral"
            title={t(props.lang, "configure.checkingTitle")}
            body={t(props.lang, "configure.checkingBody")}
            action={<Link href="/strategies"><Button variant="secondary">{t(props.lang, "configure.goMyStrategies")}</Button></Link>}
          />
        ) : props.accountMissing ? (
          <ActionPanel
            tone="attention"
            title={t(props.lang, "configure.accountMissingTitle")}
            body={t(props.lang, "configure.accountMissingBody")}
            action={<Link href="/strategies/marketplace"><Button variant="secondary">{t(props.lang, "configure.backMarketplace").replace("← ", "")}</Button></Link>}
          />
        ) : !props.hasAccount ? (
          <ActionPanel
            tone="neutral"
            title={t(props.lang, "configure.chooseAccount")}
            body={t(props.lang, "configure.chooseAccountBody")}
            action={<Link href="/strategies/marketplace"><Button variant="primary">{t(props.lang, "configure.backMarketplace").replace("← ", "")}</Button></Link>}
          />
        ) : !props.owned ? (
          <ActionPanel
            tone="neutral"
            title={t(props.lang, "configure.addTitle")}
            body={t(props.lang, "configure.addBody", { account: props.accountLabel })}
            action={<Button variant="primary" onClick={props.onGet} disabled={props.busy}>{props.busy ? t(props.lang, "configure.adding") : t(props.lang, "configure.getStrategy")}</Button>}
          />
        ) : props.enabled ? (
          <ActionPanel
            tone="ready"
            title={t(props.lang, "configure.enabledTitle")}
            body={t(props.lang, "configure.enabledBody", { strategy: props.strategyName, account: props.accountLabel })}
            action={
              <div style={{ display: "flex", gap: "0.6rem", flexWrap: "wrap" }}>
                <Button variant="secondary" onClick={props.onDisable} disabled={props.busy}>
                  {props.busy ? t(props.lang, "configure.working") : t(props.lang, "configure.disable")}
                </Button>
                <Link href="/strategies"><Button variant="primary">{t(props.lang, "configure.goMyStrategies")}</Button></Link>
              </div>
            }
          />
        ) : props.canEnable ? (
          <ActionPanel
            tone="action"
            title={t(props.lang, "configure.readyTitle")}
            body={t(props.lang, "configure.readyBody", { strategy: props.strategyName, account: props.accountLabel })}
            action={<Button variant="primary" onClick={props.onOpenEnable} disabled={props.busy}>{t(props.lang, "enableModal.confirm")}</Button>}
          />
        ) : props.workspaceUnavailable ? (
          /* AJ#7.2 (adversarial fix): the workspace will NOT become ready on its own (journey unavailable or a
             terminal error). Do NOT show the auto-updating "getting ready" panel — that promises a self-heal
             that never comes. Surface an honest attention state with a route to support. */
          <ActionPanel
            tone="attention"
            title={t(props.lang, "configure.attentionTitle")}
            body={t(props.lang, "configure.attentionBody")}
            action={<Link href="/support"><Button variant="secondary">{t(props.lang, "configure.contactSupport")}</Button></Link>}
          />
        ) : props.needsSetup ? (
          /* AJ#7.2 (adversarial fix): the workspace is at a phase that needs the CUSTOMER to complete a step
             (request the workspace, log in, confirm the account). Polling can't advance it, so we must NOT
             promise auto-update — we send them to finish setup. Safe from the AJ#6.5/7.1 loop: onboarding only
             redirects at WORKSPACE_READY, which is the canEnable branch above, never a needs-setup phase. */
          <ActionPanel
            tone="action"
            title={t(props.lang, "configure.finishTitle")}
            body={t(props.lang, "configure.finishBody")}
            action={<Link href="/onboarding/hosted"><Button variant="primary">{t(props.lang, "hostedStatus.continueSetup")}</Button></Link>}
          />
        ) : (
          <ActionPanel
            tone="neutral"
            title={t(props.lang, "configure.gettingReadyTitle")}
            body={t(props.lang, "configure.gettingReadyBody")}
            /* AJ#7.1 (adversarial fix): the forward action opens the customer's MetaTrader terminal (a stable
               page). It must NOT link back to /onboarding/hosted, which at WORKSPACE_READY bounces to the
               marketplace → owned card → Configure, re-forming the AJ#6.5-class navigation loop. */
            action={<Link href="/trading/terminal-access"><Button variant="secondary">{t(props.lang, "hostedStatus.openMetaTrader")}</Button></Link>}
            /* AJ#7.2 (adversarial fix): only promise auto-update when the workspace is genuinely preparing on
               its own (next_action "wait"). When journey is still loading/transient (not preparing), we omit
               the footnote so we never promise a self-heal we can't guarantee. */
            footnote={props.preparing
              ? t(props.lang, "configure.autoUpdate")
              : undefined}
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
