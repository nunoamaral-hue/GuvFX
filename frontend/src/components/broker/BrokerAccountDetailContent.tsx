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
import { ErrorState, LoadingState } from "@/components/broker/States";
import {
  connectionView, formatWhen, maskAccountNumber, reasonMessage, toCustomerError,
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
  const [busy, setBusy] = useState<"test" | "retry" | null>(null);
  const [actionMsg, setActionMsg] = useState<{ ok: boolean; text: string } | null>(null);
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

  useEffect(() => { if (Number.isFinite(id)) void load(); }, [id, load]);

  const runValidation = async (kind: "test" | "retry") => {
    setBusy(kind); setActionMsg(null);
    try {
      const attempt = kind === "test" ? await testConnection(id) : await retryValidation(id);
      setActionMsg({ ok: attempt.status === "HEALTHY", text: reasonMessage(attempt.reason_code) || "Validation complete." });
      const [st, hist] = await Promise.all([getBrokerStatus(id).catch(() => null), getValidationHistory(id).catch(() => history || [])]);
      setStatus(st); setHistory(hist);
    } catch (err) {
      setActionMsg({ ok: false, text: toCustomerError(err, "Validation failed. Please try again.") });
    } finally {
      setBusy(null);
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
          {/* "Never validated" was shown even after several failed attempts (packet WS-G). Distinguish
              "no successful validation yet" from a real prior success. */}
          {formatWhen(status?.validated_at) ? `Last validated ${formatWhen(status?.validated_at)}` : "No successful validation yet"}
        </div>
        {actionMsg && <div style={{ marginTop: 12 }}><Alert type={actionMsg.ok ? "success" : "error"}>{actionMsg.text}</Alert></div>}
        {!disconnected && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 14 }}>
            <Button onClick={() => void runValidation("test")} disabled={busy !== null}>{busy === "test" ? "Testing…" : "Test connection"}</Button>
            <Button variant="secondary" onClick={() => void runValidation("retry")} disabled={busy !== null}>{busy === "retry" ? "Retrying…" : "Retry validation"}</Button>
            <Button variant="secondary" onClick={() => setReplaceOpen(true)} disabled={busy !== null}>Replace credentials</Button>
            <Button variant="secondary" onClick={() => setDisconnectOpen(true)} disabled={busy !== null}>Disconnect</Button>
          </div>
        )}
        {disconnected && <div style={{ marginTop: 14 }}><Alert type="info">This account is disconnected. Its history is preserved below.</Alert></div>}
      </Card>

      <div style={{ marginTop: 18 }}>
        <Card title="Validation history">
          {history === null ? <LoadingState label="Loading history…" /> : <ValidationHistoryTable attempts={history} />}
        </Card>
      </div>

      <ReplaceCredentialsDialog open={replaceOpen} accountId={id} onClose={() => setReplaceOpen(false)} onReplaced={() => void load()} />
      <DisconnectDialog open={disconnectOpen} accountId={id} accountLabel={label} onClose={() => setDisconnectOpen(false)} onDisconnected={() => void load()} />
    </div>
  );
}
