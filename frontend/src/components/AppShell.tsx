"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ReactNode, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

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
};

type NavSection = {
  label: string;
  items: NavItem[];
};

const navSections: NavSection[] = [
  {
    label: "Dashboard",
    items: [
      { label: "Trading overview", href: "/dashboard" },
      { label: "Charts", href: "/dashboard/charts" },
      { label: "Performance", href: "/dashboard/performance" },
    ],
  },
  {
    label: "Trading",
    items: [
      { label: "Create Strategy", href: "/strategies/create" }, // <— update this
      { label: "My Strategies", href: "/strategies" },
      { label: "Backtests", href: "/backtests" },
      { label: "Performance", href: "/backtests/performance" },
    ],
  },
  {
    label: "Settings",
    items: [
      { label: "Broker Accounts", href: "/accounts" },
      { label: "Hosting", href: "/admin/hosting", adminOnly: true },
      { label: "User Settings", href: "/profile" },
    ],
  },
];

function isActive(pathname: string, href: string) {
  const [hrefPath] = href.split("?");

  if (hrefPath === "/strategies" || hrefPath === "/strategies/create") {
    return pathname === hrefPath;
  }

  return pathname === hrefPath || pathname.startsWith(hrefPath + "/");
}

export function AppShell({ children }: AppShellProps) {
  const pathname = usePathname();

const [currentUser, setCurrentUser] = useState<Me | null>(null);

  useEffect(() => {
        let cancelled = false;

    apiFetch<Me>("/api/auth/me/", {})
      .then((data) => {
        if (!cancelled) {
          setCurrentUser(data);
        }
      })
      .catch((err) => {
        console.error("Failed to load current user:", err);
        if (!cancelled) {
          setCurrentUser(null);
        }
      })
      .finally(() => {
        // no-op for now; currentUser stays as last resolved value
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const isStaff =
    !!currentUser && (currentUser.is_staff === true || currentUser.is_superuser === true);

  const visibleNavSections: NavSection[] = navSections
    .map((section) => ({
      label: section.label,
      items: section.items.filter((item) => !item.adminOnly || isStaff),
    }))
    .filter((section) => section.items.length > 0);

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        background:
          "radial-gradient(circle at top left, #0b1020, #050713 55%, #030612 100%)",
        color: "#e5f4ff",
      }}
    >
      {/* Sidebar */}
      <aside
        style={{
          width: 240,
          padding: "1.75rem 1.25rem 1.25rem 1.5rem",
          borderRight: "1px solid #111827",
          display: "flex",
          flexDirection: "column",
          gap: "1.5rem",
        }}
      >
        {/* Brand */}
        <div>
          <div
            style={{
              fontSize: "1.4rem",
              fontWeight: 600,
              letterSpacing: 0.5,
            }}
          >
            GuvFX
          </div>
          <div
            style={{
              fontSize: "0.8rem",
              color: "#9ca3af",
              marginTop: 4,
            }}
          >
            Trading Intelligence
          </div>
        </div>

        {/* Navigation sections */}
        <nav
          aria-label="Main navigation"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: "1.25rem",
            flex: 1,
          }}
        >
          {visibleNavSections.map((section) => (
            <div key={section.label}>
              <div
                style={{
                  fontSize: "0.7rem",
                  letterSpacing: 1.4,
                  textTransform: "uppercase",
                  color: "#6b7280",
                  marginBottom: "0.45rem",
                }}
              >
                {section.label}
              </div>
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                }}
              >
                {section.items.map((item) => {
                  const active = isActive(pathname, item.href);
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        padding: "0.4rem 0.6rem",
                        borderRadius: 999,
                        fontSize: "0.85rem",
                        textDecoration: "none",
                        color: active ? "#e5f4ff" : "#9ca3af",
                        background: active
                          ? "linear-gradient(90deg, #1d4ed8, #22c1c3)"
                          : "transparent",
                        boxShadow: active
                          ? "0 0 0 1px rgba(59,130,246,0.35)"
                          : "none",
                        transition:
                          "background 140ms ease, color 140ms ease, box-shadow 140ms ease",
                      }}
                    >
                      <span>{item.label}</span>
                      {active && (
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
          ))}
        </nav>

        {/* User summary / logout placeholder */}
        <div
          style={{
            marginTop: "auto",
            paddingTop: "0.5rem",
            borderTop: "1px solid #111827",
            fontSize: "0.8rem",
            color: "#9ca3af",
          }}
        >
          {/* This can be wired to actual user info later */}
          <div style={{ marginBottom: "0.3rem" }}>Logged in</div>
          <button
            type="button"
            onClick={() => {
              if (typeof window !== "undefined") {
                window.localStorage.removeItem("guvfx_access_token");
                window.location.href = "/login?reason=logged_out";
              }
            }}
            style={{
              width: "100%",
              padding: "0.4rem 0.6rem",
              borderRadius: 999,
              border: "1px solid #1f2937",
              background: "rgba(15,23,42,0.85)",
              color: "#e5f4ff",
              fontSize: "0.78rem",
              cursor: "pointer",
            }}
          >
            Log out
          </button>
        </div>
      </aside>

      {/* Main content */}
      <main
        style={{
          flex: 1,
          padding: "1.75rem 2rem 2.5rem 2rem",
        }}
      >
        {children}
      </main>
    </div>
  );
}
