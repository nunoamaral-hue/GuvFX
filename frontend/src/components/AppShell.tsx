"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "@/lib/api";

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
  label: string;
  href: string;
  adminOnly?: boolean;
  soon?: boolean; // Marks features not yet implemented
};

type NavGroup = {
  label: string;
  items: NavItem[];
  defaultOpen?: boolean;
};

// =============================================================================
// NAVIGATION CONFIGURATION
// Organized into collapsible dropdown groups (max 2 levels)
// =============================================================================

const navGroups: NavGroup[] = [
  {
    label: "Strategy",
    defaultOpen: true,
    items: [
      { label: "My Strategies", href: "/strategies" },
      { label: "Marketplace", href: "/strategies/marketplace" },
      { label: "Create Strategy", href: "/strategies/create" },
      { label: "Strategy Advisor", href: "/ai/strategy-advisor", soon: true },
    ],
  },
  {
    label: "Run",
    defaultOpen: true,
    items: [
      { label: "Backtests", href: "/backtests" },
      { label: "Live Trading", href: "/trading/live", soon: true },
      { label: "Trade History", href: "/trading/trade-history" },
    ],
  },
  {
    label: "Analytics",
    defaultOpen: false,
    items: [
      { label: "Overview", href: "/dashboard" },
      { label: "Performance", href: "/dashboard/performance", soon: true },
      { label: "Strategy Metrics", href: "/analytics/strategy-metrics" },
      { label: "Charts", href: "/charts" },
    ],
  },
  {
    label: "Settings",
    defaultOpen: false,
    items: [
      { label: "Broker Accounts", href: "/accounts" },
      { label: "User Settings", href: "/profile" },
      { label: "Hosting", href: "/admin/hosting", adminOnly: true },
    ],
  },
];

// =============================================================================
// INLINE SVG ICONS (no external dependencies)
// =============================================================================

function LogoIcon() {
  return (
    <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
      <rect width="32" height="32" rx="8" fill="url(#logoGrad)" />
      <path
        d="M8 16L12 10L16 16L20 10L24 16"
        stroke="#fff"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M8 20L12 14L16 20L20 14L24 20"
        stroke="#fff"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity="0.6"
      />
      <defs>
        <linearGradient id="logoGrad" x1="0" y1="0" x2="32" y2="32">
          <stop stopColor="#1d4ed8" />
          <stop offset="1" stopColor="#22c1c3" />
        </linearGradient>
      </defs>
    </svg>
  );
}

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
// HELPER: Check if path is active
// =============================================================================

function isActive(pathname: string, href: string) {
  const [hrefPath] = href.split("?");
  return pathname === hrefPath || pathname.startsWith(hrefPath + "/");
}

// =============================================================================
// HELPER: Logout handler (clears BOTH tokens)
// =============================================================================

function handleLogout() {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem("guvfx_access_token");
    window.localStorage.removeItem("guvfx_refresh_token");
    window.location.href = "/login?reason=logged_out";
  }
}

// =============================================================================
// COMPONENT: Collapsible Nav Group
// =============================================================================

type NavGroupComponentProps = {
  group: NavGroup;
  pathname: string;
  isStaff: boolean;
  onNavigate?: () => void; // For closing mobile drawer on nav
};

function NavGroupComponent({ group, pathname, isStaff, onNavigate }: NavGroupComponentProps) {
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
        <span>{group.label}</span>
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
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.soon ? "#" : item.href}
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
                  justifyContent: "space-between",
                  padding: "0.45rem 0.75rem",
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
                <span>{item.label}</span>
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
                    Soon
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
  onToggle: () => void;
  onClose: () => void;
};

function ProfileDropdown({ currentUser, isOpen, onToggle, onClose }: ProfileDropdownProps) {
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
          {currentUser?.username || "Account"}
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
                <span>Profile</span>
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
                <span>Settings</span>
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
                <span>Logout</span>
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// =============================================================================
// COMPONENT: Language Toggle (UI only, no functionality)
// =============================================================================

function LanguageToggle() {
  const [lang, setLang] = useState<"EN" | "JP">("EN");

  return (
    <button
      type="button"
      onClick={() => setLang(lang === "EN" ? "JP" : "EN")}
      style={{
        display: "flex",
        alignItems: "center",
        gap: "0.25rem",
        padding: "0.35rem 0.6rem",
        background: "rgba(255,255,255,0.05)",
        border: "1px solid rgba(255,255,255,0.1)",
        borderRadius: "6px",
        color: "#9ca3af",
        fontSize: "0.75rem",
        fontWeight: 600,
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
    >
      <span style={{ opacity: lang === "EN" ? 1 : 0.5 }}>EN</span>
      <span style={{ opacity: 0.3 }}>/</span>
      <span style={{ opacity: lang === "JP" ? 1 : 0.5 }}>JP</span>
    </button>
  );
}

// =============================================================================
// COMPONENT: Notifications Button (UI only, no badge logic)
// =============================================================================

function NotificationsButton() {
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
      title="Notifications"
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
  pathname: string;
  isStaff: boolean;
};

function MobileDrawer({ isOpen, onClose, pathname, isStaff }: MobileDrawerProps) {
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
            <LogoIcon />
            <span style={{ fontSize: "1.2rem", fontWeight: 600, color: "#e5f4ff" }}>GuvFX</span>
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
              key={group.label}
              group={group}
              pathname={pathname}
              isStaff={isStaff}
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
            <span>Logout</span>
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

  return (
    <>
      {/* CSS for responsive behavior */}
      <style>{`
        /* Mobile breakpoint: hide sidebar, show hamburger */
        @media (max-width: 980px) {
          .appshell-sidebar {
            display: none !important;
          }
          .appshell-hamburger {
            display: flex !important;
          }
          .appshell-main {
            margin-left: 0 !important;
          }
          .appshell-topbar {
            left: 0 !important;
          }
          .appshell-search {
            display: none !important;
          }
        }
        /* Desktop: show sidebar, hide hamburger */
        @media (min-width: 981px) {
          .appshell-sidebar {
            display: flex !important;
          }
          .appshell-hamburger {
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
            <LogoIcon />
            <div>
              <div style={{ fontSize: "1.2rem", fontWeight: 600, color: "#e5f4ff" }}>GuvFX</div>
              <div style={{ fontSize: "0.7rem", color: "#6b7280", marginTop: "2px" }}>
                Trading Intelligence
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
                key={group.label}
                group={group}
                pathname={pathname}
                isStaff={isStaff}
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
              {currentUser ? currentUser.email : "Logged in"}
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
              <span>Log out</span>
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

              {/* Mobile logo (visible only via hamburger context) */}
              <Link
                href="/dashboard"
                className="appshell-hamburger"
                style={{
                  display: "none",
                  alignItems: "center",
                  gap: "0.5rem",
                  textDecoration: "none",
                }}
              >
                <LogoIcon />
                <span style={{ fontSize: "1.1rem", fontWeight: 600, color: "#e5f4ff" }}>GuvFX</span>
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
                  placeholder="Search strategies, backtests..."
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
                  Soon
                </span>
              </div>
            </div>

            {/* Right section: Language toggle, Notifications, Profile */}
            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
              <LanguageToggle />
              <NotificationsButton />
              <ProfileDropdown
                currentUser={currentUser}
                isOpen={profileDropdownOpen}
                onToggle={() => setProfileDropdownOpen(!profileDropdownOpen)}
                onClose={closeProfileDropdown}
              />
            </div>
          </header>

          {/* ===================================================================
              PAGE CONTENT
          =================================================================== */}
          <main
            style={{
              flex: 1,
              padding: "1.5rem 2rem 2.5rem 2rem",
            }}
          >
            {children}
          </main>
        </div>

        {/* ===================================================================
            MOBILE DRAWER (Slide-over from left)
        =================================================================== */}
        <MobileDrawer
          isOpen={mobileDrawerOpen}
          onClose={closeMobileDrawer}
          pathname={pathname}
          isStaff={isStaff}
        />
      </div>
    </>
  );
}
