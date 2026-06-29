"use client";

import React from "react";

type BadgeProps = {
  children: React.ReactNode;
  color?: "green" | "gray" | "blue" | "red" | "yellow";
  style?: React.CSSProperties;
};

export const Badge: React.FC<BadgeProps> = ({ children, color = "gray", style }) => {
  const colors: Record<string, React.CSSProperties> = {
    green: {
      backgroundColor: "rgba(34,197,94,0.15)",
      color: "#86efac",
      borderColor: "rgba(34,197,94,0.3)",
    },
    gray: {
      backgroundColor: "rgba(148,163,184,0.12)",
      color: "#94a3b8",
      borderColor: "rgba(148,163,184,0.25)",
    },
    blue: {
      backgroundColor: "rgba(59,130,246,0.15)",
      color: "#93c5fd",
      borderColor: "rgba(59,130,246,0.3)",
    },
    red: {
      backgroundColor: "rgba(239,68,68,0.15)",
      color: "#fca5a5",
      borderColor: "rgba(239,68,68,0.3)",
    },
    yellow: {
      backgroundColor: "rgba(245,158,11,0.15)",
      color: "#fcd34d",
      borderColor: "rgba(245,158,11,0.3)",
    },
  };

  return (
    <span
      style={{
        fontSize: "0.75rem",
        padding: "0.15rem 0.6rem",
        borderRadius: 999,
        borderWidth: 1,
        borderStyle: "solid",
        display: "inline-block",
        ...(colors[color] || colors.gray),
        ...style,
      }}
    >
      {children}
    </span>
  );
};