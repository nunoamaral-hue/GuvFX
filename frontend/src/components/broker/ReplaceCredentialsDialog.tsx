"use client";

import React, { useState } from "react";
import { Dialog } from "@/components/broker/Dialog";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { replaceCredentials } from "@/lib/broker-api";
import { toCustomerError } from "@/lib/broker-status";

/** WP4.2 — replace the stored password. Explains that existing validation becomes invalid until the
 * account is re-validated. The password is write-only; the stored credential is never shown. */
type Props = { open: boolean; accountId: number; onClose: () => void; onReplaced: () => void };

const input: React.CSSProperties = {
  width: "100%", padding: "0.6rem 0.8rem", borderRadius: 10, border: "1px solid rgba(255,255,255,0.1)",
  background: "rgba(8,12,32,0.9)", color: "#e5f4ff", fontSize: "0.9rem", outline: "none", marginTop: 6,
};

export const ReplaceCredentialsDialog: React.FC<Props> = ({ open, accountId, onClose, onReplaced }) => {
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const close = () => { if (!busy) { setPassword(""); setError(""); onClose(); } };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!password) { setError("Enter the new password."); return; }
    setBusy(true); setError("");
    try {
      await replaceCredentials(accountId, password, true);
      setPassword("");
      onReplaced();
      close();
    } catch (err) {
      setError(toCustomerError(err, "We couldn't replace the credentials. Please try again."));
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={close} title="Replace credentials" busy={busy}>
      <Alert type="info">Replacing the password invalidates the current validation. The account will be
        re-validated with the new password.</Alert>
      <form onSubmit={submit}>
        <label htmlFor="rc-password" style={{ display: "block", fontSize: "0.82rem", color: "#9fb0c8", marginTop: 14 }}>
          New password
        </label>
        <input id="rc-password" type="password" style={input} value={password}
               onChange={(e) => setPassword(e.target.value)} disabled={busy} required autoComplete="new-password" />
        {error && <div style={{ marginTop: 12 }}><Alert type="error">{error}</Alert></div>}
        <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button type="button" variant="secondary" onClick={close} disabled={busy}>Cancel</Button>
          <Button type="submit" disabled={busy}>{busy ? "Replacing…" : "Replace & re-validate"}</Button>
        </div>
      </form>
    </Dialog>
  );
};
