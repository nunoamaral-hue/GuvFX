"use client";

import React, { useState } from "react";
import { Dialog } from "@/components/broker/Dialog";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { disconnectAccount } from "@/lib/broker-api";

/** WP4.2 — disconnect (tombstone) a broker account. Explains the effect clearly and requires an explicit
 * confirmation. Credentials are destroyed and the runtime disconnected, but trade/validation history is
 * preserved (the backend never row-deletes). */
type Props = { open: boolean; accountId: number; accountLabel: string; onClose: () => void; onDisconnected: () => void };

export const DisconnectDialog: React.FC<Props> = ({ open, accountId, accountLabel, onClose, onDisconnected }) => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const close = () => { if (!busy) { setError(""); onClose(); } };

  const confirm = async () => {
    setBusy(true); setError("");
    try {
      await disconnectAccount(accountId);
      onDisconnected();
      close();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We couldn't disconnect the account. Please try again.");
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={close} title="Disconnect account" busy={busy}>
      <Alert type="error">This disconnects <strong>{accountLabel}</strong>.</Alert>
      <ul style={{ color: "#cbd5f5", fontSize: "0.88rem", lineHeight: 1.6, margin: "12px 0 0", paddingLeft: 20 }}>
        <li>The stored credentials are permanently destroyed.</li>
        <li>The runtime is disconnected.</li>
        <li>Your trade and validation history is preserved.</li>
      </ul>
      {error && <div style={{ marginTop: 12 }}><Alert type="error">{error}</Alert></div>}
      <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end", gap: 8 }}>
        <Button type="button" variant="secondary" onClick={close} disabled={busy}>Cancel</Button>
        <Button type="button" onClick={confirm} disabled={busy}>{busy ? "Disconnecting…" : "Disconnect"}</Button>
      </div>
    </Dialog>
  );
};
