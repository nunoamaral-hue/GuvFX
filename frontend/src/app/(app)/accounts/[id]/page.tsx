"use client";

import { redirect } from "next/navigation";
import { brokerConnectivityEnabled } from "@/lib/flags";
import { BrokerAccountDetailContent } from "@/components/broker/BrokerAccountDetailContent";

/** WS-A (packet: Customer Journey Consolidation) — canonical per-account detail page. When the broker-
 * connectivity journey is built (flag ON) this renders the WP4 detail experience (status / validation
 * history / Test / Retry / Replace / Disconnect). When OFF (default) the legacy /accounts page has no
 * per-account detail, so this redirects back to the account list — never a dead end, never a loop. */
export default function AccountDetailPage() {
  if (!brokerConnectivityEnabled()) redirect("/accounts");
  return <BrokerAccountDetailContent />;
}
