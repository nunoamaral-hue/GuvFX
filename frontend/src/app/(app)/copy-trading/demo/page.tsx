"use client";

import { useLang } from "@/components/AppShell";
import { OwnerDemoControlContent } from "@/components/broker/OwnerDemoControlContent";

/** GFX-170 / ADR-0124 — OWNER-operable governed #46→#48 Copy Trading demonstration control.
 *
 * Route `/copy-trading/demo`. Lets the authenticated tenant OWNER of the governed route check readiness,
 * prepare, deliberately confirm (fresh re-authentication), and stop — without staff access and without pasting
 * shell commands or secrets. DARK by default: the API 503s unless the Sponsor has armed the feature flag, and the
 * real #46→#48 execution still requires a separate, explicit, head/runtime-pinned Sponsor real-money GO. The
 * browser never receives or submits a signing secret. */
export default function OwnerDemoControlPage() {
  const lang = useLang();
  return <OwnerDemoControlContent lang={lang} />;
}
