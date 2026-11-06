import type { MetadataRoute } from "next";

/**
 * Robots policy (Stream C / ADR-0080).
 *
 * The public broker surface — directory, broker profile, withdrawal-intelligence page, WTM preview and case study —
 * is DARK: the backend 404s every one of these endpoints until its feature flag is deliberately armed, and even when
 * armed each broker page requires editorial publication. While DARK a crawler could still reach the static page shell
 * (HTTP 200, no data), so we explicitly DISALLOW the whole `/brokers` tree and the raw `/api/` surface from indexing.
 * The rest of the marketing site stays crawlable. This is additive (no robots policy existed before) and prevents an
 * unpublished broker page from being indexed; publishing a broker later is a deliberate step that would relax this.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: "*",
        allow: "/",
        disallow: ["/brokers", "/api/"],
      },
    ],
  };
}
