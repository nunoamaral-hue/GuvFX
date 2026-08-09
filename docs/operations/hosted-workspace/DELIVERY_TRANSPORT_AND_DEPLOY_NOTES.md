# Delivery transport endpoint, deploy mechanics, and the connect-endpoint gap

Status: authoritative operational note (2026-08-09). Companion to
`WORKSPACE_DELIVERY_HOST_CERTIFICATION.md`. Written after the `TerminalNode.rdp_host` correction
(PR #322, merged to `main` `d90c0b8`, deployed to production).

---

## 1. Node identity vs delivery transport — `hostname` is NOT `rdp_host`

Two distinct facts about an execution host must never be conflated:

| Field | Meaning | Consumed by |
|-------|---------|-------------|
| `TerminalNode.hostname` | The **logical execution-node IDENTITY** (e.g. `guvfx-windows-mt5`). Used for node-agreement / routing identity. **Not necessarily an address guacd can dial.** | execution routing (`resolve_hosted_route`, node-agreement), operator display (`node_identity`) |
| `TerminalNode.rdp_host` | The **delivery TRANSPORT endpoint** — the hostname/IP guacd actually dials for RDP/RemoteApp (e.g. `100.79.101.19`). Additive, `blank`/`default=""`. | Workspace Delivery ONLY (`authorize_workspace_delivery` → RemoteApp descriptor `host`) |

Rules (enforced in code + tests):

- The RemoteApp delivery descriptor's RDP host is **`node.rdp_host`**, never `node.hostname`.
- A blank `rdp_host` **fails closed** with `DA_NODE_TRANSPORT_UNCONFIGURED` — there is **no silent fallback**
  to `hostname`. (`DA_NODE_UNASSIGNED` — no node / no hostname — is checked first, so the two gates are
  distinct and ordered.)
- **Execution routing must never read `rdp_host`.** It is delivery-only. `resolve_hosted_route` /
  `authorize_hosted_claim` resolve the node from `execution_node` / `terminal_node`, independent of the
  transport field. (Regression-guarded in `hosted_workspace/tests_delivery_rdp_host.py`.)
- The operator delivery projection exposes both distinctly: `delivery_host = rdp_host` (transport),
  `node_identity = hostname` (identity). Both are operator-only, never customer-facing.

To set the transport for a node without touching its identity:

```python
n = TerminalNode.objects.get(pk=<id>)
n.rdp_host = "<routable-ip-or-host>"   # hostname/identity left unchanged
n.save(update_fields=["rdp_host"])
```

**Legacy note (`Mt5Instance.rdp_host`):** a *separate* model, `mt5.Mt5Instance`, also has an `rdp_host`
used by the **legacy** Guacamole VNC/desktop adapter (`mt5/adapters/guacamole_vnc_adapter.py`). It is a
different mechanism from the ADR-0034 RemoteApp delivery seam. Historic defaults pointed it at
`10.50.0.2`, which is **dead** — see §2.

---

## 2. Deploy mechanics — known traps (production, `/home/ubuntu/guvfx-prod`)

The production deploy directory is **rsync-based, not a git checkout**. Established facts:

- **`docker compose build` is a NO-OP for the backend.** The `guvfx-backend` compose service has **no
  `build:` section** (it only references `image: guvfx-prod-guvfx-backend`). `docker compose build` /
  `up --build` produces **no new image** and silently runs the old one. Always build directly:

  ```bash
  docker build -f backend/Dockerfile --build-arg GIT_COMMIT=<sha> \
    -t guvfx-prod-guvfx-backend:latest backend/
  ```

  Then **verify the image ID actually changed** (RULE 11): compare `docker inspect ...:latest` against the
  pre-build rollback tag before trusting the build.

- **The wayond-listener is not a compose service you can `up --build`.** `docker compose ... up` for the
  listener fails with `depends on undefined service db` (the DB service is `guvfx-postgres`, not `db`). The
  listener runs as a standalone container:

  ```bash
  docker build -f deploy/wayond-listener/Dockerfile \
    --build-arg BACKEND_IMAGE=guvfx-prod-guvfx-backend:latest \
    -t guvfx-wayond-listener:latest deploy/wayond-listener/
  docker run -d --name guvfx-wayond-listener --restart unless-stopped \
    --network guvfx-prod_default --env-file /home/ubuntu/guvfx-prod/wayond-listener.env \
    guvfx-wayond-listener:latest python manage.py run_wayond_listener --live --health-file /tmp/wayond_health
  ```

- **MIGRATE-FIRST**, using the *new* image, before recreating the live container:

  ```bash
  docker compose run --rm --no-deps guvfx-backend python manage.py migrate
  docker compose up -d --force-recreate --no-deps guvfx-backend
  ```

- **Backend env** comes from `env_file: telegram.env, email.env, beta.env, bridge-agent.env`. Feature
  flags live in **`beta.env`**. Back it up before editing (`beta.env.bak.<purpose>-<utc>`); recreate the
  backend to pick up changes.

- **Stale `10.50.0.2` Guacamole guidance is WRONG.** From `guacd` on the VPS, `10.50.0.2:3389` is
  **UNREACHABLE** (dead). The live MT5 host RDP transport is **`100.79.101.19:3389`** (REACHABLE). Any
  operator recovery / VNC-desktop config still pointing at `10.50.0.2` must be repointed to
  `100.79.101.19`. Verify with a positive **and** negative control (RULE 11):

  ```bash
  docker exec guacd nc -z -w5 100.79.101.19 3389   # expect REACHABLE
  docker exec guacd nc -z -w5 10.50.0.2   3389   # expect UNREACHABLE
  ```

---

## 3. Gap — the customer-facing delivery **connect** endpoint does not exist yet

The ADR-0034 delivery seam provides the **authorization** primitive (`authorize_workspace_delivery`,
which mints the signed RemoteApp descriptor) and the read-only **delivery-state** API. It does **not**
provide a customer-facing endpoint that mints and returns the descriptor (`embed_url`) to the owner's
browser: `authorize_workspace_delivery` currently has **zero endpoint callers**, and
`delivery_views.py` states plainly that "minting a delivery is the future onboarding wiring, gated".

Consequence: even with `rdp_host` set, the delivery flags on, and the descriptor proven to mint correctly
(right transport, reachable, no secret leak), **there is no product path** by which a customer clicks
"Open MT5" and receives the RemoteApp window through the new seam. That connect endpoint (owner-authenticated,
returns a **credentialed** signed `embed_url`) plus its minimal frontend is a **distinct, security-sensitive
build** — it needs its own design, adversarial review, and an explicit go-ahead. It is the blocking
prerequisite for an actionable broker-login gate via the ADR-0034 RemoteApp portable path.

The **legacy** Terminal Access path (`trading/terminal-access` → mt5 session orchestration →
`GuacamoleVncAdapter` → `Mt5Instance.rdp_host`) is a separate VNC/desktop mechanism and is **not** the
ADR-0034 RemoteApp portable delivery.
