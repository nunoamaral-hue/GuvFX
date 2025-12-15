"use client";

import React from "react";
import { Button } from "./ui/Button";
import { Card } from "./ui/Card";

type TokenAuthSectionProps = {
  token: string;
  setToken: (value: string) => void;
};

export const TokenAuthSection: React.FC<TokenAuthSectionProps> = ({
  token,
  setToken,
}) => {
  const handleSaveToken = () => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem("guvfx_access_token", token);
    }
  };

  return (
    <Card
      title="Authentication"
      subtitle="Paste your access token from /api/auth/token/. It will be stored locally in your browser."
      style={{ marginBottom: "1.5rem" }}
    >
      <textarea
        value={token}
        onChange={(e) => setToken(e.target.value.trim())}
        rows={3}
        placeholder="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
        style={{
          width: "100%",
          fontFamily: "monospace",
          fontSize: "0.85rem",
          borderRadius: 8,
          border: "1px solid rgba(148,163,184,0.65)",
          padding: "0.55rem 0.7rem",
          boxSizing: "border-box",
          background: "rgba(3, 7, 18, 0.9)",
          color: "#e5f4ff",
          outline: "none",
        }}
      />
      <div
        style={{
          marginTop: "0.6rem",
          display: "flex",
          justifyContent: "flex-end",
        }}
      >
        <Button onClick={handleSaveToken}>Save token</Button>
      </div>
      <p
        style={{
          marginTop: "0.4rem",
          fontSize: "0.75rem",
          color: "#8fa0b7",
        }}
      >
        Once saved, this token will be reused across Strategies and Backtests
        pages.
      </p>
    </Card>
  );
};