// src/lib/api.ts
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
  accessToken?: string
): Promise<T> {
  const url = `${API_BASE}${path}`;

  // Normalise headers into a simple string map
  const baseHeaders: Record<string, string> = {
    "Content-Type": "application/json",
  };

  if (options.headers) {
    // Merge any headers passed into options
    const optHeaders = options.headers as Record<string, string>;
    for (const [key, value] of Object.entries(optHeaders)) {
      baseHeaders[key] = value;
    }
  }

  if (accessToken) {
    baseHeaders["Authorization"] = `Bearer ${accessToken}`;
  }

  const res = await fetch(url, {
    ...options,
    headers: baseHeaders, // OK: Record<string,string> is valid HeadersInit
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API error ${res.status}: ${text}`);
  }

  return res.json() as Promise<T>;
}