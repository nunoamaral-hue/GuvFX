const API_BASE = "https://api.guvfx.com";

async function refreshCookiesOnce(): Promise<void> {
  await fetch(`${API_BASE}/api/auth/cookie/refresh/`, {
    method: "POST",
    credentials: "include",
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

  // IMPORTANT: propagate backend error messages (e.g. 409 single-active rule)
  if (!res.ok) {
    const text = await res.text();

    try {
      const data = JSON.parse(text);
      if (data?.detail) throw new Error(String(data.detail));
      if (data?.error) throw new Error(String(data.error));
      throw new Error(text || `Request failed: ${res.status}`);
    } catch {
      throw new Error(text || `Request failed: ${res.status}`);
    }
  }

  return (await res.json()) as T;
}
