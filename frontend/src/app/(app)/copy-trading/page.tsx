"use client";

import { useLang } from "@/components/AppShell";
import { CopyTradingConfigContent } from "@/components/broker/CopyTradingConfigContent";

/** Founder-only Copy Trading CONFIGURATION — dedicated route `/copy-trading` (SHADOW — EXECUTION OFF).
 *
 * Lets the authorised owner designate exactly one master and one-or-more followers from THEIR OWN live
 * accounts, persisted through the owner-scoped, audited, idempotent config API. Cohort-gated server-side:
 * a non-cohort caller sees a calm "not available" state. Saving configuration does NOT enable execution —
 * the copier stays observe/compare only. The account-scoped read-only monitoring route
 * (`/accounts/<id>/copy-trading`) remains compatible and unchanged. */
export default function CopyTradingConfigPage() {
  const lang = useLang();
  return <CopyTradingConfigContent lang={lang} />;
}
