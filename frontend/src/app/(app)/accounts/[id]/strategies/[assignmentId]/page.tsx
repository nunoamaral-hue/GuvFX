"use client";

import { useParams, redirect } from "next/navigation";
import { AssignmentConfigContent } from "@/components/broker/AssignmentConfigContent";

/** WAYOND account-scoped strategy management — the ASSIGNMENT-specific configuration route.
 * `/accounts/<id>/strategies/<assignmentId>` configures ONE StrategyAssignment (its per-account trading
 * settings: status + position sizing), NOT the global strategy definition. Gated per user on the
 * broker_accounts_ux capability inside the content component. */
export default function AssignmentConfigPage() {
  const params = useParams();
  const rawA = Array.isArray(params?.id) ? params.id[0] : params?.id;
  const rawB = Array.isArray(params?.assignmentId) ? params.assignmentId[0] : params?.assignmentId;
  const accountId = rawA && /^\d{1,18}$/.test(rawA) ? parseInt(rawA, 10) : NaN;
  const assignmentId = rawB && /^\d{1,18}$/.test(rawB) ? parseInt(rawB, 10) : NaN;
  if (!Number.isSafeInteger(accountId) || accountId <= 0) redirect("/accounts");
  if (!Number.isSafeInteger(assignmentId) || assignmentId <= 0) redirect(`/accounts/${accountId}/strategies`);
  return <AssignmentConfigContent accountId={accountId} assignmentId={assignmentId} />;
}
