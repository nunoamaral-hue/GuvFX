import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* Security Headers Configuration
   *
   * These headers provide defense-in-depth protection against common web attacks.
   * See: https://nextjs.org/docs/app/api-reference/config/next-config-js/headers
   */
  async headers() {
    return [
      {
        // Apply to all routes
        source: "/:path*",
        headers: [
          // HSTS: Force HTTPS for 1 year, include subdomains
          {
            key: "Strict-Transport-Security",
            value: "max-age=31536000; includeSubDomains; preload",
          },
          // Prevent clickjacking
          {
            key: "X-Frame-Options",
            value: "DENY",
          },
          // Prevent MIME type sniffing
          {
            key: "X-Content-Type-Options",
            value: "nosniff",
          },
          // Control referrer information
          {
            key: "Referrer-Policy",
            value: "strict-origin-when-cross-origin",
          },
          // Restrict browser features/sensors
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=(), payment=(), usb=(), magnetometer=(), gyroscope=(), accelerometer=()",
          },
          // XSS Protection (legacy, but still useful for older browsers)
          {
            key: "X-XSS-Protection",
            value: "1; mode=block",
          },
          // Content Security Policy (Report-Only mode for MVP)
          // TODO: Move to enforce mode after thorough testing
          {
            key: "Content-Security-Policy-Report-Only",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline' 'unsafe-eval'", // Next.js requires unsafe-eval in dev
              "style-src 'self' 'unsafe-inline'", // Allow inline styles for Next.js
              "img-src 'self' data: https:",
              "font-src 'self' data:",
              "connect-src 'self' https://api.guvfx.com wss://api.guvfx.com",
              "frame-ancestors 'none'",
              "form-action 'self'",
              "base-uri 'self'",
              "object-src 'none'",
            ].join("; "),
          },
        ],
      },
    ];
  },

  // Additional security-related config
  poweredByHeader: false, // Remove X-Powered-By header

  // Ensure all pages use HTTPS redirects in production
  // (handled by Traefik/nginx in our case, but good as fallback)
};

export default nextConfig;
