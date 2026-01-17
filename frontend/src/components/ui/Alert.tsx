"use client";

import React from "react";

type AlertProps = {
  children: React.ReactNode;
  type?: "error" | "info";
};

export const Alert: React.FC<AlertProps> = ({ children, type = "info" }) => {
  const styles: Record<string, React.CSSProperties> = {
    error: {
      backgroundColor: "rgba(248,113,113,0.10)",
      borderColor: "rgba(248,113,113,0.45)",
      color: "#fecaca",
    },
    info: {
      backgroundColor: "rgba(59,130,246,0.12)",
      borderColor: "rgba(59,130,246,0.45)",
      color: "#e5f4ff",
    },
  };

  return (
    <div
      style={{
        borderRadius: 6,
        padding: "0.75rem 1rem",
        borderWidth: 1,
        borderStyle: "solid",
        fontSize: "0.9rem",
        marginBottom: "1rem",
        ...(styles[type] || styles.info),
      }}
    >
      {children}
    </div>
  );
};