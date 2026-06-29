"use client";

export default function UsagePage() {
  return (
    <div style={{ maxWidth: 900, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Usage</h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1.5rem" }}>
        Monitor your platform usage and resource consumption.
      </p>

      <div
        style={{
          borderRadius: 16,
          border: "1px solid rgba(74, 179, 255, 0.12)",
          background:
            "linear-gradient(135deg, rgba(10, 15, 40, 0.95) 0%, rgba(5, 8, 22, 0.98) 100%)",
          padding: "2rem",
          textAlign: "center",
          color: "#8fa0b7",
          fontSize: "0.9rem",
        }}
      >
        <p style={{ margin: 0 }}>
          Usage analytics will be available once tracking is enabled.
        </p>
      </div>
    </div>
  );
}
