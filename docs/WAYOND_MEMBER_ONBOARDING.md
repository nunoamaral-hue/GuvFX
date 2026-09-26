# Wayond — Member Onboarding & Staff Support

Written against the **actual implemented UI** (Phase 9): the per-user Broker Accounts experience
(`/accounts` → `BrokerAccountsContent`), the hosted-workspace journey banner, and the hosted-aware
account cards. The new experience is shown only to per-user-granted customers (`broker_accounts_ux`
capability); every other customer sees the legacy accounts page. No infrastructure terms (SID, runtime,
endpoint, node) are shown to members.

## A. Member quick-start (1 page)

**Trade multiple broker accounts from one GuvFX login.**

1. **Log in** at guvfx.com.
2. **Open "Broker accounts."** The header shows how many accounts can trade at once — **"Active N / 5"**
   (you can run several at once) or **"Only one account can trade at a time"** (one active on your plan).
3. **Add a broker account.** Click **Add account**, choose your broker, enter your account number and the
   DEMO/LIVE type. (Your MT5 password is entered later, securely inside MetaTrader — never shown back to you.)
4. **We prepare your private MetaTrader terminal.** A banner shows *"We're setting up your private
   MetaTrader terminal — this usually takes a few minutes."* The account card shows **"Preparing your
   trading terminal…"**.
5. **Open MT5 and log in.** When the card shows **"Terminal ready"** and the banner says *"Open MetaTrader
   and log in to your broker to finish connecting,"* click **Open MT5** on the card, then log in to your
   broker inside MetaTrader (you type your password there).
6. **Account detected / connected.** Once you've logged in, the workspace shows as connected; if asked,
   **confirm this is your trading account**.
7. **Choose a strategy.** On the card, click **Manage strategies** and pick a strategy for that account.
   Each account runs its own strategies independently.
8. **Configure risk.** Set your lot size / risk per trade. Sizing is explicit per account — one account's
   settings never affect another.
9. **Activate.** Click **Activate**. If your plan trades one account at a time, we'll confirm the switch
   (activating this one pauses the other). Activation only lets the account trade — it never places a trade
   by itself.
10. **Monitor.** Each card shows the broker, a masked account number (e.g. ••••2587), DEMO/LIVE, terminal
    status, how many strategies are assigned, whether it's currently **Trading**, **Open MT5**, **Manage
    strategies**, and **View on Myfxbook** (if you've linked a results page). Telegram notifications name the
    broker, the masked account, and the strategy for each trade.

## B. Onboarding checklist

- [ ] Logged in; opened Broker accounts; understood the "active" allowance (Active N/5 or one-at-a-time).
- [ ] Added broker account A (broker, account number, DEMO/LIVE).
- [ ] Saw "Preparing your trading terminal…" then "Terminal ready".
- [ ] Clicked **Open MT5** and logged in to the broker inside MetaTrader.
- [ ] Account shows connected / confirmed.
- [ ] Assigned a strategy (Manage strategies) and set explicit risk/sizing.
- [ ] Activated the account (saw the switch-confirm if on a one-at-a-time plan).
- [ ] (Optional) Added account B; repeated prepare → Open MT5 → log in → strategy → risk → activate.
- [ ] (Optional) Linked a Myfxbook page per account; connected Telegram.
- [ ] Saw the first natural trade on the correct account with the correct strategy.

## C. Troubleshooting states (member-facing)

| What you see | Meaning | What to do |
|---|---|---|
| Banner "We're setting up your private MetaTrader terminal" + card "Preparing your trading terminal…" | Your terminal is being prepared | Wait a few minutes; stay on the page |
| Card "Terminal ready" + banner "Open MetaTrader and log in" | Terminal is up; broker login needed | Click **Open MT5** and log in to your broker |
| **Open MT5** button greyed out | Terminal isn't ready yet | Wait for "Terminal ready" |
| Banner "Connected — confirm this is your trading account" | Broker login detected | Confirm the account to finish setup |
| Banner "Your trading workspace is ready" | Setup complete | Choose a strategy and set risk |
| "Only one account can trade at a time" + switch prompt | One-at-a-time plan; activating B pauses A | Confirm the switch, or Cancel to keep A |
| "Active 5 / 5" and Activate refused | At your concurrent limit | Deactivate one account first, or ask about a higher plan |
| Broker connection: needs attention (traditional account) | Credentials didn't validate | Manage → Replace credentials, re-validate |

## D. Staff support checklist

1. **Identity & ownership:** confirm the account is the member's (all reads/actions are owner-scoped; staff never act cross-account without confirming ownership).
2. **UX gate:** is this member granted the new Broker Accounts experience (`broker_accounts_ux`)? If not, they're on the legacy page (expected for non-pilot customers).
3. **Terminal status:** card shows Preparing vs Terminal ready. If stuck "Preparing" well beyond a few minutes → escalate (do not expose internal identifiers to the member).
4. **Broker login:** journey banner shows awaiting-login vs connected vs ready. Waiting-for-login is a **member** action (Open MT5 → log in), not a fault.
5. **Entitlement:** Active N / limit and owned count explain "why can't I add/activate another account."
6. **Strategy & execution:** correct strategy assigned; sizing explicit; correct account trading; recent trades on the correct account with the correct strategy/magic; **cross-account ownership violations must be zero**.
7. **Notifications:** Telegram bound? Broker + masked account + strategy shown correctly?
8. **Never** request or share a password; direct the member to the secure MetaTrader login / replace-credentials flow.

## E. Member-launch gating (engineering)

- The new experience is **per-user** (`broker_accounts_ux` grant); default is the legacy page for everyone. Enable a member with `grant_broker_ux(user)`; roll back with `revoke_broker_ux(user)`.
- The legacy global build flag `NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` still force-enables the new UX for **all** users (dev/staging only) — do **not** turn it on in production; use the per-user grant.
- MUST-HAVE before first controlled member: this UX gate (done), the Open-MT5 → login flow (done), one 2-account natural-execution certification. SHOULD-HAVE: Telegram binding UX, per-account Myfxbook linking, strategy diversity. NOT REQUIRED for launch: accounts C–E, global UX rollout.
