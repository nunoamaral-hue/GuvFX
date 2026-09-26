"use client";

import { useParams, redirect } from "next/navigation";
import { AccountStrategiesContent } from "@/components/broker/AccountStrategiesContent";

/** WAYOND account-scoped strategy management — the ACCOUNT-EXPLICIT "Manage strategies" route.
 * `/accounts/<id>/strategies` shows and manages ONLY this account's StrategyAssignments (add/remove),
 * gated per user on the `broker_accounts_ux` capability inside the content component. */
export default function AccountStrategiesPage() {
  const params = useParams();
  const raw = Array.isArray(params?.id) ? params.id[0] : params?.id;
  const accountId = raw && /^\d{1,18}$/.test(raw) ? parseInt(raw, 10) : NaN;
  if (!Number.isSafeInteger(accountId) || accountId <= 0) redirect("/accounts");
  return <AccountStrategiesContent accountId={accountId} />;
}
