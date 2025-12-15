"use client";

import React from "react";

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary";
};

export const Button: React.FC<ButtonProps> = ({
  children,
  variant = "primary",
  style,
  ...props
}) => {
  const base: React.CSSProperties = {
    padding: "0.45rem 1rem",
    borderRadius: 999,
    border: "1px solid transparent",
    fontSize: "0.9rem",
    cursor: props.disabled ? "not-allowed" : "pointer",
    transition: "background 0.15s ease, transform 0.05s ease, opacity 0.15s ease",
  };

  const variants: Record<string, React.CSSProperties> = {
    primary: {
      background:
        "linear-gradient(135deg, #2979ff 0%, #3fe0ff 50%, #2979ff 100%)",
      color: "#ffffff",
      boxShadow: props.disabled
        ? "none"
        : "0 10px 30px rgba(37, 99, 235, 0.45)",
      opacity: props.disabled ? 0.65 : 1,
    },
    secondary: {
      background: "transparent",
      color: "#cbd5f5",
      borderColor: "rgba(148,163,184,0.6)",
      boxShadow: "none",
    },
  };

  return (
    <button
      {...props}
      style={{
        ...base,
        ...(variants[variant] || variants.primary),
        ...(style || {}),
      }}
      onMouseDown={(e) => {
        if (!props.disabled) {
          (e.currentTarget as HTMLButtonElement).style.transform = "scale(0.97)";
        }
        props.onMouseDown?.(e);
      }}
      onMouseUp={(e) => {
        (e.currentTarget as HTMLButtonElement).style.transform = "scale(1)";
        props.onMouseUp?.(e);
      }}
    >
      {children}
    </button>
  );
};