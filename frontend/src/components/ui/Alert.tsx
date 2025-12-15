"use client";

import React from "react";

type AlertProps = {
  children: React.ReactNode;
  type?: "error" | "info";
};

export const Alert: React.FC<AlertProps> = ({ children, type = "info" }) => {
  const styles: Record<string, React.CSSProperties> = {
    error: {
      backgroundColor: "#ffecec",
      borderColor: "#f3b3b3",
      color: "#8b0000",
    },
    info: {
      backgroundColor: "#eef5ff",
      borderColor: "#c2d3ff",
      color: "#10418a",
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