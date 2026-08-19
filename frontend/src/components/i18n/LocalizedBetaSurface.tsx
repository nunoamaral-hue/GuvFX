"use client";

import { Children, cloneElement, isValidElement, type ReactElement, type ReactNode } from "react";
import type { Lang } from "@/lib/i18n";
import { localizeActiveBetaCopy } from "@/lib/active-beta-i18n";

const CUSTOMER_COPY_PROPS = new Set([
  "aria-label",
  "alt",
  "body",
  "confirmationBody",
  "description",
  "info",
  "label",
  "message",
  "note",
  "placeholder",
  "subtitle",
  "text",
  "title",
]);

function localizeNode(lang: Lang, node: ReactNode): ReactNode {
  if (typeof node === "string") return localizeActiveBetaCopy(lang, node);
  if (Array.isArray(node)) return Children.map(node, (child) => localizeNode(lang, child));
  if (!isValidElement(node)) return node;

  const element = node as ReactElement<Record<string, unknown>>;
  const props = { ...element.props };
  for (const [name, value] of Object.entries(props)) {
    if (name === "children") props.children = localizeNode(lang, value as ReactNode);
    else if (CUSTOMER_COPY_PROPS.has(name) && typeof value === "string") {
      props[name] = localizeActiveBetaCopy(lang, value);
    }
  }
  return cloneElement(element, props);
}

/**
 * Render-time adapter for legacy active-beta pages.
 *
 * This transforms React text/accessible-copy props before DOM creation. It does
 * not mutate the DOM, reload the page, touch API data, or remount descendants.
 */
export function LocalizedBetaSurface({ lang, children }: { lang: Lang; children: ReactNode }) {
  return <>{localizeNode(lang, children)}</>;
}
