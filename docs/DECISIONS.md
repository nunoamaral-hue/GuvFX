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
- Traefik runs on the shared `traefik-public` docker network as the single TLS entrypoint for `guvfx.com`, `api.guvfx.com`, and `guac.guvfx.com`; certificates managed via Let's Encrypt keep routing uniform.
- Guacamole sits behind `guac.guvfx.com/guacamole/` so the MT5 desktop is always served over HTTPS via Traefik.
- `/srv/guvfx/mt5_handoff` is a setgid host bind mount (owner `10001`, group `1000`, mode `2770`) shared between `/app/.guvfx_handoff` (backend) and `/home/mt5free/.guvfx` (MT5) for JSON configs.
- MT5 automation scripts stay on the host and are triggered via Openbox autostart + `apply-account-config`, which uses `xdotool`/`wmctrl` to fill the MT5 login dialog, so configs can evolve without rebuilding.

## D-001 — XRDP over VNC for MT5
**Decision:** Prefer XRDP/Xorg for MT5 UI and keep VNC (`:99`) as a fallback.
**Reason:** Clipboard/keyboard/window focus behave better under XRDP, multi-monitor support is reliable, and Wine runs stably when attached to XRDP.

## D-002 — Autostart via `startwm.sh`
**Decision:** Launch MT5 from `/etc/xrdp/startwm.sh`.
**Reason:** Ensures MT5 is bound to the proper XRDP display (e.g., `:10`) and avoids Wine attaching to `:99`.

## D-003 — Persist only Wine + scripts
**Decision:** Persist `/home/mt5free/.wine` and `/home/mt5free/bin/autostart-rdp.sh`.
**Reason:** Supports clean container rebuilds and prevents config drift inside the image.

## D-004 — No interactive passwords after rebuild
**Decision:** Unlock the `mt5free` user and keep PAM minimal.
**Reason:** Enables unattended restarts and works with Guacamole’s credential flow without prompting for passwords.
- Decision: Use a single Traefik instance on ports 80/443 (docker `traefik-public` network) as GuvFX’s TLS entrypoint with Let’s Encrypt certificate automation for `guvfx.com`, `api.guvfx.com`, and `guac.guvfx.com`.
- Decision: Serve Guacamole through `guac.guvfx.com/guacamole/` routed by Traefik so the browser-facing MT5 remote desktop always stays behind HTTPS.
- Decision: Share `/srv/guvfx/mt5_handoff` as a host bind mount (owner `10001`, group `1000`, mode `2770` setgid) between `/app/.guvfx_handoff` (backend container) and `/home/mt5free/.guvfx` (MT5 container) so both services access JSON configs without extra overlays.
- Decision: Keep the MT5 automation scripts on the host and trigger them via Openbox autostart + the `apply-account-config` helper, rather than baking the scripts into the image, so configs can be iterated without rebuilding containers.
