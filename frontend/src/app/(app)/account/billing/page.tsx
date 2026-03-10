"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { ActionRequestModal } from "@/components/ActionRequestModal";
import type { Subscription, SubscriptionResponse } from "@/types/billing";

// ─────────────────────────────────────────────────────────────────────
// Display helpers — humanization is display-only, never used for logic
// ─────────────────────────────────────────────────────────────────────

const humanize = (s: string) =>
  s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });

const fmtDateTime = (iso: string) =>
  new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

// ─────────────────────────────────────────────────────────────────────
// Product subscription tiers — static catalog (not driven by API)
// ─────────────────────────────────────────────────────────────────────

type ProductPlan = {
  key: string; // matches backend current_plan value
  name: string;
  price: string;
  period: string;
  subtitle: string;
  features: string[];
  highlighted?: boolean;
  badge?: string;
};

const PRODUCT_PLANS: ProductPlan[] = [
  {
    key: "starter_trial",
    name: "Starter Trial",
    price: "$0",
    period: "for 30 days",
    subtitle: "Explore the full platform before you commit.",
    badge: "30-day trial",
    features: [
      "Full dashboard access",
      "Create & edit strategies",
      "Run backtests (limited)",
      "Marketplace browsing",
      "Community support",
    ],
  },
  {
    key: "standard",
    name: "Standard",
    price: "$29",
    period: "/month",
    subtitle: "For active traders building and testing strategies.",
    highlighted: true,
    badge: "Most Popular",
    features: [
      "Everything in Starter",
      "Unlimited backtests",
      "Strategy marketplace access",
      "Priority execution queue",
      "Email support",
    ],
  },
  {
    key: "pro",
    name: "Pro",
    price: "$79",
    period: "/month",
    subtitle: "For serious traders who need full automation.",
    badge: "Best Value",
    features: [
      "Everything in Standard",
      "Live automation",
      "Multi-account support",
      "Advanced analytics",
      "Priority support",
    ],
  },
  {
    key: "advanced",
    name: "Advanced",
    price: "$149",
    period: "/month",
    subtitle: "For power users and institutional workflows.",
    features: [
      "Everything in Pro",
      "Custom execution hooks",
      "API access",
      "Dedicated account manager",
      "SLA guarantees",
    ],
  },
];

// ─────────────────────────────────────────────────────────────────────
// Shared styles
// ─────────────────────────────────────────────────────────────────────

const glassCard: React.CSSProperties = {
  borderRadius: 16,
  border: "1px solid rgba(74, 179, 255, 0.12)",
  background:
    "linear-gradient(135deg, rgba(10, 15, 40, 0.95) 0%, rgba(5, 8, 22, 0.98) 100%)",
  boxShadow:
    "0 8px 32px rgba(0, 0, 0, 0.4), 0 0 60px rgba(30, 111, 255, 0.04)",
  padding: "1.5rem",
  display: "flex",
  flexDirection: "column" as const,
};

const labelStyle: React.CSSProperties = {
  fontSize: "0.8rem",
  color: "#94a3b8",
  marginBottom: 2,
};

const valueStyle: React.CSSProperties = {
  fontSize: "0.9rem",
  color: "#e9f4ff",
};

// Status color mapping
const statusColor: Record<string, string> = {
  trial_active: "#4ade80",
  active: "#4ade80",
  past_due: "#fbbf24",
  cancelled: "#f87171",
  expired: "#94a3b8",
  viewer_only: "#94a3b8",
};

// ─────────────────────────────────────────────────────────────────────
// Subscription detail row — only renders non-null fields
// ─────────────────────────────────────────────────────────────────────

function DetailRow({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null;
  return (
    <div style={{ minWidth: 200 }}>
      <div style={labelStyle}>{label}</div>
      <div style={valueStyle}>{value}</div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────

export default function BillingPage() {
  const [modalOpen, setModalOpen] = useState(false);
  const [modalContext, setModalContext] = useState("");

  // Subscription state from API
  const [subscription, setSubscription] = useState<Subscription | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchSubscription = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await apiFetch<SubscriptionResponse>(
          "/api/billing/subscription/",
          {}
        );
        setSubscription(data.subscription);
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Failed to load subscription.";
        setError(message);
      } finally {
        setLoading(false);
      }
    };
    fetchSubscription();
  }, []);

  const openModal = (planName: string) => {
    setModalContext(planName);
    setModalOpen(true);
  };

  // Compare raw backend value for plan card highlighting
  const isCurrent = (planKey: string) =>
    subscription?.current_plan === planKey;

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
        Billing &amp; Plans
      </h1>
      <p
        style={{
          fontSize: "0.9rem",
          color: "#b7c5dd",
          marginBottom: "1.5rem",
        }}
      >
        Manage your GuvFX product subscription.
      </p>

      {/* ── Current plan banner ── */}
      <div
        style={{
          ...glassCard,
          marginBottom: "1.5rem",
          padding: "1rem 1.25rem",
          flexDirection: "row" as const,
          alignItems: "center",
          justifyContent: "space-between",
          gap: "1rem",
          flexWrap: "wrap" as const,
        }}
      >
        <div>
          <span
            style={{
              fontSize: "0.8rem",
              color: "#94a3b8",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              fontWeight: 600,
            }}
          >
            Current plan
          </span>

          {/* Loading */}
          {loading && (
            <div
              style={{
                fontSize: "1.1rem",
                color: "#8fa0b7",
                fontWeight: 600,
                marginTop: "0.15rem",
              }}
            >
              Loading subscription…
            </div>
          )}

          {/* Error */}
          {!loading && error && (
            <div
              style={{
                fontSize: "0.95rem",
                color: "#f87171",
                marginTop: "0.15rem",
              }}
            >
              {error}
            </div>
          )}

          {/* Null subscription */}
          {!loading && !error && !subscription && (
            <div
              style={{
                fontSize: "1.1rem",
                color: "#e9f4ff",
                fontWeight: 600,
                marginTop: "0.15rem",
              }}
            >
              No active subscription found.
            </div>
          )}

          {/* Subscription present */}
          {!loading && !error && subscription && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.75rem",
                marginTop: "0.15rem",
              }}
            >
              <span
                style={{
                  fontSize: "1.1rem",
                  color: "#e9f4ff",
                  fontWeight: 600,
                }}
              >
                {subscription.current_plan
                  ? humanize(subscription.current_plan)
                  : "No plan selected"}
              </span>
              <span
                style={{
                  fontSize: "0.75rem",
                  fontWeight: 600,
                  padding: "0.15rem 0.6rem",
                  borderRadius: 999,
                  background: `${statusColor[subscription.plan_status] ?? "#94a3b8"}20`,
                  color:
                    statusColor[subscription.plan_status] ?? "#94a3b8",
                  border: `1px solid ${statusColor[subscription.plan_status] ?? "#94a3b8"}40`,
                }}
              >
                {humanize(subscription.plan_status)}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* ── Subscription details (only when data exists) ── */}
      {!loading && !error && subscription && (
        <div
          style={{
            ...glassCard,
            marginBottom: "1.5rem",
            padding: "1.25rem",
          }}
        >
          <div
            style={{
              fontSize: "0.8rem",
              color: "#94a3b8",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              fontWeight: 600,
              marginBottom: "0.75rem",
            }}
          >
            Subscription details
          </div>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: "1.25rem 2rem",
            }}
          >
            <DetailRow
              label="Billing cycle"
              value={
                subscription.billing_cycle
                  ? humanize(subscription.billing_cycle)
                  : null
              }
            />
            <DetailRow
              label="Current period"
              value={
                subscription.current_period_started_at &&
                subscription.current_period_ends_at
                  ? `${fmtDate(subscription.current_period_started_at)} – ${fmtDate(subscription.current_period_ends_at)}`
                  : null
              }
            />
            <DetailRow
              label="Trial started"
              value={
                subscription.trial_started_at
                  ? fmtDate(subscription.trial_started_at)
                  : null
              }
            />
            <DetailRow
              label="Trial expires"
              value={
                subscription.trial_expires_at
                  ? fmtDate(subscription.trial_expires_at)
                  : null
              }
            />
            <DetailRow
              label="Next invoice"
              value={
                subscription.next_invoice_date
                  ? fmtDate(subscription.next_invoice_date)
                  : null
              }
            />
            <DetailRow
              label="Next payment due"
              value={
                subscription.next_payment_due_date
                  ? fmtDate(subscription.next_payment_due_date)
                  : null
              }
            />
            <DetailRow
              label="Last invoice"
              value={
                subscription.last_invoice_date
                  ? fmtDate(subscription.last_invoice_date)
                  : null
              }
            />
            <DetailRow
              label="Last payment"
              value={
                subscription.last_payment_at
                  ? fmtDateTime(subscription.last_payment_at)
                  : null
              }
            />
            <DetailRow
              label="Last plan change"
              value={
                subscription.last_plan_change_at
                  ? fmtDateTime(subscription.last_plan_change_at)
                  : null
              }
            />
          </div>
        </div>
      )}

      {/* ── Plan cards grid (always visible) ── */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
          gap: "1.25rem",
          marginBottom: "2rem",
        }}
      >
        {PRODUCT_PLANS.map((plan) => {
          const current = isCurrent(plan.key);
          return (
            <div
              key={plan.key}
              style={{
                ...glassCard,
                border: current
                  ? "1px solid rgba(74, 222, 128, 0.35)"
                  : plan.highlighted
                    ? "1px solid rgba(59, 130, 246, 0.35)"
                    : "1px solid rgba(74, 179, 255, 0.12)",
                position: "relative",
              }}
            >
              {/* Badge */}
              {(plan.badge || current) && (
                <span
                  style={{
                    display: "inline-block",
                    fontSize: "0.72rem",
                    fontWeight: 600,
                    padding: "0.2rem 0.65rem",
                    borderRadius: 999,
                    background: current
                      ? "rgba(74, 222, 128, 0.15)"
                      : plan.highlighted
                        ? "rgba(59, 130, 246, 0.2)"
                        : "rgba(234, 179, 8, 0.15)",
                    color: current
                      ? "#4ade80"
                      : plan.highlighted
                        ? "#93c5fd"
                        : "#fbbf24",
                    border: `1px solid ${
                      current
                        ? "rgba(74, 222, 128, 0.3)"
                        : plan.highlighted
                          ? "rgba(59, 130, 246, 0.3)"
                          : "rgba(234, 179, 8, 0.3)"
                    }`,
                    marginBottom: "0.75rem",
                    width: "fit-content",
                  }}
                >
                  {current ? "Current Plan" : plan.badge}
                </span>
              )}

              {/* Plan name */}
              <h3
                style={{
                  fontSize: "1.15rem",
                  fontWeight: 700,
                  margin: "0 0 0.5rem",
                  color: "#e9f4ff",
                }}
              >
                {plan.name}
              </h3>

              {/* Price */}
              <div style={{ marginBottom: "0.5rem" }}>
                <span
                  style={{
                    fontSize: "2rem",
                    fontWeight: 700,
                    color: "#93c5fd",
                  }}
                >
                  {plan.price}
                </span>
                <span
                  style={{
                    fontSize: "0.85rem",
                    color: "#64748b",
                    marginLeft: 4,
                  }}
                >
                  {plan.period}
                </span>
              </div>

              {/* Subtitle */}
              <p
                style={{
                  fontSize: "0.85rem",
                  color: "#8fa0b7",
                  lineHeight: 1.5,
                  margin: "0 0 1rem",
                }}
              >
                {plan.subtitle}
              </p>

              {/* Features */}
              <ul
                style={{
                  listStyle: "none",
                  padding: 0,
                  margin: "0 0 1.25rem",
                  flex: 1,
                }}
              >
                {plan.features.map((f) => (
                  <li
                    key={f}
                    style={{
                      display: "flex",
                      alignItems: "flex-start",
                      gap: 8,
                      fontSize: "0.85rem",
                      color: "#c2d5ff",
                      marginBottom: "0.4rem",
                    }}
                  >
                    <span style={{ color: "#4ade80", marginTop: 1 }}>✓</span>
                    {f}
                  </li>
                ))}
              </ul>

              {/* CTA */}
              <button
                onClick={() => !current && openModal(plan.name)}
                disabled={current}
                style={{
                  marginTop: "auto",
                  width: "100%",
                  padding: "0.65rem 1rem",
                  borderRadius: 999,
                  border: current
                    ? "1px solid rgba(74, 222, 128, 0.25)"
                    : plan.highlighted
                      ? "none"
                      : "1px solid rgba(255, 255, 255, 0.15)",
                  background: current
                    ? "transparent"
                    : plan.highlighted
                      ? "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)"
                      : "transparent",
                  color: current
                    ? "#4ade80"
                    : plan.highlighted
                      ? "#fff"
                      : "#c2d5ff",
                  fontSize: "0.9rem",
                  fontWeight: 600,
                  cursor: current ? "default" : "pointer",
                  opacity: current ? 0.7 : 1,
                  boxShadow:
                    !current && plan.highlighted
                      ? "0 8px 24px rgba(30, 111, 255, 0.3)"
                      : "none",
                }}
              >
                {current
                  ? "Current Plan"
                  : plan.highlighted
                    ? "Upgrade"
                    : "Change Plan"}
              </button>
            </div>
          );
        })}
      </div>

      {/* Modal */}
      <ActionRequestModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="Plan change request"
        contextLine={
          modalContext ? `Requested plan: ${modalContext}` : undefined
        }
        confirmationBody="Your request will be recorded once account plan management is enabled."
      />
    </div>
  );
}
