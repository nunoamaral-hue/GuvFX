"use client";

import React, { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { notFound, useParams } from "next/navigation";
import { operationsEnabled } from "@/lib/flags";
import { useAdminRole } from "@/components/admin/useAdminRole";
import { getAccountEvents } from "@/lib/operations-api";
import { OpsSummary } from "@/components/operations/OpsSummary";
import { OpsTimelineTable } from "@/components/operations/OpsTimelineTable";
import { EventDetailDialog } from "@/components/operations/EventDetailDialog";
import { OpsFilters, EMPTY_FILTERS, applyClientFilters, type OpsFilterState } from "@/components/operations/OpsFilters";
import { EmptyState, ErrorState, LoadingState } from "@/components/broker/States";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { toCustomerError } from "@/lib/broker-status";
import type { OperationalEvent, OperationsResponse } from "@/types/operations";

/** WP5.3 (ADR-0032) — Operations & Support: account overview + timeline + event detail. Flag-gated 404
 * when OFF (BEFORE any hook/API); operator-only (useAdminRole; backend enforces owner-scoping). Read-only:
 * category is filtered server-side + paginated; the other filters + search are client-side over the page.
 * The fetch runs in an async IIFE inside the effect so no setState is synchronous in the effect body
 * (react-hooks/set-state-in-effect); loading-resets happen in the page/filter handlers, not the effect. */
const wrap: React.CSSProperties = { maxWidth: 1040, margin: "0 auto", padding: "1.5rem 1rem" };
const PAGE = 50;

export default function OperationsAccountDetailPage() {
  if (!operationsEnabled()) notFound();

  const { loading: authLoading, authorized } = useAdminRole();
  const params = useParams();
  const id = Number(Array.isArray(params?.id) ? params.id[0] : params?.id);

  const [data, setData] = useState<OperationsResponse | null>(null);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [filters, setFilters] = useState<OpsFilterState>(EMPTY_FILTERS);
  const [selected, setSelected] = useState<OperationalEvent | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // `category` is the ONLY server-side filter; it (with offset) drives the reload effect.
  const category = filters.category;

  useEffect(() => {
    if (!authorized || !Number.isFinite(id)) return;
    let cancelled = false;
    void (async () => {
      try {
        // Peek one extra row (PAGE + 1) to know deterministically whether a next page exists, then show
        // only PAGE rows. Without this, an exactly-full final page (length === PAGE) leaves "Next" enabled
        // and clicking it lands on an empty page.
        const res = await getAccountEvents(id, { limit: PAGE + 1, offset, category: category || null });
        if (cancelled) return;
        setData({ summary: res.summary, timeline: res.timeline.slice(0, PAGE) });
        setHasMore(res.timeline.length > PAGE);
        setError("");
      } catch (err) {
        if (!cancelled) setError(toCustomerError(err, "We couldn't load this account's operations data."));
      }
    })();
    return () => { cancelled = true; };
  }, [authorized, id, offset, category, reloadKey]);

  const visibleEvents = useMemo(
    () => (data ? applyClientFilters(data.timeline, filters) : []),
    [data, filters],
  );

  // Loading-resets are triggered from user gestures (not the effect) — synchronous setState is fine here.
  const reload = () => { setData(null); setError(""); setReloadKey((k) => k + 1); };
  const goToPage = (nextOffset: number) => { setData(null); setError(""); setOffset(nextOffset); };
  const onFilterChange = (next: OpsFilterState) => {
    // A change to the server-side category resets pagination and refetches.
    if (next.category !== filters.category) { setData(null); setError(""); setOffset(0); }
    setFilters(next);
  };

  if (!Number.isFinite(id)) notFound();
  if (authLoading) return <div style={wrap}><LoadingState label="Loading…" /></div>;
  if (!authorized) {
    return (
      <div style={wrap}>
        <EmptyState title="Restricted"
                    body="The Operations & Support console is available to internal operators only." />
      </div>
    );
  }
  if (error) {
    return <div style={wrap}><ErrorState message={error} onRetry={reload} /></div>;
  }

  return (
    <div style={wrap}>
      <Link href="/operations/accounts" style={{ color: "#93c5fd", fontSize: "0.85rem", textDecoration: "none" }}>
        ← Operations &amp; Support
      </Link>
      <h1 style={{ margin: "8px 0 16px", fontSize: "1.5rem", color: "#e9f4ff" }}>Account #{id}</h1>

      <Card title="Overview">
        {data === null ? <LoadingState label="Loading overview…" /> : <OpsSummary summary={data.summary} />}
      </Card>

      <div style={{ marginTop: 18 }}>
        <Card title="Timeline">
          <div style={{ marginBottom: 14 }}>
            <OpsFilters value={filters} onChange={onFilterChange} />
          </div>
          {data === null
            ? <LoadingState label="Loading timeline…" />
            : <OpsTimelineTable events={visibleEvents} onSelect={setSelected} />}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 14, gap: 8 }}>
            <span style={{ color: "#8fa0b7", fontSize: "0.8rem" }}>
              {data ? `Showing ${visibleEvents.length} of ${data.timeline.length} on this page` : ""}
            </span>
            <div style={{ display: "flex", gap: 8 }}>
              <Button variant="secondary" disabled={offset === 0}
                      onClick={() => goToPage(Math.max(0, offset - PAGE))}>Previous</Button>
              <Button variant="secondary" disabled={!data || !hasMore}
                      onClick={() => goToPage(offset + PAGE)}>Next</Button>
            </div>
          </div>
        </Card>
      </div>

      <EventDetailDialog event={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
