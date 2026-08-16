// Guacamole embed hygiene — shared between Terminal Access (legacy viewer + hosted RemoteApp) and the hosted
// onboarding journey. Moved verbatim from trading/terminal-access/page.tsx so the hosted RemoteApp component
// can be embedded in more than one surface without duplicating this logic. NO behaviour change.
//
// ─────────────────────────────────────────────────────────────────────
// TX-RDP3 — Guacamole clean-auth before embed
//
// The Guacamole webapp persists its session (GUAC_AUTH / GUAC_HISTORY /
// GUAC_PREFERENCES) in localStorage on the IFRAME ORIGIN. A stale session
// there — a prior json-auth session that ended up with no connections, or a
// PostgreSQL admin (Gadmin) login — SHADOWS the fresh per-launch ?data=
// token: the webapp reuses the stale session, so the deep-link to
// mt5-terminal/json resolves against a connectionless session and Guacamole
// reports "The requested connection does not exist" (TX-RDP2).
//
// Fix (two parts, both required):
//   1. Pin the embed to THIS app's origin. The backend hard-codes
//      www.guvfx.com; when the user is on guvfx.com that forces the iframe
//      cross-origin, so the parent can neither share nor clear its storage.
//      Rewriting the origin to window.location.origin makes the iframe
//      same-origin → its localStorage IS this document's localStorage.
//   2. Clear the stale Guacamole keys immediately before (re)mounting, so the
//      fresh ?data= token is the first and only active auth state.
// Protocol-agnostic: only touches auth/session storage + the embed origin, so
// it is safe for the legacy VNC path and the future dedicated RDP path alike.
// No token/secret is read or logged — keys are removed by name only.
// ─────────────────────────────────────────────────────────────────────

import type { SafeLaunchDescriptor } from "@/types/mt5-interaction";

const GUAC_STORAGE_KEYS = ["GUAC_AUTH", "GUAC_HISTORY", "GUAC_PREFERENCES"];

// Rewrite the embed URL's origin to this app's origin (string-swap the origin
// prefix only, so the #/client/<id>?data=<token> fragment — and its
// percent-encoding — is preserved exactly).
export function pinEmbedToAppOrigin(embedUrl: string): string {
  if (typeof window === "undefined") return embedUrl;
  try {
    const embedOrigin = new URL(embedUrl).origin;
    if (
      embedOrigin &&
      embedOrigin !== window.location.origin &&
      embedUrl.startsWith(embedOrigin)
    ) {
      return window.location.origin + embedUrl.slice(embedOrigin.length);
    }
  } catch {
    /* malformed URL — fall back to the original */
  }
  return embedUrl;
}

// Drop any stale Guacamole session on this origin (best-effort; storage may be
// unavailable in private mode).
export function clearStaleGuacSession(): void {
  if (typeof window === "undefined") return;
  try {
    for (const key of GUAC_STORAGE_KEYS) window.localStorage.removeItem(key);
  } catch {
    /* storage blocked — nothing else we can safely do client-side */
  }
}

// Returns a descriptor whose iframe is guaranteed to boot from a clean auth
// state: the embed is pinned to this app's origin and any stale Guacamole
// session on that origin is cleared. Apply at every point an embed_url enters
// viewer state (initial launch AND reconnect/resume).
export function withCleanGuacAuth(descriptor: SafeLaunchDescriptor): SafeLaunchDescriptor {
  if (!descriptor?.embed_url) return descriptor;
  const embed_url = pinEmbedToAppOrigin(descriptor.embed_url);
  // Clear targets this document's origin, which (after pinning) is exactly the
  // origin the iframe will load from — so the stale session it would otherwise
  // reuse is gone before the ?data= token boots.
  clearStaleGuacSession();
  return { ...descriptor, embed_url };
}
