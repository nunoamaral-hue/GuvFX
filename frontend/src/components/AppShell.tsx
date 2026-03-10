"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useEffect, useState, useCallback, createContext, useContext } from "react";
import { apiFetch } from "@/lib/api";
import { type Lang, detectLang, setLang as persistLang, t } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

// =============================================================================
// TYPES
// =============================================================================

type Me = {
  id: number;
  email: string;
  username: string;
  is_staff: boolean;
  is_superuser: boolean;
};

type AppShellProps = {
  children: ReactNode;
};

type NavItem = {
  labelKey: string; // i18n key
  href: string;
  adminOnly?: boolean;
  soon?: boolean; // Marks features not yet implemented
};

type NavGroup = {
  labelKey: string; // i18n key
  items: NavItem[];
  defaultOpen?: boolean;
};

// =============================================================================
// LANGUAGE CONTEXT (for child components to access lang)
// =============================================================================

const LangContext = createContext<Lang>("en");
export const useLang = () => useContext(LangContext);

// =============================================================================
// NAVIGATION CONFIGURATION
// Organized into collapsible dropdown groups (max 2 levels)
// =============================================================================

const navGroups: NavGroup[] = [
  {
    labelKey: "nav.strategy",
    defaultOpen: true,
    items: [
      { labelKey: "nav.myStrategies", href: "/strategies" },
      { labelKey: "nav.marketplace", href: "/strategies/marketplace" },
      { labelKey: "nav.createStrategy", href: "/strategies/create" },
      { labelKey: "nav.strategyAdvisor", href: "/ai/strategy-advisor", soon: true },
    ],
  },
  {
    labelKey: "nav.run",
    defaultOpen: true,
    items: [
      { labelKey: "nav.backtests", href: "/backtests" },
      { labelKey: "nav.liveTrading", href: "/trading/live-trading" },
      { labelKey: "nav.tradeHistory", href: "/trading/trade-history" },
    ],
  },
  {
    labelKey: "nav.analytics",
    defaultOpen: false,
    items: [
      { labelKey: "nav.overview", href: "/dashboard" },
      { labelKey: "nav.performance", href: "/dashboard/performance", soon: true },
      { labelKey: "nav.strategyMetrics", href: "/analytics/strategy-metrics" },
      { labelKey: "nav.charts", href: "/charts" },
    ],
  },
  {
    labelKey: "nav.account",
    defaultOpen: false,
    items: [
      { labelKey: "nav.billingPlans", href: "/account/billing" },
      { labelKey: "nav.accountHosting", href: "/account/hosting" },
    ],
  },
  {
    labelKey: "nav.settings",
    defaultOpen: false,
    items: [
      { labelKey: "nav.brokerAccounts", href: "/accounts" },
      { labelKey: "nav.userSettings", href: "/profile" },
      { labelKey: "nav.hosting", href: "/admin/hosting", adminOnly: true },
    ],
  },
];

// =============================================================================
// INLINE SVG ICONS (no external dependencies)
// =============================================================================

function SearchIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="11" cy="11" r="8" />
      <path d="M21 21l-4.35-4.35" />
    </svg>
  );
}

function BellIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </svg>
  );
}

function UserIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  );
}

function ChevronDownIcon({ open }: { open: boolean }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      style={{
        transform: open ? "rotate(180deg)" : "rotate(0deg)",
        transition: "transform 200ms ease",
      }}
    >
      <path d="M6 9l6 6 6-6" />
    </svg>
  );
}

function HamburgerIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M3 12h18M3 6h18M3 18h18" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M18 6L6 18M6 6l12 12" />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <polyline points="16,17 21,12 16,7" />
      <line x1="21" y1="12" x2="9" y2="12" />
    </svg>
  );
}

function SettingsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function ProfileIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  );
}

// =============================================================================
// HELPER: Per-item inline SVG icons (no external library)
// =============================================================================

const NAV_ICON_PROPS = {
  width: 16, height: 16, viewBox: "0 0 24 24", fill: "none",
  stroke: "currentColor", strokeWidth: 2,
  strokeLinecap: "round" as const, strokeLinejoin: "round" as const,
};

function navIcon(labelKey: string): ReactNode {
  switch (labelKey) {
    case "nav.myStrategies":
      return <svg {...NAV_ICON_PROPS}><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg>;
    case "nav.marketplace":
      return <svg {...NAV_ICON_PROPS}><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z" /><line x1="3" y1="6" x2="21" y2="6" /><path d="M16 10a4 4 0 0 1-8 0" /></svg>;
    case "nav.createStrategy":
      return <svg {...NAV_ICON_PROPS}><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="16" /><line x1="8" y1="12" x2="16" y2="12" /></svg>;
    case "nav.strategyAdvisor":
      return <svg {...NAV_ICON_PROPS}><path d="M9.66 17h4.68M12 3v1m6.36 1.64l-.7.7M21 12h-1M4 12H3m3.34-5.66l-.7-.7m2.83 9.9a5 5 0 1 1 7.07 0l-.55.55A3.37 3.37 0 0 0 14 18.47V19a2 2 0 1 1-4 0v-.53c0-.9-.36-1.75-.99-2.39l-.55-.55z" /></svg>;
    case "nav.backtests":
      return <svg {...NAV_ICON_PROPS}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></svg>;
    case "nav.liveTrading":
      return <svg {...NAV_ICON_PROPS}><polygon points="13 2 3 14 12 14 11 22 21 10 12 10" /></svg>;
    case "nav.tradeHistory":
      return <svg {...NAV_ICON_PROPS}><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>;
    case "nav.overview":
      return <svg {...NAV_ICON_PROPS}><rect x="3" y="3" width="7" height="7" /><rect x="14" y="3" width="7" height="7" /><rect x="14" y="14" width="7" height="7" /><rect x="3" y="14" width="7" height="7" /></svg>;
    case "nav.performance":
      return <svg {...NAV_ICON_PROPS}><polyline points="23 6 13.5 15.5 8.5 10.5 1 18" /><polyline points="17 6 23 6 23 12" /></svg>;
    case "nav.strategyMetrics":
      return <svg {...NAV_ICON_PROPS}><line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" /></svg>;
    case "nav.charts":
      return <svg {...NAV_ICON_PROPS}><line x1="4" y1="19" x2="4" y2="14" /><line x1="8" y1="19" x2="8" y2="9" /><line x1="12" y1="19" x2="12" y2="5" /><line x1="16" y1="19" x2="16" y2="10" /><line x1="20" y1="19" x2="20" y2="7" /></svg>;
    case "nav.brokerAccounts":
      return <svg {...NAV_ICON_PROPS}><rect x="1" y="4" width="22" height="16" rx="2" ry="2" /><line x1="1" y1="10" x2="23" y2="10" /></svg>;
    case "nav.userSettings":
      return <svg {...NAV_ICON_PROPS}><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></svg>;
    case "nav.hosting":
      return <svg {...NAV_ICON_PROPS}><rect x="2" y="2" width="20" height="8" rx="2" ry="2" /><rect x="2" y="14" width="20" height="8" rx="2" ry="2" /><line x1="6" y1="6" x2="6.01" y2="6" /><line x1="6" y1="18" x2="6.01" y2="18" /></svg>;
    default:
      return null;
  }
}

// =============================================================================
// HELPER: Active route resolution (single-active-item)
// =============================================================================

const allNavHrefs: string[] = navGroups.flatMap((g) =>
  g.items.filter((i) => !i.soon).map((i) => i.href)
);

function findActiveHref(pathname: string): string | null {
  // 1. Exact match wins
  const exact = allNavHrefs.find((h) => pathname === h);
  if (exact) return exact;
  // 2. Longest prefix match (e.g. /strategies/123 → /strategies)
  let best: string | null = null;
  for (const href of allNavHrefs) {
    if (pathname.startsWith(href + "/")) {
      if (!best || href.length > best.length) best = href;
    }
  }
  return best;
}

// =============================================================================
// HELPER: Logout handler (clears ALL auth state — server + client)
// =============================================================================

const API_BASE = "https://api.guvfx.com";

async function handleLogout() {
  if (typeof window === "undefined") return;

  // 1. Server-side logout — clears HttpOnly session cookies.
  //    POST /api/auth/cookie/logout/ returns 200 and expires guvfx_access + guvfx_refresh cookies.
  //    Best-effort: if this fails we still clear client state and redirect.
  try {
    // Grab CSRF token for the POST
    const csrfMatch = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    const csrf = csrfMatch ? decodeURIComponent(csrfMatch[1]) : "";

    await fetch(`${API_BASE}/api/auth/cookie/logout/`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(csrf ? { "X-CSRFToken": csrf } : {}),
      },
    });
  } catch {
    // Network error — proceed with client-side cleanup anyway
  }

  // 2. Clear localStorage tokens
  window.localStorage.removeItem("guvfx_access_token");
  window.localStorage.removeItem("guvfx_refresh_token");

  // 3. Expire non-HttpOnly cookies we control (best effort)
  document.cookie = "csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";

  // 4. Replace history entry — prevents back-button access to app pages.
  //    window.location.replace() removes the current entry from history,
  //    so pressing Back after logout won't land on the protected page.
  window.location.replace("/login?reason=logged_out");
}

// =============================================================================
// COMPONENT: Collapsible Nav Group
// =============================================================================

type NavGroupComponentProps = {
  group: NavGroup;
  activeHref: string | null;
  isStaff: boolean;
  lang: Lang;
  onNavigate?: () => void;
};

function NavGroupComponent({ group, activeHref, isStaff, lang, onNavigate }: NavGroupComponentProps) {
  const [isOpen, setIsOpen] = useState(group.defaultOpen ?? false);

  // Filter out admin-only items if user is not staff
  const visibleItems = group.items.filter((item) => !item.adminOnly || isStaff);

  if (visibleItems.length === 0) return null;

  return (
    <div style={{ marginBottom: "0.5rem" }}>
      {/* Group header (clickable to toggle) */}
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          width: "100%",
          padding: "0.5rem 0.75rem",
          background: "transparent",
          border: "none",
          borderRadius: "6px",
          color: "#9ca3af",
          fontSize: "0.75rem",
          fontWeight: 600,
          letterSpacing: "0.05em",
          textTransform: "uppercase",
          cursor: "pointer",
          transition: "color 150ms ease, background 150ms ease",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.color = "#e5f4ff";
          e.currentTarget.style.background = "rgba(255,255,255,0.03)";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.color = "#9ca3af";
          e.currentTarget.style.background = "transparent";
        }}
      >
        <span>{t(lang, group.labelKey)}</span>
        <ChevronDownIcon open={isOpen} />
      </button>

      {/* Collapsible items */}
      <div
        style={{
          overflow: "hidden",
          maxHeight: isOpen ? `${visibleItems.length * 40}px` : "0px",
          transition: "max-height 200ms ease",
        }}
      >
        <div style={{ paddingLeft: "0.5rem", paddingTop: "0.25rem" }}>
          {visibleItems.map((item) => {
            const active = item.href === activeHref;
            return (
              <Link
                key={item.href}
                href={item.soon ? "#" : item.href}
                className={`nav-item${active ? " nav-item-active" : ""}${item.soon ? " nav-item-soon" : ""}`}
                onClick={(e) => {
                  if (item.soon) {
                    e.preventDefault();
                  } else if (onNavigate) {
                    onNavigate();
                  }
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  padding: "0.5rem 0.75rem",
                  marginBottom: "2px",
                  borderRadius: "6px",
                  fontSize: "0.85rem",
                  textDecoration: "none",
                  color: item.soon ? "#6b7280" : active ? "#e5f4ff" : "#9ca3af",
                  background: active
                    ? "linear-gradient(90deg, #1d4ed8, #22c1c3)"
                    : "transparent",
                  boxShadow: active ? "0 0 0 1px rgba(59,130,246,0.35)" : "none",
                  cursor: item.soon ? "not-allowed" : "pointer",
                  transition: "background 140ms ease, color 140ms ease",
                }}
              >
                <span style={{ display: "flex", alignItems: "center", opacity: item.soon ? 0.4 : active ? 1 : 0.6, flexShrink: 0 }}>
                  {navIcon(item.labelKey)}
                </span>
                <span style={{ flex: 1 }}>{t(lang, item.labelKey)}</span>
                {item.soon && (
                  <span
                    style={{
                      fontSize: "0.65rem",
                      padding: "2px 6px",
                      borderRadius: "4px",
                      background: "rgba(107, 114, 128, 0.3)",
                      color: "#6b7280",
                      fontWeight: 500,
                    }}
                  >
                    {t(lang, "ui.soon")}
                  </span>
                )}
                {active && !item.soon && (
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: "999px",
                      backgroundColor: "rgba(15,23,42,0.9)",
                    }}
                  />
                )}
              </Link>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// COMPONENT: Profile Dropdown
// =============================================================================

type ProfileDropdownProps = {
  currentUser: Me | null;
  isOpen: boolean;
  lang: Lang;
  onToggle: () => void;
  onClose: () => void;
};

function ProfileDropdown({ currentUser, isOpen, lang, onToggle, onClose }: ProfileDropdownProps) {
  return (
    <div style={{ position: "relative" }}>
      <button
        type="button"
        onClick={onToggle}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          padding: "0.4rem 0.75rem",
          background: "rgba(255,255,255,0.05)",
          border: "1px solid rgba(255,255,255,0.1)",
          borderRadius: "8px",
          color: "#e5f4ff",
          cursor: "pointer",
          transition: "background 150ms ease",
        }}
      >
        <UserIcon />
        <span style={{ fontSize: "0.85rem", maxWidth: "100px", overflow: "hidden", textOverflow: "ellipsis" }}>
          {currentUser?.username || t(lang, "ui.account")}
        </span>
        <ChevronDownIcon open={isOpen} />
      </button>

      {/* Dropdown menu */}
      {isOpen && (
        <>
          {/* Backdrop to close dropdown */}
          <div
            onClick={onClose}
            style={{
              position: "fixed",
              inset: 0,
              zIndex: 999,
            }}
          />
          <div
            style={{
              position: "absolute",
              top: "calc(100% + 8px)",
              right: 0,
              minWidth: "180px",
              background: "#0f172a",
              border: "1px solid rgba(255,255,255,0.1)",
              borderRadius: "8px",
              boxShadow: "0 10px 40px rgba(0,0,0,0.5)",
              zIndex: 1000,
              overflow: "hidden",
            }}
          >
            {/* User info header */}
            {currentUser && (
              <div
                style={{
                  padding: "0.75rem 1rem",
                  borderBottom: "1px solid rgba(255,255,255,0.1)",
                }}
              >
                <div style={{ fontSize: "0.85rem", fontWeight: 500, color: "#e5f4ff" }}>
                  {currentUser.username}
                </div>
                <div style={{ fontSize: "0.75rem", color: "#6b7280", marginTop: "2px" }}>
                  {currentUser.email}
                </div>
              </div>
            )}

            {/* Menu items */}
            <div style={{ padding: "0.5rem" }}>
              <Link
                href="/profile"
                onClick={onClose}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  padding: "0.5rem 0.75rem",
                  borderRadius: "6px",
                  fontSize: "0.85rem",
                  color: "#9ca3af",
                  textDecoration: "none",
                  transition: "background 150ms ease, color 150ms ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "rgba(255,255,255,0.05)";
                  e.currentTarget.style.color = "#e5f4ff";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "#9ca3af";
                }}
              >
                <ProfileIcon />
                <span>{t(lang, "ui.profile")}</span>
              </Link>
              <Link
                href="/profile"
                onClick={onClose}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  padding: "0.5rem 0.75rem",
                  borderRadius: "6px",
                  fontSize: "0.85rem",
                  color: "#9ca3af",
                  textDecoration: "none",
                  transition: "background 150ms ease, color 150ms ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "rgba(255,255,255,0.05)";
                  e.currentTarget.style.color = "#e5f4ff";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                  e.currentTarget.style.color = "#9ca3af";
                }}
              >
                <SettingsIcon />
                <span>{t(lang, "ui.settings")}</span>
              </Link>
              <div style={{ height: "1px", background: "rgba(255,255,255,0.1)", margin: "0.5rem 0" }} />
              <button
                type="button"
                onClick={() => {
                  onClose();
                  handleLogout();
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  width: "100%",
                  padding: "0.5rem 0.75rem",
                  borderRadius: "6px",
                  fontSize: "0.85rem",
                  color: "#ef4444",
                  background: "transparent",
                  border: "none",
                  cursor: "pointer",
                  transition: "background 150ms ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "rgba(239, 68, 68, 0.1)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "transparent";
                }}
              >
                <LogoutIcon />
                <span>{t(lang, "ui.logout")}</span>
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// =============================================================================
// COMPONENT: Language Toggle — now delegates to shared LanguageDropdown
// See: frontend/src/components/LanguageDropdown.tsx
// =============================================================================

// =============================================================================
// COMPONENT: Notifications Button (UI only, no badge logic)
// =============================================================================

type NotificationsButtonProps = {
  lang: Lang;
};

function NotificationsButton({ lang }: NotificationsButtonProps) {
  return (
    <button
      type="button"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: "36px",
        height: "36px",
        background: "rgba(255,255,255,0.05)",
        border: "1px solid rgba(255,255,255,0.1)",
        borderRadius: "8px",
        color: "#9ca3af",
        cursor: "pointer",
        transition: "background 150ms ease, color 150ms ease",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.background = "rgba(255,255,255,0.08)";
        e.currentTarget.style.color = "#e5f4ff";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.background = "rgba(255,255,255,0.05)";
        e.currentTarget.style.color = "#9ca3af";
      }}
      title={t(lang, "ui.notifications")}
    >
      <BellIcon />
    </button>
  );
}

// =============================================================================
// COMPONENT: Mobile Drawer
// =============================================================================

type MobileDrawerProps = {
  isOpen: boolean;
  onClose: () => void;
  activeHref: string | null;
  isStaff: boolean;
  lang: Lang;
};

function MobileDrawer({ isOpen, onClose, activeHref, isStaff, lang }: MobileDrawerProps) {
  // Prevent body scroll when drawer is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [isOpen]);

  return (
    <>
      {/* Backdrop */}
      <div
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0,0,0,0.6)",
          zIndex: 9998,
          opacity: isOpen ? 1 : 0,
          visibility: isOpen ? "visible" : "hidden",
          transition: "opacity 200ms ease, visibility 200ms ease",
        }}
      />

      {/* Drawer panel */}
      <div
        style={{
          position: "fixed",
          top: 0,
          left: 0,
          bottom: 0,
          width: "280px",
          maxWidth: "85vw",
          background: "#0a0f1a",
          borderRight: "1px solid rgba(255,255,255,0.1)",
          zIndex: 9999,
          transform: isOpen ? "translateX(0)" : "translateX(-100%)",
          transition: "transform 250ms ease",
          display: "flex",
          flexDirection: "column",
          overflowY: "auto",
        }}
      >
        {/* Drawer header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "1rem 1.25rem",
            borderBottom: "1px solid rgba(255,255,255,0.1)",
          }}
        >
          <Link
            href="/dashboard"
            onClick={onClose}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.75rem",
              textDecoration: "none",
            }}
          >
            <img
              src="/brand/logo.png"
              alt="GuvFX"
              style={{ width: 32, height: 32, objectFit: "contain" }}
            />
            <span style={{ fontSize: "1.2rem", fontWeight: 600, color: "#ffffff" }}>GuvFX</span>
          </Link>
          <button
            type="button"
            onClick={onClose}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              width: "36px",
              height: "36px",
              background: "transparent",
              border: "none",
              color: "#9ca3af",
              cursor: "pointer",
            }}
          >
            <CloseIcon />
          </button>
        </div>

        {/* Navigation groups */}
        <nav style={{ flex: 1, padding: "1rem 0.75rem" }}>
          {navGroups.map((group) => (
            <NavGroupComponent
              key={group.labelKey}
              group={group}
              activeHref={activeHref}
              isStaff={isStaff}
              lang={lang}
              onNavigate={onClose}
            />
          ))}
        </nav>

        {/* Logout button at bottom */}
        <div
          style={{
            padding: "1rem 1.25rem",
            borderTop: "1px solid rgba(255,255,255,0.1)",
          }}
        >
          <button
            type="button"
            onClick={() => {
              onClose();
              handleLogout();
            }}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: "0.5rem",
              width: "100%",
              padding: "0.65rem 1rem",
              background: "rgba(239, 68, 68, 0.1)",
              border: "1px solid rgba(239, 68, 68, 0.3)",
              borderRadius: "8px",
              color: "#ef4444",
              fontSize: "0.85rem",
              fontWeight: 500,
              cursor: "pointer",
              transition: "background 150ms ease",
            }}
          >
            <LogoutIcon />
            <span>{t(lang, "ui.logout")}</span>
          </button>
        </div>
      </div>
    </>
  );
}

// =============================================================================
// MAIN COMPONENT: AppShell
// =============================================================================

export function AppShell({ children }: AppShellProps) {
  const pathname = usePathname();

  // Language state (persisted via cookie + localStorage)
  // Using lazy initial state to avoid calling detectLang() on every render
  // detectLang() is safe to call during SSR (returns "en" when window is undefined)
  const [lang, setLangState] = useState<Lang>(() => {
    // On server, default to "en"; on client, detect from storage
    if (typeof window === "undefined") return "en";
    return detectLang();
  });

  // Handler for language changes (persist + update state)
  const handleLangChange = useCallback((newLang: Lang) => {
    persistLang(newLang);
    setLangState(newLang);
  }, []);

  // Auth state (best-effort only)
  const [currentUser, setCurrentUser] = useState<Me | null>(null);

  // UI state
  const [profileDropdownOpen, setProfileDropdownOpen] = useState(false);
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);

  // =============================================================================
  // AUTH EFFECT: Best-effort /api/auth/me call
  // - Skipped on localhost to avoid CORS issues
  // - Never crashes the shell if it fails
  // - Clears both tokens on logout (handled in handleLogout)
  // =============================================================================
  useEffect(() => {
    // AppShell should never hard-fail if /auth/me is unavailable.
    // In local development, calling the live API often triggers CORS and noisy overlays.
    // Treat /auth/me as best-effort enrichment only.
    const isLocalhost =
      typeof window !== "undefined" &&
      (window.location.hostname === "localhost" ||
        window.location.hostname === "127.0.0.1" ||
        window.location.hostname === "0.0.0.0");

    const hasToken =
      typeof window !== "undefined" &&
      (!!window.localStorage.getItem("guvfx_access_token") ||
        !!window.localStorage.getItem("guvfx_refresh_token"));

    // If we cannot reasonably determine auth, keep currentUser as null and render the shell.
    if (isLocalhost || !hasToken) {
      return;
    }

    let cancelled = false;

    apiFetch<Me>("/api/auth/me/", {})
      .then((data) => {
        if (!cancelled) {
          setCurrentUser(data);
        }
      })
      .catch((err) => {
        // Best-effort only: do not spam console.error (Next dev overlay) or block rendering.
        console.warn("AppShell: failed to load current user (non-blocking):", err);
        if (!cancelled) {
          setCurrentUser(null);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  // Determine staff status for admin-only nav items
  const isStaff =
    !!currentUser && (currentUser.is_staff === true || currentUser.is_superuser === true);

  // Close profile dropdown when clicking outside
  const closeProfileDropdown = useCallback(() => setProfileDropdownOpen(false), []);

  // Close mobile drawer on route change (handled via onNavigate prop)
  const closeMobileDrawer = useCallback(() => setMobileDrawerOpen(false), []);

  // Single-active-item: resolve which nav href best matches current path
  const activeHref = findActiveHref(pathname);

  return (
    <LangContext.Provider value={lang}>
      {/* CSS for responsive behavior */}
      <style>{`
        /* Prevent horizontal overflow globally */
        html, body {
          overflow-x: hidden;
        }
        /* Nav item hover (non-active, non-disabled) */
        .nav-item:not(.nav-item-active):not(.nav-item-soon):hover {
          background: rgba(255,255,255,0.06) !important;
          color: #d1dff0 !important;
        }
        .nav-item:not(.nav-item-active):not(.nav-item-soon):hover span {
          opacity: 1 !important;
        }
        /* Mobile breakpoint: hide sidebar, show hamburger and brand */
        @media (max-width: 980px) {
          .appshell-sidebar {
            display: none !important;
          }
          .appshell-hamburger,
          .appshell-brand {
            display: flex !important;
          }
          .appshell-main {
            margin-left: 0 !important;
            min-width: 0;
          }
          .appshell-topbar {
            left: 0 !important;
            padding: 0 1rem !important;
          }
          .appshell-search {
            display: none !important;
          }
          .appshell-content {
            padding: 1rem 1rem 1.5rem 1rem !important;
          }
          .appshell-brand-text {
            max-width: 120px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
          }
          .appshell-brand-logo {
            width: 28px !important;
            height: 28px !important;
          }
        }
        /* Extra small screens: tighter padding */
        @media (max-width: 480px) {
          .appshell-content {
            padding: 1rem 0.75rem 1.5rem 0.75rem !important;
          }
          .appshell-topbar {
            padding: 0 0.75rem !important;
          }
          .appshell-brand-text {
            max-width: 80px;
            font-size: 1rem !important;
          }
          .appshell-topbar-right {
            gap: 0.5rem !important;
          }
          .appshell-brand-logo {
            width: 26px !important;
            height: 26px !important;
          }
        }
        /* Desktop: show sidebar, hide hamburger and mobile brand */
        @media (min-width: 981px) {
          .appshell-sidebar {
            display: flex !important;
          }
          .appshell-hamburger,
          .appshell-brand {
            display: none !important;
          }
        }
      `}</style>

      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          background:
            "radial-gradient(circle at top left, #0b1020, #050713 55%, #030612 100%)",
          color: "#e5f4ff",
          minWidth: 0,
          maxWidth: "100vw",
          overflowX: "hidden",
        }}
      >
        {/* ===================================================================
            SIDEBAR (Desktop only, hidden < 980px)
        =================================================================== */}
        <aside
          className="appshell-sidebar"
          style={{
            width: 260,
            padding: "1rem 0.75rem",
            borderRight: "1px solid rgba(255,255,255,0.08)",
            display: "flex",
            flexDirection: "column",
            position: "fixed",
            top: 0,
            left: 0,
            bottom: 0,
            overflowY: "auto",
            background: "rgba(10, 15, 26, 0.95)",
            zIndex: 100,
          }}
        >
          {/* Brand / Logo */}
          <Link
            href="/dashboard"
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.75rem",
              padding: "0.75rem 1rem",
              marginBottom: "1rem",
              textDecoration: "none",
            }}
          >
            <img
              src="/brand/logo.png"
              alt="GuvFX"
              style={{ width: 32, height: 32, objectFit: "contain" }}
            />
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", textAlign: "center" }}>
              <div style={{ fontSize: "1.2rem", fontWeight: 600, color: "#ffffff", letterSpacing: "0.01em" }}>GuvFX</div>
              <div style={{ fontSize: "0.7rem", color: "#6b7280", marginTop: "2px", letterSpacing: "0.02em" }}>
                {t(lang, "ui.tradingIntelligence")}
              </div>
            </div>
          </Link>

          {/* Navigation groups */}
          <nav
            aria-label="Main navigation"
            style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
            }}
          >
            {navGroups.map((group) => (
              <NavGroupComponent
                key={group.labelKey}
                group={group}
                activeHref={activeHref}
                isStaff={isStaff}
                lang={lang}
              />
            ))}
          </nav>

          {/* User section at bottom of sidebar */}
          <div
            style={{
              marginTop: "auto",
              padding: "1rem 0.75rem",
              borderTop: "1px solid rgba(255,255,255,0.08)",
            }}
          >
            <div style={{ fontSize: "0.75rem", color: "#6b7280", marginBottom: "0.5rem" }}>
              {currentUser ? currentUser.email : t(lang, "ui.loggedIn")}
            </div>
            <button
              type="button"
              onClick={handleLogout}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: "0.5rem",
                width: "100%",
                padding: "0.5rem 0.75rem",
                borderRadius: "6px",
                border: "1px solid rgba(255,255,255,0.1)",
                background: "rgba(15,23,42,0.85)",
                color: "#9ca3af",
                fontSize: "0.8rem",
                cursor: "pointer",
                transition: "background 150ms ease, color 150ms ease",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = "rgba(239, 68, 68, 0.1)";
                e.currentTarget.style.color = "#ef4444";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = "rgba(15,23,42,0.85)";
                e.currentTarget.style.color = "#9ca3af";
              }}
            >
              <LogoutIcon />
              <span>{t(lang, "ui.logout")}</span>
            </button>
          </div>
        </aside>

        {/* ===================================================================
            MAIN CONTENT AREA
        =================================================================== */}
        <div
          className="appshell-main"
          style={{
            flex: 1,
            marginLeft: 260, // Matches sidebar width
            display: "flex",
            flexDirection: "column",
            minHeight: "100vh",
            minWidth: 0,
          }}
        >
          {/* ===================================================================
              TOP BAR
          =================================================================== */}
          <header
            className="appshell-topbar"
            style={{
              position: "sticky",
              top: 0,
              left: 260,
              right: 0,
              height: "60px",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "0 1.5rem",
              background: "rgba(10, 15, 26, 0.9)",
              backdropFilter: "blur(12px)",
              borderBottom: "1px solid rgba(255,255,255,0.08)",
              zIndex: 99,
            }}
          >
            {/* Left section: Hamburger (mobile) + Logo (mobile) */}
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
              {/* Hamburger button (mobile only) */}
              <button
                type="button"
                className="appshell-hamburger"
                onClick={() => setMobileDrawerOpen(true)}
                style={{
                  display: "none", // Hidden by default, shown via CSS on mobile
                  alignItems: "center",
                  justifyContent: "center",
                  width: "40px",
                  height: "40px",
                  background: "transparent",
                  border: "none",
                  color: "#e5f4ff",
                  cursor: "pointer",
                }}
              >
                <HamburgerIcon />
              </button>

              {/* Mobile logo (visible only on mobile) */}
              <Link
                href="/dashboard"
                className="appshell-brand"
                style={{
                  display: "none",
                  alignItems: "center",
                  gap: "0.5rem",
                  textDecoration: "none",
                  minWidth: 0,
                }}
              >
                <img
                  src="/brand/logo.png"
                  alt="GuvFX"
                  className="appshell-brand-logo"
                  style={{ width: 32, height: 32, objectFit: "contain", flexShrink: 0 }}
                />
                <span
                  className="appshell-brand-text"
                  style={{ fontSize: "1.1rem", fontWeight: 600, color: "#ffffff" }}
                >
                  GuvFX
                </span>
              </Link>
            </div>

            {/* Center section: Search bar (presentational only) */}
            <div
              className="appshell-search"
              style={{
                flex: 1,
                maxWidth: "400px",
                margin: "0 2rem",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  padding: "0.5rem 1rem",
                  background: "rgba(255,255,255,0.05)",
                  border: "1px solid rgba(255,255,255,0.1)",
                  borderRadius: "8px",
                  color: "#6b7280",
                }}
              >
                <SearchIcon />
                <input
                  type="text"
                  placeholder={t(lang, "ui.search")}
                  disabled
                  style={{
                    flex: 1,
                    background: "transparent",
                    border: "none",
                    outline: "none",
                    color: "#9ca3af",
                    fontSize: "0.85rem",
                  }}
                />
                <span
                  style={{
                    fontSize: "0.65rem",
                    padding: "2px 6px",
                    borderRadius: "4px",
                    background: "rgba(107, 114, 128, 0.3)",
                    color: "#6b7280",
                  }}
                >
                  {t(lang, "ui.soon")}
                </span>
              </div>
            </div>

            {/* Right section: Language toggle, Notifications, Profile */}
            <div
              className="appshell-topbar-right"
              style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexShrink: 0 }}
            >
              <LanguageDropdown lang={lang} onChange={handleLangChange} variant="compact" />
              <NotificationsButton lang={lang} />
              <ProfileDropdown
                currentUser={currentUser}
                isOpen={profileDropdownOpen}
                lang={lang}
                onToggle={() => setProfileDropdownOpen(!profileDropdownOpen)}
                onClose={closeProfileDropdown}
              />
            </div>
          </header>

          {/* ===================================================================
              PAGE CONTENT
          =================================================================== */}
          <main
            className="appshell-content"
            style={{
              flex: 1,
              padding: "1.5rem 2rem 2.5rem 2rem",
              minWidth: 0,
            }}
          >
            {children}
          </main>

          {/* ===================================================================
              LEGAL FOOTER
          =================================================================== */}
          <LegalFooter lang={lang} />
        </div>

        {/* ===================================================================
            MOBILE DRAWER (Slide-over from left)
        =================================================================== */}
        <MobileDrawer
          isOpen={mobileDrawerOpen}
          onClose={closeMobileDrawer}
          activeHref={activeHref}
          isStaff={isStaff}
          lang={lang}
        />
      </div>
    </LangContext.Provider>
  );
}
