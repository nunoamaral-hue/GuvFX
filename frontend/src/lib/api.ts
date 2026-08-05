const API_BASE = "https://api.guvfx.com";

function getCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp(`(?:^|; )${name.replace(/[$()*+./?[\\\]^{|}-]/g, "\\$&")}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

async function ensureCsrfCookieOnce(): Promise<void> {
  // This endpoint sets the CSRF cookie for subsequent POSTs.
  await fetch(`${API_BASE}/api/auth/cookie/csrf/`, {
    method: "GET",
    credentials: "include",
  });
}

async function refreshCookiesOnce(): Promise<void> {
  // Refresh endpoint may require CSRF depending on backend settings.
  await ensureCsrfCookieOnce();

  const csrf = getCookie("csrftoken");
  await fetch(`${API_BASE}/api/auth/cookie/refresh/`, {
    method: "POST",
    credentials: "include",
    headers: csrf ? { "X-CSRFToken": csrf } : undefined,
  });
}

export async function apiFetch<T>(
  path: string,
  opts: RequestInit = {}
): Promise<T> {
  const url = path.startsWith("http") ? path : `${API_BASE}${path}`;

  const doFetch = async () => {
    const headers: Record<string, string> = {
      ...(opts.headers as Record<string, string> | undefined),
    };

    // Ensure JSON content type when body is a string
    if (typeof opts.body === "string" && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }

    const method = (opts.method || "GET").toUpperCase();
    const needsCsrf = !["GET", "HEAD", "OPTIONS"].includes(method);
    if (needsCsrf) {
      // Ensure CSRF cookie exists, then attach it.
      // (No-op if backend exempts the endpoint.)
      await ensureCsrfCookieOnce();
      const csrf = getCookie("csrftoken");
      if (csrf && !headers["X-CSRFToken"]) headers["X-CSRFToken"] = csrf;
    }

    return fetch(url, {
      ...opts,
      headers,
      credentials: "include",
    });
  };

  // A transport-level failure (dropped/reset connection, DNS, CORS, an aborted or gunicorn-killed request)
  // rejects with a raw `TypeError: "Failed to fetch"`. Tag it as `kind: "network"` with a non-leaky code so
  // no caller (or `toCustomerError`) can ever surface the raw exception text to a customer.
  let res: Response;
  try {
    res = await doFetch();
  } catch (e) {
    const err = new Error("network_unreachable") as Error & { kind?: string; cause?: unknown };
    err.kind = "network";
    err.cause = e;
    throw err;
  }

  // If access cookie expired, refresh once then retry
  if (res.status === 401) {
    try {
      await refreshCookiesOnce();
      res = await doFetch();
    } catch {
      // fall through (network/refresh failure) — the 401 handling below still applies
    }
  }

  // Session is fully dead (access + refresh both invalid).
  // Redirect to login so the user can re-authenticate.
  // Uses replace() so the protected page is removed from browser history —
  // pressing Back after session expiry won't land on the gated page.
  if (res.status === 401 && typeof window !== "undefined") {
    const reason = url.includes("/api/auth/me/") ? "unauthenticated" : "expired";
    window.location.replace(`/login?reason=${reason}`);
    // Throw to abort any calling code while redirect is in flight
    throw new Error("Unauthorized");
  }

  // IMPORTANT: propagate backend error messages (incl. DRF field errors) AND the machine-readable
  // parts (HTTP status + parsed body) so callers can branch on a reason code rather than string
  // matching (IPR Area D). The parsed body is exposed as `err.body`; the numeric HTTP status as both
  // `err.status` (fixes the pre-existing `e?.status` checks that were always undefined) and the
  // explicit alias `err.httpStatus`. The display `message` is the cleanest available string.
  if (!res.ok) {
    const text = await res.text();

    let body: unknown = undefined;
    try {
      body = JSON.parse(text); // DRF often returns JSON on errors
    } catch {
      body = undefined; // not JSON — leave undefined, fall back to raw text below
    }

    let message: string;
    if (body && typeof body === "object") {
      const obj = body as Record<string, unknown>;
      // Common DRF shapes
      if (typeof obj.detail === "string") message = obj.detail;
      else if (typeof obj.error === "string") message = obj.error;
      // Field errors (e.g. { magic_number: ["..."] }) — preserve full JSON so the UI can extract
      // the right field (unchanged behaviour for those callers).
      else message = JSON.stringify(obj);
    } else {
      message = text || `Request failed: ${res.status}`;
    }

    const err = new Error(message) as Error & {
      status?: number;
      httpStatus?: number;
      body?: unknown;
    };
    err.status = res.status;
    err.httpStatus = res.status;
    err.body = body;
    throw err;
  }

  // Some endpoints (e.g. DELETE) return 204 No Content (empty body).
  // Avoid calling res.json() on an empty response.
  if (res.status === 204 || res.status === 205) {
    return undefined as unknown as T;
  }

  const contentType = res.headers.get("content-type") || "";
  const text = await res.text();

  if (!text) {
    return undefined as unknown as T;
  }

  // Prefer JSON when the server says it's JSON; otherwise return raw text.
  if (contentType.includes("application/json")) {
    return JSON.parse(text) as T;
  }

  return text as unknown as T;
}