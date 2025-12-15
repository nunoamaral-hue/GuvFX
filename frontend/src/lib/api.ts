// src/lib/api.ts
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function apiFetch<T>(
  url: string,
  init: RequestInit = {},
  token?: string
): Promise<T> {
  const baseHeaders: Record<string, string> = {};

  // Normalise headers from init into a plain object
  if (init.headers instanceof Headers) {
    init.headers.forEach((value, key) => {
      baseHeaders[key] = value;
    });
  } else if (Array.isArray(init.headers)) {
    for (const [key, value] of init.headers) {
      baseHeaders[key] = value;
    }
  } else if (init.headers) {
    Object.assign(baseHeaders, init.headers as Record<string, string>);
  }

  // If there is a body and no explicit content type, set JSON content type
  if (
    init.body &&
    !(init.body instanceof FormData) &&
    !baseHeaders["Content-Type"]
  ) {
    baseHeaders["Content-Type"] = "application/json";
  }

  if (token) {
    baseHeaders["Authorization"] = `Bearer ${token}`;
  }

  const fullUrl = url.startsWith("http://") || url.startsWith("https://")
    ? url
    : `${API_BASE}${url}`;

  const res = await fetch(fullUrl, {
    ...init,
    headers: baseHeaders,
    credentials: "include",
  });

  // 🔒 Centralized auth failure handling

  // 401: Unauthorized → treat as token expired/invalid and log the user out
  if (res.status === 401) {
    let reason = "session_expired";

    try {
      const data = await res.json();
      if (data && typeof data === "object" && "code" in data) {
        const code = (data as { code?: string }).code;
        if (code === "token_not_valid") {
          reason = "token_expired";
        }
      }
    } catch {
      // Ignore JSON parse errors for 401, fall back to generic reason
    }

    if (typeof window !== "undefined") {
      window.localStorage.removeItem("guvfx_access_token");
      window.location.href = `/login?reason=${reason}`;
    }

    throw new Error("Unauthorized");
  }

  // 403: Forbidden → do NOT log the user out, just surface an error to the page
  if (res.status === 403) {
    const text = await res.text();
    if (!text) {
      throw new Error("Forbidden");
    }
    throw new Error(text);
  }

  // Generic error handling
  const text = await res.text();

  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${text}`);
  }

  if (!text) {
    // @ts-expect-error allow void responses
    return null;
  }

  return JSON.parse(text) as T;
}