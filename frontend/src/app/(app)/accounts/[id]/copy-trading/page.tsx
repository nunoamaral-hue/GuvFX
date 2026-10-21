"use client";

import { useParams, redirect } from "next/navigation";
import { useLang } from "@/components/AppShell";
import { CopyTradingContent } from "@/components/broker/CopyTradingContent";

/** Copy Trading — account-scoped, read-only monitoring dashboard (ADR-0051, PR C).
 * `/accounts/<id>/copy-trading` shows how THIS master account's trades are copied to the member's
 * selected follower accounts, plus the visible execution limitations. DARK-safe: the content renders a
 * calm "not enabled" state when the API reports enabled:false (COPIER_UI_ENABLED off). Display-only. */
export default function CopyTradingPage() {
  const params = useParams();
  const lang = useLang();
  const raw = Array.isArray(params?.id) ? params.id[0] : params?.id;
  const accountId = raw && /^\d{1,18}$/.test(raw) ? parseInt(raw, 10) : NaN;
  if (!Number.isSafeInteger(accountId) || accountId <= 0) redirect("/accounts");
  return <CopyTradingContent accountId={accountId} lang={lang} />;
}
