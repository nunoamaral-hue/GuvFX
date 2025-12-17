# Decision Log (ADR-style)

Record decisions that affect architecture, security, or major UX.

## Template
- Date:
- Decision:
- Context:
- Options considered:
- Pros/Cons:
- Decision:
- Consequences:

## 2025-12-16: VPS production routing & MT5 handoff
- Decision: Use a single Traefik instance on ports 80/443 (docker `traefik-public` network) as GuvFX’s TLS entrypoint with Let’s Encrypt certificate automation for `guvfx.com`, `api.guvfx.com`, and `guac.guvfx.com`.
- Decision: Serve Guacamole through `guac.guvfx.com/guacamole/` routed by Traefik so the browser-facing MT5 remote desktop always stays behind HTTPS.
- Decision: Share `/srv/guvfx/mt5_handoff` as a host bind mount (owner `10001`, group `1000`, mode `2770` setgid) between `/app/.guvfx_handoff` (backend container) and `/home/mt5free/.guvfx` (MT5 container) so both services access JSON configs without extra overlays.
- Decision: Keep the MT5 automation scripts on the host and trigger them via Openbox autostart + the `apply-account-config` helper, rather than baking the scripts into the image, so configs can be iterated without rebuilding containers.
