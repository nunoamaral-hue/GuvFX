"use client";

import React from "react";

type BadgeProps = {
  children: React.ReactNode;
  color?: "green" | "gray" | "blue" | "red";
};

export const Badge: React.FC<BadgeProps> = ({ children, color = "gray" }) => {
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
      }}
    >
      {children}
    </span>
  );
};