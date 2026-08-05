"use client";

import { redirect, useParams } from "next/navigation";

/** WS-A (packet: Customer Journey Consolidation) — the per-account detail now lives under the canonical
 * /accounts tree. This legacy route PERMANENTLY redirects /broker-accounts/[id] → /accounts/[id] so
 * existing deep links keep working. Loop-safe: /accounts/[id] renders in-place, it never redirects here. */
export default function BrokerAccountDetailPage() {
  const params = useParams();
  const id = Array.isArray(params?.id) ? params.id[0] : params?.id;
  redirect(id ? `/accounts/${id}` : "/accounts");
}
