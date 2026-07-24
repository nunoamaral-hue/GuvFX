# Report C — production bridge network characterisation

`WIN-RD8VDS93DK7`, `2026-07-24T05:33Z`. **Read-only. No firewall, service, or bridge change.** Every
conclusion is demonstrated from captured rule/route/interface state, not inferred.

## Topology

| Interface | idx | metric | IPv4 | category | firewall profile |
|---|---|---|---|---|---|
| **Tailscale** (WinTun) | 6 | **5** (preferred) | `100.79.101.19/32` | Private | Private |
| Ethernet | 7 | 15 | `217.154.45.114/32` (public) | Public | Public |
| Loopback | 1 | 75 | `127.0.0.1/8` | — | — |

Tailnet peers (from `tailscale status`): this node `100.79.101.19 guvfx-windows-mt5`; the backend
`100.119.23.29 guvfx-ubuntu`; plus mac-control-node, n8n-raspberrypi, nas, wayond-ubuntu — **six peers on
the tailnet**, all authenticated to the same tailnet.

## Packet path to the backend

`Find-NetRoute -RemoteIPAddress 100.119.23.29` → **ifIndex 6 (Tailscale), local source `100.79.101.19`.**
Backend↔host traffic rides the Tailscale WinTun adapter, not Ethernet.

## Listener

```
0.0.0.0:8788  pid 13292  (python.exe = the bridge)
```

`0.0.0.0` binds every interface, including the Tailscale IP and loopback. Established connections at
capture time: **0** (the VPS connects on demand; the listener is idle but bound).

## Why the bridge is reachable — PROVEN, not inferred

The earlier loopback hypothesis was wrong. The mechanism is an explicit Tailscale-installed firewall rule:

```
RULE Tailscale-In   profile=Domain, Private   Inbound / Allow
     protocol=Any   localport=Any   remoteport=Any   program=Any
     LocalAddress=100.79.101.19   RemoteAddress=Any
```

This rule matches the bridge's inbound traffic on **every** field: inbound ✓, program=Any covers python.exe
✓, protocol=Any covers TCP ✓, localport=Any covers 8788 ✓, LocalAddress `100.79.101.19` is the destination
of packets arriving on the Tailscale interface ✓, RemoteAddress=Any covers the backend ✓, and the Private
profile matches the Tailscale interface's category ✓.

**No rule is scoped to port 8788 or to the bridge program (verified: 0 such rules).** The bridge is admitted
solely by this broad `Tailscale-In` rule.

## Would `DefaultInboundAction = Block` preserve the bridge? — YES

Two facts settle it:

1. **`NotConfigured` already resolves to Block for inbound.** All three profiles are `NotConfigured`, yet
   only explicitly-allowed inbound reaches services — which is why the bridge depends on `Tailscale-In`.
   The effective inbound default is *already* deny.
2. **An explicit Allow overrides the default Block.** `Tailscale-In` is an explicit Allow, so setting
   Private → Block changes the *default* action for traffic no rule matches, and leaves `Tailscale-In` (and
   therefore the bridge) untouched.

Setting Private → `Block` is therefore **effectively a no-op for connectivity** and preserves the bridge.
It is not a hypothesis: it follows from the rule precedence and the `Tailscale-In` scoping above.

## But the SAME rule over-exposes the future agent port — and `firewall.ps1` correctly refuses it

The beta agent will bind **`8791` on `100.79.101.19`**. `Tailscale-In` (`localport=Any`, `RemoteAddress=Any`)
admits `:8791` from **every** tailnet peer — mac-control-node, nas, wayond-ubuntu, n8n-raspberrypi — not
only the backend. `firewall.ps1`'s pre-existing-danger scan (a rule that covers the port **and** admits a
non-backend remote **and** is broad-program) flags exactly this, and halts:

```
DANGER pre-existing inbound allow rules could expose :8791 beyond the backend
```

That halt is **correct**, and it is a second, independent gate from the `NotConfigured` one. The agent's own
signed-protocol + keyring authentication is defence in depth, but the firewall design is "backend only", and
`Tailscale-In` breaks that at the network layer.

## Risk assessment and recommended minimum-safe change

- **Bridge (8788):** no risk from default-deny. Preserved by `Tailscale-In`, proven above.
- **Agent (8791):** exposed tailnet-wide by `Tailscale-In` unless narrowed.

**Recommendation — do NOT flip the machine-wide profile default.** It is a whole-estate change, it is not
authorised, and it is unnecessary: the effective inbound default is already deny. The surgical fix that
makes the agent backend-only regardless of `Tailscale-In` breadth is a **higher-priority explicit Block**
for `TCP 8791` on `100.79.101.19` from any remote other than `100.119.23.29` — which is exactly the
remediation `firewall.ps1`'s own message proposes ("add a higher-priority block for :8791 from
non-backend"). A block rule outranks the broad `Tailscale-In` allow, so the agent becomes backend-only while
`Tailscale-In` continues to admit the bridge and everything else unchanged.

This needs a sponsor decision and a corresponding change to `firewall.ps1` (add the scoped block before the
allow), because the current script halts rather than emitting the block itself.

## Evidence

Raw capture: `scratchpad/netchar_out.txt` (interfaces, addresses, listener, tailscale status, route, rule
scoping). All commands were `Get-*` / `Find-NetRoute` / `tailscale status` — read-only.
