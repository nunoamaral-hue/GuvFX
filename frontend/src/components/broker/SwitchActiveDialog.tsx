"use client";

import React from "react";
import { Dialog } from "@/components/broker/Dialog";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import type { BrokerAccount } from "@/types/broker";
import { accountTitle } from "@/lib/broker-status";

/** Phase C4 (DARK) — STANDARD-mode "activate this account" confirmation.
 *
 * In STANDARD mode only ONE broker account can trade at a time, so activating a different account STOPS the
 * one that is currently active. This modal makes that consequence explicit and requires a deliberate confirm
 * before the switch. It is a customer-facing UX + API contract ONLY: the actual one-active-per-user switch is
 * enforced on the backend solely when concurrent enforcement is armed (a later gate). While DARK the confirm
 * still performs a safe activate; it never arms execution. */
type Props = {
  open: boolean;
  target: BrokerAccount | null;
  currentActive: BrokerAccount | null;
  busy?: boolean;
  error?: string;
  onConfirm: () => void;
  onClose: () => void;
};

function label(a: BrokerAccount | null): string {
  if (!a) return "your current account";
  return accountTitle(a) || `Account ${a.id}`;
}

export const SwitchActiveDialog: React.FC<Props> = ({
  open, target, currentActive, busy = false, error, onConfirm, onClose,
}) => {
  const close = () => { if (!busy) onClose(); };
  return (
    <Dialog open={open} onClose={close} title="Switch trading account" busy={busy}>
      <Alert type="info">
        Only one account can trade at a time on your plan.
      </Alert>
      <p style={{ color: "#cbd5f5", fontSize: "0.9rem", lineHeight: 1.6, margin: "12px 0 0" }}>
        Starting trading on <strong>{label(target)}</strong> will stop{" "}
        <strong>{label(currentActive)}</strong> from trading. Your other account stays connected — it just
        won&apos;t place new trades until you switch back.
      </p>
      {error && <div style={{ marginTop: 12 }}><Alert type="error">{error}</Alert></div>}
      <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end", gap: 8 }}>
        <Button type="button" variant="secondary" onClick={close} disabled={busy}>Cancel</Button>
        <Button type="button" onClick={onConfirm} disabled={busy}>
          {busy ? "Starting…" : `Start trading on ${label(target)}`}
        </Button>
      </div>
    </Dialog>
  );
};
