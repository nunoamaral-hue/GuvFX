
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

  let res = await doFetch();

  // If access cookie expired, refresh once then retry
  if (res.status === 401) {
    try {
      await refreshCookiesOnce();
      res = await doFetch();
    } catch {
      // fall through
    }
  }

  // Hard redirect only for identity checks
  if (res.status === 401 && typeof window !== "undefined") {
    if (url.includes("/api/auth/me/")) {
      window.location.href = "/login?reason=unauthenticated";
    }
    throw new Error("Unauthorized");
  }

  // IMPORTANT: propagate backend error messages (incl. DRF field errors)
  if (!res.ok) {
    const text = await res.text();

    // Try JSON first (DRF often returns JSON on errors)
    try {
      const data = JSON.parse(text) as unknown;

      if (data && typeof data === "object") {
        const obj = data as Record<string, unknown>;

        // Common DRF shapes
        if (typeof obj.detail === "string") throw new Error(obj.detail);
        if (typeof obj.error === "string") throw new Error(obj.error);

        // Field errors (e.g. { magic_number: ["..."] })
        // Preserve full JSON so the UI can extract the right field.
        throw new Error(JSON.stringify(obj));
      }

      // If JSON parses but isn't an object, fall back
      throw new Error(text || `Request failed: ${res.status}`);
    } catch {
      // Not JSON — return raw text
      throw new Error(text || `Request failed: ${res.status}`);
    }
  }

  return (await res.json()) as T;
}
