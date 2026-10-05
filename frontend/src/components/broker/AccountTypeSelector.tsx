"use client";

import React from "react";

export type AccountType = "demo" | "live";

export type AccountTypeLabels = {
  /** Field legend, e.g. "Account type *". */
  legend: string;
  demoTitle: string;
  demoDesc: string;
  liveTitle: string;
  liveDesc: string;
  /** Real-money warning shown once LIVE is selected. */
  liveWarning?: string;
};

type Props = {
  value: AccountType | null;
  onChange: (value: AccountType) => void;
  labels: AccountTypeLabels;
  disabled?: boolean;
};

/**
 * D2 (Stream D §4) — the REQUIRED Demo/Live account-type selector with NO default selection.
 *
 * The caller gates its submit button on a non-null value (`value === null` → disabled), so a customer
 * can never create an account without explicitly choosing demo or live — this is the UI half of the
 * server-side required-`account_type` contract (the backend rejects a missing/null/invalid type too).
 * LIVE is rendered with a real-money visual treatment (amber) and a warning note. The component is
 * i18n-agnostic (labels are passed in) so it is reused by both the localized accounts form and the
 * English broker wizard. Mirrors the no-default button-group pattern of PlanSelectionStep.
 */
export const AccountTypeSelector: React.FC<Props> = ({ value, onChange, labels, disabled = false }) => {
  const options: { key: AccountType; title: string; desc: string; live: boolean }[] = [
    { key: "demo", title: labels.demoTitle, desc: labels.demoDesc, live: false },
    { key: "live", title: labels.liveTitle, desc: labels.liveDesc, live: true },
  ];
  return (
    <fieldset style={{ border: "none", margin: 0, padding: 0 }}>
      <legend style={{ fontSize: "0.85rem", color: "#9fb0c8", margin: "0 0 0.4rem", padding: 0 }}>
        {labels.legend}
      </legend>
      <div style={{ display: "flex", gap: "0.6rem" }}>
        {options.map((opt) => {
          const selected = value === opt.key;
          // Amber for LIVE (real funds), blue for DEMO — matches the app's accent conventions.
          const accent = opt.live ? "251, 146, 60" : "74, 179, 255";
          return (
            <button
              key={opt.key}
              type="button"
              role="radio"
              aria-checked={selected}
              aria-label={opt.title}
              disabled={disabled}
              onClick={() => !disabled && onChange(opt.key)}
              style={{
                flex: 1,
                display: "flex",
                flexDirection: "column",
                gap: "0.2rem",
                padding: "0.7rem 0.85rem",
                borderRadius: 10,
                textAlign: "left",
                cursor: disabled ? "not-allowed" : "pointer",
                border: selected
                  ? `1.5px solid rgba(${accent}, 0.7)`
                  : "1px solid rgba(255,255,255,0.1)",
                background: selected ? `rgba(${accent}, 0.1)` : "rgba(255,255,255,0.02)",
                outline: "none",
                transition: "border-color 0.15s, background 0.15s",
              }}
            >
              <span
                style={{
                  fontSize: "0.9rem",
                  fontWeight: 600,
                  color: selected && opt.live ? "#fdba74" : "#e9f4ff",
                }}
              >
                {opt.title}
              </span>
              <span style={{ fontSize: "0.78rem", color: "#8fa0b7" }}>{opt.desc}</span>
            </button>
          );
        })}
      </div>
      {value === "live" && labels.liveWarning && (
        <p style={{ margin: "0.5rem 0 0", fontSize: "0.78rem", color: "#fdba74", lineHeight: 1.45 }}>
          {labels.liveWarning}
        </p>
      )}
    </fieldset>
  );
};
