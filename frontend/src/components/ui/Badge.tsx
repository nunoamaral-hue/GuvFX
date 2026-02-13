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
      backgroundColor: "#e5f9e7",
      color: "#1b7c3a",
      borderColor: "#b5e3bd",
    },
    gray: {
      backgroundColor: "#f3f3f3",
      color: "#555",
      borderColor: "#dddddd",
    },
    blue: {
      backgroundColor: "#e5f0ff",
      color: "#1a4fbf",
      borderColor: "#b4c7f2",
    },
    red: {
      backgroundColor: "#fee2e2",
      color: "#b91c1c",
      borderColor: "#fecaca",
    },
    yellow: {
      backgroundColor: "#fef3c7",
      color: "#92400e",
      borderColor: "#fcd34d",
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