"use client";

import { redirect } from "next/navigation";

/** WS-A (packet: Customer Journey Consolidation) — /broker-accounts is NO LONGER a customer journey.
 * /accounts is the single canonical broker-account page. This route now PERMANENTLY redirects there so
 * existing bookmarks/links keep working. Loop-safe: /accounts never redirects back to /broker-accounts
 * (it renders the broker journey in-place when the flag is ON, legacy content when OFF). */
export default function BrokerAccountsPage() {
  redirect("/accounts");
}
