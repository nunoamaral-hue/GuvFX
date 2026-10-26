"use client";

import { useLang } from "@/components/AppShell";
import { InstrumentMatrixContent } from "@/components/broker/InstrumentMatrixContent";

/** Founder-only, owner-scoped, READ-ONLY multi-broker instrument-mapping matrix — `/copy-trading/instruments`.
 *
 * Renders the coverage matrix for every ordered master→follower pair of the owner's own accounts, with a
 * per-pair drill-down of native symbols, classification, verification evidence and material differences.
 * Cohort-gated server-side (a non-cohort caller sees a calm "not available" state). It writes nothing, maps
 * nothing and keeps Copy Trading execution OFF — catalogue coverage is explicitly not execution readiness. */
export default function InstrumentMatrixPage() {
  const lang = useLang();
  return <InstrumentMatrixContent lang={lang} />;
}
