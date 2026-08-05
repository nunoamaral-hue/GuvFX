"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { notFound, useParams } from "next/navigation";
import {
  getAccount, getBrokerStatus, getValidationHistory, retryValidation, testConnection,
} from "@/lib/broker-api";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { StatusBadge } from "@/components/broker/StatusBadge";
import { ValidationHistoryTable } from "@/components/broker/ValidationHistoryTable";
import { ReplaceCredentialsDialog } from "@/components/broker/ReplaceCredentialsDialog";
import { DisconnectDialog } from "@/components/broker/DisconnectDialog";
import { Dialog } from "@/components/broker/Dialog";
import { ErrorState, LoadingState } from "@/components/broker/States";

/** Self-contained SVG spinner (SMIL animation — no global CSS keyframes needed). */
const Spinner: React.FC = () => (
  <svg width="30" height="30" viewBox="0 0 50 50" role="status" aria-label="Testing" style={{ flexShrink: 0 }}>
    <circle cx="25" cy="25" r="20" fill="none" stroke="rgba(147,197,253,0.2)" strokeWidth="5" />
    <path d="M25 5 A20 20 0 0 1 45 25" fill="none" stroke="#93c5fd" strokeWidth="5" strokeLinecap="round">
      <animateTransform attributeName="transform" type="rotate" dur="0.9s" from="0 25 25" to="360 25 25" repeatCount="indefinite" />
    </path>
  </svg>
);
import {
  connectionView, lastValidatedLine, maskAccountNumber, reasonMessage, toCustomerError,
  validationStatusView,
} from "@/lib/broker-status";
import type { BrokerAccount, BrokerStatus, ValidationAttempt } from "@/types/broker";

/** WP4.2 broker-account DETAIL body (validation history + Test / Retry / Replace / Disconnect), extracted
 * so the canonical customer route (/accounts/[id], packet WS-A) and the legacy /broker-accounts/[id]
 * redirect share one implementation. The caller owns the flag gate. Test/Retry run inline; Replace/
 * Disconnect use confirmation dialogs. */
export function BrokerAccountDetailContent() {
  const params = useParams();
  const id = Number(Array.isArray(params?.id) ? params.id[0] : params?.id);

  const [account, setAccount] = useState<BrokerAccount | null>(null);
  const [status, setStatus] = useState<BrokerStatus | null>(null);
  const [history, setHistory] = useState<ValidationAttempt[] | null>(null);
  const [error, setError] = useState("");
  // Validation runs in a modal (never an inline hang): opens immediately with a spinner, then shows a
  // success/failure result. Every failure path is customer-safe (no raw "Failed to fetch"/exception text).
  type ValModal =
    | { open: false }
    | { open: true; phase: "running"; kind: "test" | "retry" }
    | { open: true; phase: "done"; kind: "test" | "retry"; ok: boolean; text: string };
  const [valModal, setValModal] = useState<ValModal>({ open: false });
  const running = valModal.open && valModal.phase === "running";
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [disconnectOpen, setDisconnectOpen] = useState(false);

  const load = useCallback(async () => {
    setError(""); setAccount(null);
    try {
      const [acct, st, hist] = await Promise.all([
        getAccount(id),
        getBrokerStatus(id).catch(() => null),
        getValidationHistory(id).catch(() => []),
      ]);
      setAccount(acct); setStatus(st); setHistory(hist);
    } catch (err) {
      setError(toCustomerError(err, "We couldn't load this account."));
    }
  }, [id]);

  // Standard load-on-mount/reload: load() resets to a loading state then fetches (intended data-fetch pattern).
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { if (Number.isFinite(id)) void load(); }, [id, load]);

  const runValidation = async (kind: "test" | "retry") => {
    setValModal({ open: true, phase: "running", kind });   // modal opens immediately with the spinner
    try {
      const attempt = kind === "test" ? await testConnection(id) : await retryValidation(id);
      // refresh the durable status + history so the page reflects the persisted attempt
      const [st, hist] = await Promise.all([getBrokerStatus(id).catch(() => null), getValidationHistory(id).catch(() => history || [])]);
      setStatus(st); setHistory(hist);
      const ok = attempt.status === "HEALTHY";
      setValModal({ open: true, phase: "done", kind, ok,
        text: ok ? "Your broker connection is verified." : (reasonMessage(attempt.reason_code) || "We couldn't verify the connection. Please try again shortly.") });
    } catch (err) {
      // Any transport/timeout/exception path — customer-safe only (toCustomerError never leaks raw errors).
      setValModal({ open: true, phase: "done", kind, ok: false,
        text: toCustomerError(err, "We couldn't complete the connection check. Please try again shortly.") });
    }
  };

  if (!Number.isFinite(id)) notFound();
  if (error) return <div style={{ maxWidth: 860, margin: "0 auto", padding: "1.5rem 1rem" }}><ErrorState message={error} onRetry={() => void load()} /></div>;
  if (account === null) return <div style={{ maxWidth: 860, margin: "0 auto", padding: "1.5rem 1rem" }}><LoadingState label="Loading account…" /></div>;

  const broker = account.broker_display_name || account.broker_name || "Broker";
  const label = account.name || broker;
  const disconnected = !!status?.disconnected_at;

  return (
    <div style={{ maxWidth: 860, margin: "0 auto", padding: "1.5rem 1rem" }}>
      <Link href="/accounts" style={{ color: "#93c5fd", fontSize: "0.85rem", textDecoration: "none" }}>← Broker accounts</Link>
      <h1 style={{ margin: "8px 0 4px", fontSize: "1.5rem", color: "#e9f4ff" }}>{label}</h1>
      <div style={{ color: "#8fa0b7", fontSize: "0.9rem", marginBottom: 16 }}>
        {broker}{account.server_name ? ` · ${account.server_name}` : ""} · {maskAccountNumber(account.account_number)}
      </div>

      <Card title="Status">
        {/* WS-G — two clearly-labelled, non-overlapping concepts. The former layout showed three unlabelled
            badges where "validation status" and "broker health" both derived from the same latest attempt
            and read as the same thing (e.g. "Temporarily unavailable" + "Unavailable"); the redundant
            health badge is removed and each remaining concept now carries its own label. */}
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ minWidth: 140, color: "#8fa0b7", fontSize: "0.8rem" }}>Broker connection</span>
            <StatusBadge view={validationStatusView(status?.validation_status)} title="Broker connection" />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ minWidth: 140, color: "#8fa0b7", fontSize: "0.8rem" }}>Trading account</span>
            <StatusBadge view={connectionView(status ? status.is_active : account.is_active, status?.disconnected_at)} title="Trading account" />
          </div>
        </div>
        <div style={{ color: "#8fa0b7", fontSize: "0.85rem" }}>
          {/* "Never validated" was shown even after several failed attempts (packet WS-G). lastValidatedLine
              distinguishes a real prior success from "no successful validation yet" AND cross-checks the
              timestamp against validation_status so it can't contradict the badge (e.g. after disconnect). */}
          {lastValidatedLine(status?.validation_status, status?.validated_at)}
        </div>
        {!disconnected && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 14 }}>
            <Button onClick={() => void runValidation("test")} disabled={running}>Test connection</Button>
            <Button variant="secondary" onClick={() => void runValidation("retry")} disabled={running}>Retry validation</Button>
            <Button variant="secondary" onClick={() => setReplaceOpen(true)} disabled={running}>Replace credentials</Button>
            <Button variant="secondary" onClick={() => setDisconnectOpen(true)} disabled={running}>Disconnect</Button>
          </div>
        )}
        {disconnected && <div style={{ marginTop: 14 }}><Alert type="info">This account is disconnected. Its history is preserved below.</Alert></div>}
      </Card>

      <div style={{ marginTop: 18 }}>
        <Card title="Validation history">
          {history === null ? <LoadingState label="Loading history…" /> : <ValidationHistoryTable attempts={history} />}
        </Card>
      </div>

      {/* Validation runs in a modal so the page never appears to hang; every result path is customer-safe. */}
      <Dialog
        open={valModal.open}
        busy={running}
        title={valModal.open && valModal.kind === "retry" ? "Retry validation" : "Test connection"}
        onClose={() => setValModal({ open: false })}
      >
        {valModal.open && valModal.phase === "running" && (
          <div style={{ display: "flex", alignItems: "center", gap: 14, padding: "0.35rem 0 0.6rem" }}>
            <Spinner />
            <div style={{ color: "#cbd5f5", fontSize: "0.92rem" }}>
              Testing your broker connection… This can take up to two minutes. Please keep this window open.
            </div>
          </div>
        )}
        {valModal.open && valModal.phase === "done" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
              <span aria-hidden style={{ fontSize: "1.3rem", lineHeight: 1.2, color: valModal.ok ? "#22c55e" : "#f59e0b" }}>{valModal.ok ? "✓" : "○"}</span>
              <div style={{ color: "#e2e8f0", fontSize: "0.95rem" }}>{valModal.text}</div>
            </div>
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <Button onClick={() => setValModal({ open: false })}>Dismiss</Button>
            </div>
          </div>
        )}
      </Dialog>

      <ReplaceCredentialsDialog open={replaceOpen} accountId={id} onClose={() => setReplaceOpen(false)} onReplaced={() => void load()} />
      <DisconnectDialog open={disconnectOpen} accountId={id} accountLabel={label} onClose={() => setDisconnectOpen(false)} onDisconnected={() => void load()} />
    </div>
  );
}
