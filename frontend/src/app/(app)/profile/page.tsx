"use client";

import type React from "react";
import { useEffect, useState } from "react";
import { Card } from "@/components/ui/Card";
import { Alert } from "@/components/ui/Alert";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";

type MeResponse = {
  id: number;
  email: string;
  username: string;
  first_name: string;
  last_name: string;
};

type ChangePasswordResponse = {
  detail: string;
};

export default function ProfilePage() {
  const [accessToken, setAccessToken] = useState<string>("");
  const [me, setMe] = useState<MeResponse | null>(null);
  const [loadingMe, setLoadingMe] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [oldPassword, setOldPassword] = useState("");
  const [newPassword1, setNewPassword1] = useState("");
  const [newPassword2, setNewPassword2] = useState("");
  const [pwLoading, setPwLoading] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwSuccess, setPwSuccess] = useState<string | null>(null);

  const labelStyle: React.CSSProperties = {
    color: "#8fa0b7",
    fontSize: "0.85rem",
    marginRight: 4,
  };

  const valueStyle: React.CSSProperties = {
    color: "#e9f4ff",
    fontSize: "0.9rem",
  };

  // Load token
  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      }
    }
  }, []);

  // Fetch /me
  useEffect(() => {
    

    const fetchMe = async () => {
      setLoadingMe(true);
      setError(null);
      try {
        const data = await apiFetch<MeResponse>("/api/auth/me/", {});
        setMe(data);
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error ? err.message : "Failed to load profile.";
        setError(message);
      } finally {
        setLoadingMe(false);
      }
    };

    fetchMe();
  }, [accessToken]);

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPwError(null);
    setPwSuccess(null);

    if (!newPassword1 || newPassword1.length < 8) {
      setPwError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword1 !== newPassword2) {
      setPwError("New passwords do not match.");
      return;
    }
    if (!accessToken) {
      setPwError("");
      return;
    }

    setPwLoading(true);
    try {
      const body = {
        old_password: oldPassword,
        new_password: newPassword1,
      };

      const res = await apiFetch<ChangePasswordResponse>(
        "/api/auth/change-password/",
        {
          method: "POST",
          body: JSON.stringify(body),
        }
);

      setPwSuccess(res.detail || "Password updated successfully.");
      setOldPassword("");
      setNewPassword1("");
      setNewPassword2("");
    } catch (err: unknown) {
      console.error(err);
      const message =
        err instanceof Error
          ? err.message
          : "Failed to change password. Please check your old password and try again.";
      setPwError(message);
    } finally {
      setPwLoading(false);
    }
  };

  return (
      <div style={{ maxWidth: 900, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Profile</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          View your GuvFX account details and update your password.
        </p>

        {error && <Alert type="error">{error}</Alert>}

        {/* Profile details */}
        <Card title="Account Details">
          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              
            </p>
          )}

          {loadingMe && <p>Loading profile…</p>}

          {me && (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                gap: "0.4rem 1.5rem",
              }}
            >
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>ID:</span>
                <span style={valueStyle}>{me.id}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>Email:</span>
                <span style={valueStyle}>{me.email}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>Username:</span>
                <span style={valueStyle}>{me.username}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>First name:</span>
                <span style={valueStyle}>{me.first_name || "—"}</span>
              </p>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>Last name:</span>
                <span style={valueStyle}>{me.last_name || "—"}</span>
              </p>
            </div>
          )}
        </Card>

        {/* Billing UI → /account/billing · Hosting UI → /account/hosting (hidden from nav) */}

        {/* Password change */}
        <Card
          title="Change Password"
          subtitle="Update your GuvFX account password. You’ll need your current password."
        >
          {pwError && <Alert type="error">{pwError}</Alert>}
          {pwSuccess && <Alert type="info">{pwSuccess}</Alert>}

          <form onSubmit={handleChangePassword}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr",
                gap: "0.75rem",
              }}
            >
              <div>
                <label
                  htmlFor="old-password"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Current password
                </label>
                <input
                  id="old-password"
                  type="password"
                  required
                  value={oldPassword}
                  onChange={(e) => setOldPassword(e.target.value)}
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>

              <div>
                <label
                  htmlFor="new-password1"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  New password
                </label>
                <input
                  id="new-password1"
                  type="password"
                  required
                  value={newPassword1}
                  onChange={(e) => setNewPassword1(e.target.value)}
                  placeholder="At least 8 characters"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>

              <div>
                <label
                  htmlFor="new-password2"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Confirm new password
                </label>
                <input
                  id="new-password2"
                  type="password"
                  required
                  value={newPassword2}
                  onChange={(e) => setNewPassword2(e.target.value)}
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                />
              </div>
            </div>

            <div
              style={{
                marginTop: "0.9rem",
                display: "flex",
                justifyContent: "flex-end",
              }}
            >
              <Button type="submit" disabled={pwLoading || !accessToken}>
                {pwLoading ? "Updating password…" : "Update password"}
              </Button>
            </div>
          </form>
        </Card>
      </div>
  );
}
