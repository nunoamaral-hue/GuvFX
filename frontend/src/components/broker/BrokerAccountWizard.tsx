"use client";

import React, { useState } from "react";
import { Dialog } from "@/components/broker/Dialog";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { StatusBadge } from "@/components/broker/StatusBadge";
import { createAccount, testConnection } from "@/lib/broker-api";
import { healthStatusView, reasonMessage } from "@/lib/broker-status";
import type { ValidationAttempt } from "@/types/broker";

/** WP4.2 — Add a broker account, then validate it via the existing backend. The password is submitted
 * write-only to the create endpoint and never stored/echoed in the UI. On success the parent refreshes. */
type Props = { open: boolean; onClose: () => void; onAdded: () => void };

const label: React.CSSProperties = { display: "block", fontSize: "0.82rem", color: "#9fb0c8", margin: "0.7rem 0 0.3rem" };
const input: React.CSSProperties = {
  width: "100%", padding: "0.6rem 0.8rem", borderRadius: 10, border: "1px solid rgba(255,255,255,0.1)",
  background: "rgba(8,12,32,0.9)", color: "#e5f4ff", fontSize: "0.9rem", outline: "none",
};

export const BrokerAccountWizard: React.FC<Props> = ({ open, onClose, onAdded }) => {
  const [name, setName] = useState("");
  const [broker, setBroker] = useState("");
  const [server, setServer] = useState("");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [isDemo, setIsDemo] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ValidationAttempt | null>(null);

  const reset = () => {
    setName(""); setBroker(""); setServer(""); setLogin(""); setPassword("");
    setIsDemo(true); setBusy(false); setError(""); setResult(null);
  };
  const close = () => { if (!busy) { reset(); onClose(); } };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(""); setResult(null);
    if (!broker.trim() || !login.trim() || !password) { setError("Broker, account number and password are required."); return; }
    setBusy(true);
    try {
      const acct = await createAccount({
        name: name.trim() || broker.trim(), broker_name: `${broker.trim()}${server.trim() ? ` (${server.trim()})` : ""}`,
        account_number: login.trim(), password, is_demo: isDemo,
      });
      setPassword(""); // drop the plaintext as soon as it is submitted
      const attempt = await testConnection(acct.id);
      setResult(attempt);
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : "We couldn't add the account. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={close} title="Add a broker account" busy={busy}>
      {result ? (
        <div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10 }}>
            <StatusBadge view={healthStatusView(result.status)} />
            <span style={{ color: "#cbd5f5", fontSize: "0.9rem" }}>{reasonMessage(result.reason_code)}</span>
          </div>
          {result.status === "HEALTHY"
            ? <Alert type="success">Account added and validated.</Alert>
            : <Alert type="error">Account added, but validation did not succeed. You can retry from the account page.</Alert>}
          <div style={{ marginTop: 14, textAlign: "right" }}>
            <Button onClick={close}>Done</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={submit}>
          <label style={label} htmlFor="ba-name">Account name (optional)</label>
          <input id="ba-name" style={input} value={name} onChange={(e) => setName(e.target.value)} disabled={busy} autoComplete="off" />
          <label style={label} htmlFor="ba-broker">Broker</label>
          <input id="ba-broker" style={input} value={broker} onChange={(e) => setBroker(e.target.value)} disabled={busy} required autoComplete="off" />
          <label style={label} htmlFor="ba-server">Server</label>
          <input id="ba-server" style={input} value={server} onChange={(e) => setServer(e.target.value)} disabled={busy} autoComplete="off" />
          <label style={label} htmlFor="ba-login">Account number / login</label>
          <input id="ba-login" style={input} value={login} onChange={(e) => setLogin(e.target.value)} disabled={busy} required autoComplete="off" />
          <label style={label} htmlFor="ba-password">Password</label>
          <input id="ba-password" type="password" style={input} value={password} onChange={(e) => setPassword(e.target.value)} disabled={busy} required autoComplete="new-password" />
          <label style={{ ...label, display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <input type="checkbox" checked={isDemo} onChange={(e) => setIsDemo(e.target.checked)} disabled={busy} />
            This is a demo account
          </label>
          {error && <div style={{ marginTop: 12 }}><Alert type="error">{error}</Alert></div>}
          <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <Button type="button" variant="secondary" onClick={close} disabled={busy}>Cancel</Button>
            <Button type="submit" disabled={busy}>{busy ? "Validating…" : "Add & validate"}</Button>
          </div>
        </form>
      )}
    </Dialog>
  );
};
