"use client";

import { useParams, redirect } from "next/navigation";
import { AccountManageContent } from "@/components/broker/AccountManageContent";

/** WAYOND account-scoped strategy management — the canonical per-account management page.
 *
 * Previously this route redirected back to /accounts whenever the legacy global broker-connectivity build
 * flag was OFF, so "Manage" was a dead end (it bounced) and "Manage strategies" landed on a strategy-free
 * page. It now renders the ACCOUNT-EXPLICIT management experience (AccountManageContent), gated PER USER on
 * the certified `broker_accounts_ux` capability inside that component — never on the legacy build flag. */
export default function AccountDetailPage() {
  const params = useParams();
  const raw = Array.isArray(params?.id) ? params.id[0] : params?.id;
  const accountId = raw && /^\d{1,18}$/.test(raw) ? parseInt(raw, 10) : NaN;
  if (!Number.isSafeInteger(accountId) || accountId <= 0) redirect("/accounts");
  return <AccountManageContent accountId={accountId} />;
}
