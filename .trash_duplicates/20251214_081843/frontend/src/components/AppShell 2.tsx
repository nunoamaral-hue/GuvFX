"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { apiFetch } from "@/lib/api";

type AppShellProps = {
  children: React.ReactNode;
};

type MeResponse = {
  id: number;
  email: string;
  username: string;
  first_name: string;
  last_name: string;
};

export const AppShell: React.FC<AppShellProps> = ({ children }) => {
  const router = useRouter();
  const pathname = usePathname();

  const [user, setUser] = useState<MeResponse | null>(null);
  const [userLoading, setUserLoading] = useState<boolean>(true);

  // Basic auth guard + fetch /auth/me
  useEffect(() => {
    if (typeof window === "undefined") return;

    const token = window.localStorage.getItem("guvfx_access_token");

    if (!token) {
      setUser(null);
      setUserLoading(false);
      router.push("/login");
      return;
    }

    const fetchUser = async () => {
      try {
        setUserLoading(true);
        const me = await apiFetch<MeResponse>("/api/auth/me/", {}, token);
        setUser(me);
      } catch (err) {
        console.error("Failed to fetch /api/auth/me/:", err);
        // Token might be invalid/expired – clear and force login
        window.localStorage.removeItem("guvfx_access_token");
        window.localStorage.removeItem("guvfx_refresh_token");
        setUser(null);
        router.push("/login");
      } finally {
        setUserLoading(false);
      }
    };

    fetchUser();
  }, [router]);

  const navItems = [
    { label: "Strategies", href: "/strategies" },
    { label: "Backtests", href: "/backtests" },
    { label: "Accounts", href: "/accounts" },
    { label: "Profile", href: "/profile" },
    // future:
    // { label: "Analytics", href: "/analytics" },
  ];

  const isActive = (href: string) => {
    if (!pathname) return false;
    return pathname === href || pathname.startsWith(`${href}/`);
  };

  const handleLogout = () => {
    if (typeof window !== "undefined") {
      window.localStorage.removeItem("guvfx_access_token");
      window.localStorage.removeItem("guvfx_refresh_token");
    }
    setUser(null);
    router.push("/login");
  };

  const displayName = (() => {
    if (!user) return "";
    if (user.first_name || user.last_name) {
      return `${user.first_name || ""} ${user.last_name || ""}`.trim();
    }
    if (user.username) return user.username;
    return user.email;
  })();

  const truncatedEmail =
    user?.email && user.email.length > 26
      ? user.email.slice(0, 23) + "..."
      : user?.email || "";

  return (
    <div
      style={{
        minHeight: "100vh",
        width: "100%",
        display: "flex",
        background:
          "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)",
        color: "#e5f4ff",
        fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
      }}
    >
      {/* Sidebar */}
      <aside
        style={{
          width: 220,
          borderRight: "1px solid rgba(255,255,255,0.06)",
          padding: "1.8rem 1.4rem",
          boxSizing: "border-box",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
        }}
      >
        <div>
          {/* Brand */}
          <div style={{ marginBottom: "2rem" }}>
            <Link href="/" style={{ textDecoration: "none" }}>
              <div
                style={{
                  fontSize: "1.2rem",
                  fontWeight: 600,
                  background:
                    "linear-gradient(120deg, #4ab3ff 0%, #7af0ff 40%, #4ab3ff 100%)",
                  WebkitBackgroundClip: "text",
                  WebkitTextFillColor: "transparent",
                }}
              >
                GuvFX
              </div>
              <div
                style={{
                  fontSize: "0.75rem",
                  color: "#8fa0b7",
                  marginTop: "0.25rem",
                }}
              >
                Trading Intelligence
              </div>
            </Link>
          </div>

          {/* Navigation */}
          <nav style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {navItems.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                style={{
                  textDecoration: "none",
                  fontSize: "0.9rem",
                  padding: "0.45rem 0.7rem",
                  borderRadius: 8,
                  color: isActive(item.href) ? "#e9f4ff" : "#9bb0c6",
                  background: isActive(item.href)
                    ? "rgba(74,179,255,0.18)"
                    : "transparent",
                  border: isActive(item.href)
                    ? "1px solid rgba(74,179,255,0.5)"
                    : "1px solid transparent",
                  transition: "background 0.15s ease, color 0.15s ease",
                }}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        </div>

        {/* User section + logout */}
        <div>
          <div
            style={{
              marginBottom: "0.6rem",
              padding: "0.55rem 0.6rem",
              borderRadius: 10,
              background: "rgba(15,23,42,0.9)",
              border: "1px solid rgba(148,163,184,0.4)",
              fontSize: "0.78rem",
            }}
          >
            {userLoading && (
              <div style={{ color: "#9bb0c6" }}>Loading user…</div>
            )}

            {!userLoading && user && (
              <>
                <div style={{ color: "#9bb0c6", marginBottom: 2 }}>
                  Logged in as
                </div>
                <div
                  style={{
                    fontSize: "0.82rem",
                    fontWeight: 500,
                    color: "#e5f4ff",
                    marginBottom: 2,
                  }}
                >
                  {displayName}
                </div>
                <div
                  style={{
                    fontSize: "0.75rem",
                    color: "#7c8ca4",
                    wordBreak: "break-all",
                  }}
                >
                  {truncatedEmail}
                </div>
              </>
            )}

            {!userLoading && !user && (
              <div style={{ color: "#9bb0c6" }}>Not authenticated</div>
            )}
          </div>

          <button
            onClick={handleLogout}
            style={{
              width: "100%",
              padding: "0.45rem 0.7rem",
              borderRadius: 8,
              border: "1px solid rgba(255,255,255,0.2)",
              background: "transparent",
              color: "#b7c5dd",
              fontSize: "0.85rem",
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
          padding: "2rem 3rem",
          boxSizing: "border-box",
          overflowX: "hidden",
        }}
      >
        {children}
      </main>
    </div>
  );
};