"use client";

import React from "react";

type CardProps = {
  children: React.ReactNode;
  title?: string;
  subtitle?: string;
  style?: React.CSSProperties;
};

export const Card: React.FC<CardProps> = ({ children, title, subtitle, style }) => {
  return (
    <section
      style={{
        borderRadius: 12,
        padding: "1.25rem 1.5rem",
        background: "rgba(7, 12, 30, 0.96)",          // <<< dark panel background
        border: "1px solid rgba(148, 163, 184, 0.35)", // subtle slate border
        boxShadow: "0 18px 45px rgba(0, 0, 0, 0.6)",   // soft dark shadow
        marginBottom: "1rem",
        ...(style || {}),
      }}
    >
      {(title || subtitle) && (
        <header style={{ marginBottom: "0.6rem" }}>
          {title && (
            <h2
              style={{
                fontSize: "1.05rem",
                margin: 0,
                marginBottom: subtitle ? "0.15rem" : 0,
                color: "#e9f4ff", // bright heading
              }}
            >
              {title}
            </h2>
          )}
          {subtitle && (
            <p
              style={{
                margin: 0,
                fontSize: "0.85rem",
                color: "#8fa0b7",
              }}
            >
              {subtitle}
            </p>
          )}
        </header>
      )}
      {children}
    </section>
  );
};