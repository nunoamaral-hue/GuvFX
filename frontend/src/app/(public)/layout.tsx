/**
 * Public Layout — No AppShell, no sidebar, no auth context.
 *
 * Routes under (public)/ are marketing/legal pages accessible without login:
 *   /login, /register, /how-it-works
 *
 * The root landing page (/) lives at app/page.tsx (outside both groups).
 */
export default function PublicLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}
