# Hosted Workspace UX — Roadmap (DEFINITION ONLY, not implemented)

Status: **DEFINITION**. This is the next product workstream **after** Customer Zero certification. Nothing
here is implemented in this packet. It exists so the post-certification UX work is scoped and prioritised.
None of these items is a certification blocker; the certified path (portable RemoteApp, single session,
AppLocker Enforce, execution DARK) is unaffected by any of them.

## Guiding constraints (inherited, non-negotiable)
- MT5-only surface: no change may reintroduce a desktop/shell or widen the AppLocker boundary.
- Delivery vs execution authority stays separated; UX changes touch **delivery/presentation only**.
- Execution stays gated by the backend arming chain + live bridge gate regardless of UX.

## Roadmap items

| # | Item | What it means | Layer | Notes / dependencies |
|---|------|---------------|-------|----------------------|
| R1 | **Fullscreen MT5** | A one-click fullscreen for the RemoteApp iframe (Fullscreen API on the embed container). | Frontend | Pure presentation; no backend change. |
| R2 | **Expand / collapse** | Collapse surrounding GuvFX chrome to maximise the terminal area. | Frontend | Complements R1. |
| R3 | **Auto-resize** | Propagate the iframe/container size to the RDP session so MT5 reflows (Guacamole `resize-method=display-update` already set; wire the resize events). | Frontend + guac param | Verify no reconnect churn. |
| R4 | **Browser-responsive sizing** | Sensible layout at tablet/laptop/desktop widths; the terminal never overflows the viewport. | Frontend | Ties into R3. |
| R5 | **Hide surrounding navigation** | An immersive "workspace mode" that hides the app nav while in MT5. | Frontend | Must keep an obvious exit affordance. |
| R6 | **Floating workspace** | Detachable/pop-out MT5 window. | Frontend | Same-origin embed constraints apply. |
| R7 | **Multiple monitors** | Span/position MT5 across displays. | Frontend + RDP | RDP multimon is heavier; later. |
| R8 | **Multiple simultaneous workspaces** | More than one hosted terminal per user at once. | Backend + host | **Interacts with the single-session invariant** — needs a design decision (distinct Windows identities per workspace, not multiple sessions per user); treat as its own ADR. |
| R9 | **Better clipboard UX** | Beyond the current right-click browser→MT5 paste: a paste helper / clearer affordance; keep copy-out disabled. | Frontend + guac param | Must not enable MT5→browser copy without a security review. |
| R10 | **Native Mac shortcuts** | Map Cmd-based shortcuts (Cmd+V etc.) to the RDP Ctrl equivalents. | Frontend | See Beta UX backlog in `TECH_DEBT_REGISTER.md`. |
| R11 | **Keyboard polish** | Correct symbol mapping for non-US client layouts (`#`/`@`/`£`); consider client-layout detection vs the server-pinned `en-us-qwerty`. | Frontend + guac param | Beta UX backlog. |
| R12 | **Loading UX** | A proper connecting/loading state for the RemoteApp embed. | Frontend | Small, high perceived-quality win. |
| R13 | **Better reconnect UX** | Clear "resuming your session" state on reconnect (the single-session invariant already rejoins the same session). | Frontend | Pairs with R12. |
| R14 | **Workspace switching** | UI to switch between workspaces/accounts. | Frontend + backend | Depends on R8. |
| R15 | **Future multi-terminal support** | More than one MT5 terminal in a workspace. | Backend + host | Furthest out; own ADR. |

## Sequencing suggestion
Presentation wins first (R1, R2, R4, R12, R13) — low risk, high perceived quality. Then clipboard/keyboard
polish (R9, R10, R11) off the Beta UX backlog. R3/R6 next. R7/R8/R14/R15 are architectural (multi-session /
multi-terminal) and each needs its own ADR because they touch the single-session and identity invariants.
