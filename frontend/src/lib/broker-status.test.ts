import { describe, expect, it } from "vitest";
import {
  connectionView, healthStatusView, lastValidatedLine, latestAttemptLine, maskAccountNumber, reasonMessage,
  reasonShort, toCustomerError, validationActions, validationStatusView,
} from "@/lib/broker-status";

describe("validationStatusView", () => {
  it("maps every known status to a label + colour", () => {
    expect(validationStatusView("VALIDATED")).toEqual({ label: "Validated", color: "green" });
    expect(validationStatusView("CONNECTION_FAILED").color).toBe("red");
    expect(validationStatusView("TECHNICAL_ERROR").color).toBe("yellow");
    expect(validationStatusView("NEVER").label).toBe("Not validated");
  });
  it("falls back to Unknown for unrecognised/empty values", () => {
    expect(validationStatusView("GIBBERISH")).toEqual({ label: "Unknown", color: "gray" });
    expect(validationStatusView(null).label).toBe("Unknown");
    expect(validationStatusView(undefined).label).toBe("Unknown");
  });
});

describe("healthStatusView", () => {
  it("maps HEALTHY/NEEDS_ATTENTION/UNAVAILABLE", () => {
    expect(healthStatusView("HEALTHY").color).toBe("green");
    expect(healthStatusView("NEEDS_ATTENTION").color).toBe("yellow");
    expect(healthStatusView("UNAVAILABLE").color).toBe("red");
  });
  it("defaults to Unknown", () => {
    expect(healthStatusView(undefined).label).toBe("Unknown");
    expect(healthStatusView("weird").label).toBe("Unknown");
  });
});

describe("reasonMessage", () => {
  it("maps known reason codes to customer-safe wording", () => {
    expect(reasonMessage("invalid_password")).toMatch(/password was not accepted/i);
    expect(reasonMessage("demo_ok")).toMatch(/demo account verified/i);
  });
  it("never surfaces an unknown raw code — returns a NEUTRAL generic message (not 'check your details')", () => {
    const out = reasonMessage("internal_stacktrace_x99");
    expect(out).not.toContain("internal_stacktrace_x99");
    // Regression (packet WS-H): an unknown/technical code must NOT blame the customer's details.
    expect(out).not.toMatch(/check your details/i);
    expect(out).toMatch(/couldn't complete the connection check/i);
  });
  it("classifies validation_unconfigured as a service-side issue — never the customer's details", () => {
    // Root cause of the beta failure: broker validation isn't provisioned yet. The message must say so,
    // confirm nothing changed, tell the user there is nothing to fix on their side, and NOT invite a
    // futile retry of details they cannot change.
    const out = reasonMessage("validation_unconfigured");
    expect(out).toMatch(/broker validation isn't available/i);
    expect(out).toMatch(/weren't changed/i);
    expect(out).toMatch(/nothing to fix on your side/i);
    expect(out).not.toMatch(/check your details/i);
  });
  it("classifies a validation-host/IPC failure as a start failure — NEVER a broker outage, no internals", () => {
    // WS-A: the local MT5 IPC failure must not read as "the broker is down", and must not leak IPC/session/
    // MT5/error-code/host detail.
    const out = reasonMessage("validation_ipc_unavailable");
    expect(out).toMatch(/couldn't start the secure broker-validation session/i);
    expect(out).toMatch(/weren't rejected/i);
    expect(out).not.toMatch(/unavailable/i);          // no broker-outage claim
    // internal leaks forbidden — but the ordinary word "session" ("validation session") is customer-safe.
    expect(out).not.toMatch(/\bIPC\b|Session\s*0|MetaTrader|\bMT5\b|10004|process id|window station/i);
    expect(out).not.toMatch(/check your details/i);
  });
  it("maps credential_missing / broker_server_missing to their own actionable wording", () => {
    expect(reasonMessage("credential_missing")).toMatch(/saved password/i);
    expect(reasonMessage("broker_server_missing")).toMatch(/broker server is set/i);
  });
  it("no service-side reason code is presented as a user-credential error", () => {
    for (const code of ["validation_unconfigured", "bridge_unavailable", "mt5_unavailable",
                        "runtime_unavailable", "login_timeout", "could_not_verify"]) {
      expect(reasonMessage(code)).not.toMatch(/check (your )?(details|password|account number)/i);
    }
  });
  it("returns empty for no code", () => {
    expect(reasonMessage("")).toBe("");
    expect(reasonMessage(null)).toBe("");
  });
});

describe("Phase-4 WS-C — reliability terminology (broker vs validation-infra distinction)", () => {
  it("login_timeout is transport-ambiguous — NEVER attributes the failure to the broker", () => {
    // S1: login_timeout can come from a backend->agent transport timeout (agent maybe never reached) — it
    // must not say/imply "the broker didn't respond / is at fault".
    expect(reasonMessage("login_timeout")).not.toMatch(/broker/i);
    expect(reasonMessage("login_timeout")).toMatch(/didn't complete in time/i);
    expect(reasonShort("login_timeout")).not.toMatch(/broker/i);
  });
  it("validation_busy is a DISTINCT, retryable service condition — not a broker fault, not the credentials", () => {
    // S3: validation_busy is agent-single-flight busy. It must have its own wording (not the generic
    // fallback), never blame the broker, and never blame the customer's details.
    const msg = reasonMessage("validation_busy");
    expect(msg).toMatch(/busy/i);
    expect(msg).not.toMatch(/broker/i);
    expect(msg).not.toMatch(/check (your )?(details|password)/i);
    expect(msg).not.toBe(reasonMessage("some_unknown_code_zzz"));   // distinct from the generic fallback
    expect(reasonShort("validation_busy")).toMatch(/busy/i);
  });
  it("the 'validation service unavailable' family has a distinct SHORT outcome (not the generic fallback)", () => {
    // S14: mt5_/bridge_/runtime_unavailable had long-form messages but no reasonShort → generic fallback.
    for (const c of ["mt5_unavailable", "bridge_unavailable", "runtime_unavailable"]) {
      expect(reasonShort(c)).toMatch(/validation temporarily unavailable/i);
      expect(reasonShort(c)).not.toMatch(/couldn't complete the check/i);   // no longer the generic fallback
      expect(reasonShort(c)).not.toMatch(/broker/i);
    }
  });
  it("only genuinely broker-reached reasons mention the broker being unavailable", () => {
    // server_unavailable is preserved ONLY for broker-reached evidence — it MAY mention the broker.
    expect(reasonMessage("server_unavailable")).toMatch(/broker server is temporarily unavailable/i);
    // ...but the validation-infra reasons must NOT.
    for (const c of ["validation_ipc_unavailable", "validation_busy", "mt5_unavailable", "bridge_unavailable",
                     "runtime_unavailable", "login_timeout"]) {
      expect(reasonMessage(c)).not.toMatch(/broker (server )?is (temporarily )?unavailable/i);
    }
  });
});

describe("lastValidatedLine", () => {
  const T1 = "2026-08-01T10:00:00Z";
  it("shows the timestamp only when currently validated", () => {
    expect(lastValidatedLine("VALIDATED", T1)).toMatch(/^Last validated /);
    // was validated then degraded — still a real prior success, keep the timestamp
    expect(lastValidatedLine("TECHNICAL_ERROR", T1)).toMatch(/^Last validated /);
  });
  it("suppresses a stale timestamp under a never-validated status (no contradiction after disconnect)", () => {
    // disconnect resets validation_status to NEVER but does not null validated_at — the timestamp must
    // NOT render as "Last validated" under a "Not validated" badge.
    expect(lastValidatedLine("NEVER", T1)).toBe("No successful validation yet");
  });
  it("reads 'No successful validation yet' when there is no timestamp", () => {
    expect(lastValidatedLine("TECHNICAL_ERROR", null)).toBe("No successful validation yet");
    expect(lastValidatedLine("NEVER", null)).toBe("No successful validation yet");
    expect(lastValidatedLine(undefined, undefined)).toBe("No successful validation yet");
  });
});

describe("validationActions", () => {
  it("offers Replace credentials for a fixable credential problem", () => {
    expect(validationActions("invalid_password")).toEqual(["replace"]);
    expect(validationActions("credential_missing")).toEqual(["replace"]);
  });
  it("offers Try again for transient / service hiccups and for any unknown code", () => {
    for (const c of ["login_timeout", "server_unavailable", "could_not_verify", "mt5_unavailable",
                     "bridge_unavailable", "runtime_unavailable", "some_new_code_x1", ""]) {
      expect(validationActions(c)).toEqual(["retry"]);
    }
  });
  it("offers NO in-modal action when the customer can't fix it (guidance only, never a misleading button)", () => {
    // validation_unconfigured / disabled / live-in-demo-beta / wrong account number with no in-place edit:
    // the message carries the guidance; no button pretends the customer can change these.
    for (const c of ["validation_unconfigured", "account_disabled", "live_detected", "classification_mismatch",
                     "broker_server_missing", "server_not_found", "invalid_login"]) {
      expect(validationActions(c)).toEqual([]);
    }
  });
  it("offers NO immediate retry for a validation-host/IPC failure (prevents retry storms)", () => {
    // WS-A: this failure is slow and holds the single-flight validator; an immediate Try again would just
    // queue another self-colliding attempt. Guidance ("try again later") lives in the message; Close only.
    expect(validationActions("validation_ipc_unavailable")).toEqual([]);
  });
  it("never offers Replace for a service-side reason (no false 'fix your password' affordance)", () => {
    for (const c of ["validation_unconfigured", "login_timeout", "bridge_unavailable", "account_disabled"]) {
      expect(validationActions(c)).not.toContain("replace");
    }
  });
});

describe("reasonShort + latestAttemptLine (WS-C dual state)", () => {
  it("reasonShort is concise, safe, never leaks internals", () => {
    expect(reasonShort("demo_ok")).toBe("Verified");
    expect(reasonShort("validation_ipc_unavailable")).toMatch(/couldn't start the validation session/i);
    expect(reasonShort("some_unknown")).toMatch(/couldn't complete the check/i);
    expect(reasonShort("")).toMatch(/couldn't complete the check/i);
    for (const c of ["validation_ipc_unavailable", "login_timeout", "server_unavailable"]) {
      expect(reasonShort(c)).not.toMatch(/\bIPC\b|Session\s*0|\bMT5\b|10004/i);
    }
  });
  it("latestAttemptLine renders the most recent attempt as its own line, distinct from last success", () => {
    const T = "2026-08-05T17:23:00Z";
    expect(latestAttemptLine({ status: "HEALTHY", created_at: T })).toMatch(/^Latest attempt: .* — Verified$/);
    const ipc = latestAttemptLine({ status: "UNAVAILABLE", reason_code: "validation_ipc_unavailable", created_at: T });
    expect(ipc).toMatch(/^Latest attempt: /);
    expect(ipc).toMatch(/couldn't start the validation session/i);
    expect(ipc).not.toMatch(/unavailable|\bIPC\b|\bMT5\b/i);   // not a broker-outage claim, no internals
    expect(latestAttemptLine(null)).toBe("");
    expect(latestAttemptLine({ status: "UNAVAILABLE" })).toBe("");  // no timestamp → no line
  });
  it("suppresses a stale HEALTHY 'latest attempt' under a NEVER badge (no contradiction after disconnect)", () => {
    const T = "2026-08-05T13:37:00Z";
    // disconnect resets validation_status to NEVER but keeps the old HEALTHY attempt as the latest.
    expect(latestAttemptLine({ status: "HEALTHY", created_at: T }, "NEVER")).toBe("");
    // a FAILED latest under NEVER is consistent → still shown.
    expect(latestAttemptLine({ status: "UNAVAILABLE", reason_code: "validation_ipc_unavailable", created_at: T }, "NEVER"))
      .toMatch(/^Latest attempt: /);
    // HEALTHY latest under a VALIDATED badge is consistent → shown.
    expect(latestAttemptLine({ status: "HEALTHY", created_at: T }, "VALIDATED")).toMatch(/Verified$/);
  });
});

describe("connectionView", () => {
  it("disconnected wins over active", () => {
    expect(connectionView(true, "2026-08-04T00:00:00Z").label).toBe("Disconnected");
    expect(connectionView(true, null).label).toBe("Connected");
    expect(connectionView(false, null).label).toBe("Inactive");
  });
});

describe("toCustomerError", () => {
  it("returns a plain DRF detail as-is (already customer-safe)", () => {
    expect(toCustomerError(new Error("password is required."))).toBe("password is required.");
  });
  it("flattens a DRF field-error JSON blob into readable text (no raw JSON)", () => {
    const out = toCustomerError(new Error('{"account_number":["This account number already exists."]}'));
    expect(out).toBe("This account number already exists.");
    expect(out).not.toContain("{");
    expect(out).not.toContain("account_number");
  });
  it("falls back to the generic message on empty/unparseable input", () => {
    expect(toCustomerError(new Error("{bad json"), "fallback")).toBe("fallback");
    expect(toCustomerError(null, "fallback")).toBe("fallback");
  });
  it("maps a tagged network error to a safe message — NEVER the raw transport text", () => {
    const netErr = Object.assign(new Error("network_unreachable"), { kind: "network" });
    const out = toCustomerError(netErr, "fallback");
    expect(out).toMatch(/couldn't reach the validation service/i);
    expect(out).toMatch(/weren't changed/i);
    expect(out).not.toMatch(/network_unreachable|Failed to fetch|TypeError/i);
  });
  it("never surfaces a raw fetch/exception string, even if it reaches here untagged", () => {
    // Regression for the customer-visible "Failed to fetch" (gunicorn-killed request).
    for (const raw of ["Failed to fetch", "NetworkError when attempting to fetch resource.",
                       "TypeError: Failed to fetch", "Load failed", "network_unreachable",
                       "The operation was aborted", "Request failed: 502",
                       "Unexpected token < in JSON", "boom\n    at foo (app.js:12)"]) {
      const out = toCustomerError(new Error(raw), "safe fallback");
      expect(out).toBe("safe fallback");
      expect(out).not.toContain("fetch");
    }
  });
});

describe("maskAccountNumber", () => {
  it("shows only the last 4 digits", () => {
    expect(maskAccountNumber("1302575")).toBe("••••2575");
    expect(maskAccountNumber("12")).toBe("••12");
    expect(maskAccountNumber("")).toBe("");
    expect(maskAccountNumber(null)).toBe("");
    expect(maskAccountNumber("1302575")).not.toContain("130");
  });
});
