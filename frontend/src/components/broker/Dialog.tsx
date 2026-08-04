"use client";

import React, { useEffect, useRef } from "react";

/** WP4.2 — accessible modal base for the Broker Accounts action dialogs. role=dialog + aria-modal,
 * labelled title, ESC to close, backdrop click to close, focus moved in on open and a simple focus
 * trap (Tab/Shift+Tab cycle within the dialog). The focus/keydown effect depends ONLY on `open`, so an
 * in-flight action toggling `busy` never re-steals focus. No backend calls. */
type DialogProps = {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  /** Disables ESC/backdrop/close-button while an action is in flight. */
  busy?: boolean;
  labelId?: string;
};

const overlay: React.CSSProperties = {
  position: "fixed", inset: 0, background: "rgba(3,6,20,0.72)", display: "flex",
  alignItems: "center", justifyContent: "center", zIndex: 1000, padding: "1rem",
};
const panel: React.CSSProperties = {
  width: "min(560px, 100%)", maxHeight: "90vh", overflowY: "auto",
  background: "rgba(12,18,40,0.98)", border: "1px solid rgba(255,255,255,0.12)",
  borderRadius: 16, padding: "1.4rem 1.5rem", boxShadow: "0 24px 64px rgba(0,0,0,0.5)",
};

export const Dialog: React.FC<DialogProps> = ({ open, onClose, title, children, busy = false, labelId }) => {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = labelId || "broker-dialog-title";

  // Keep the latest busy/onClose in refs (synced in an effect, never during render) so the focus+keydown
  // effect can depend on `open` alone.
  const busyRef = useRef(busy);
  const onCloseRef = useRef(onClose);
  useEffect(() => { busyRef.current = busy; onCloseRef.current = onClose; });

  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const focusables = () =>
      Array.from(ref.current?.querySelectorAll<HTMLElement>(
        'a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])') ?? []);
    (focusables()[0] ?? ref.current)?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); if (!busyRef.current) onCloseRef.current(); return; }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) { e.preventDefault(); return; }
      const first = items[0], last = items[items.length - 1];
      const active = document.activeElement as HTMLElement;
      if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => { document.removeEventListener("keydown", onKey, true); prev?.focus?.(); };
  }, [open]);

  if (!open) return null;
  return (
    <div style={overlay} onMouseDown={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div ref={ref} role="dialog" aria-modal="true" aria-labelledby={titleId} style={panel}
           onMouseDown={(e) => e.stopPropagation()}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.9rem" }}>
          <h2 id={titleId} style={{ margin: 0, fontSize: "1.15rem", color: "#e9f4ff" }}>{title}</h2>
          <button type="button" aria-label="Close" onClick={() => { if (!busy) onClose(); }} disabled={busy}
                  style={{ background: "transparent", border: "none", color: "#8fa0b7", fontSize: "1.4rem",
                           cursor: busy ? "default" : "pointer", lineHeight: 1 }}>×</button>
        </div>
        {children}
      </div>
    </div>
  );
};
