"use client";

import React, { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

/** MAILBOX INGESTION NOTICE (pkt-149 / ADR-0113) — a truthful, additive, owner-facing banner on the Withdrawal
 * Intelligence page.
 *
 * It warns the member when one of THEIR OWN connected mailboxes is unhealthy — so a silent OAuth outage (the 7-day
 * Testing-mode grant expiry) no longer passes as "all current". It is strictly ADDITIVE: it never replaces or zeroes
 * the withdrawal cards, never shows a false "all current", and only ADDS a warning when there is POSITIVE evidence of
 * degradation. It derives purely from the EXISTING owner-scoped, already-masked `/mailboxes/status/` payload — no new
 * endpoint, no token/credential/cursor/full-address ever touched. DARK-safe: when the status API 404s (feature off) or
 * errors, it renders nothing (it never fabricates either a healthy or a degraded state). */
type Lang = "en" | "ja";

type MailboxLite = { state?: string; reconnect_required?: boolean };

export type IngestionNoticeLevel = "reconnect" | "degraded" | null;

/** PURE, unit-tested: the owner notice level from the mailbox states. `reconnect` wins over `degraded`; neither when
 * every mailbox is fine (or there are none). A mailbox needing re-consent is RECONNECT_REQUIRED/REVOKED (or the
 * explicit `reconnect_required` flag); a late/slow but not-reconnect mailbox is DEGRADED. */
export function deriveIngestionNoticeLevel(mailboxes: MailboxLite[] | undefined | null): IngestionNoticeLevel {
  if (!mailboxes || mailboxes.length === 0) return null;
  const anyReconnect = mailboxes.some(
    (m) => m.reconnect_required === true || m.state === "RECONNECT_REQUIRED" || m.state === "REVOKED",
  );
  if (anyReconnect) return "reconnect";
  if (mailboxes.some((m) => m.state === "DEGRADED")) return "degraded";
  return null;
}

const COPY: Record<"reconnect" | "degraded", Record<Lang, string>> = {
  reconnect: {
    en: "One or more connected email accounts needs reconnection. New withdrawal notifications may be delayed until you reconnect it below. Your existing withdrawal history is unaffected.",
    ja: "接続済みのメールアカウントの再接続が必要です。再接続するまで、新しい出金通知が遅れる場合があります。これまでの出金履歴には影響しません。",
  },
  degraded: {
    en: "Email ingestion is currently delayed. Your existing withdrawal history is unaffected, but new withdrawal notifications may be delayed until it recovers.",
    ja: "メールの取り込みが現在遅延しています。これまでの出金履歴には影響しませんが、復旧するまで新しい出金通知が遅れる場合があります。",
  },
};

type StatusResponse = { mailboxes?: MailboxLite[] };

const STATUS = "/api/broker-intelligence/mailboxes/status/";

export function MailboxIngestionNotice({ lang }: { lang: Lang }) {
  const [level, setLevel] = useState<IngestionNoticeLevel>(null);

  useEffect(() => {
    let alive = true;
    apiFetch<StatusResponse>(STATUS, {})
      .then((r) => {
        if (alive) setLevel(deriveIngestionNoticeLevel(r?.mailboxes));
      })
      .catch(() => {
        // 404 (feature dark) or any transient error → show nothing; never fabricate a healthy OR degraded state.
        if (alive) setLevel(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!level) return null;

  const amber = level === "degraded";
  const style: React.CSSProperties = {
    border: `1px solid ${amber ? "rgba(201,162,74,0.5)" : "rgba(232,160,160,0.5)"}`,
    background: amber ? "rgba(201,162,74,0.14)" : "rgba(232,160,160,0.12)",
    color: amber ? "#e7c77a" : "#e8a0a0",
    borderRadius: 14,
    padding: "0.85rem 1.1rem",
    fontSize: "0.86rem",
    lineHeight: 1.6,
    display: "flex",
    gap: "0.6rem",
    alignItems: "flex-start",
  };

  return (
    <div style={style} role="status" data-testid="wi-ingestion-notice" data-level={level}>
      <span aria-hidden style={{ fontSize: "1rem", lineHeight: 1.4 }}>⚠</span>
      <span>{COPY[level][lang]}</span>
    </div>
  );
}
