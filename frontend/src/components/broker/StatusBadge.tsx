"use client";

import React from "react";
import { Badge } from "@/components/ui/Badge";
import type { StatusView } from "@/lib/broker-status";

/** WP4.2 — renders a mapped StatusView (from lib/broker-status) as a coloured badge. Components pass a
 * StatusView, never a raw backend enum string, so the label/colour mapping stays in one place. */
export const StatusBadge: React.FC<{ view: StatusView; title?: string }> = ({ view, title }) => (
  <Badge color={view.color}>
    <span title={title} aria-label={title || view.label}>{view.label}</span>
  </Badge>
);
