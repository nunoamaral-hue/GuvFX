"use client";

import React from "react";
import { StatusBadge } from "@/components/broker/StatusBadge";
import { categoryView, resolutionView, severityView } from "@/lib/operations-status";

/** WP5.3 — the reusable operational badges. Each is a thin wrapper over the shared StatusBadge and a
 * lib mapper (operations-status.ts) — no colour mapping is duplicated here, and no raw backend enum is
 * ever rendered. */

export const SeverityBadge: React.FC<{ severity: string; title?: string }> = ({ severity, title }) => (
  <StatusBadge view={severityView(severity)} title={title} />
);

export const CategoryBadge: React.FC<{ category: string; title?: string }> = ({ category, title }) => (
  <StatusBadge view={categoryView(category)} title={title} />
);

export const ResolutionBadge: React.FC<{ resolved: boolean; title?: string }> = ({ resolved, title }) => (
  <StatusBadge view={resolutionView(resolved)} title={title} />
);
