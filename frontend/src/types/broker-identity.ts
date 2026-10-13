/** Broker email identity verification — shapes of the DARK owner-scoped API (WP7).
 *
 * GET  /api/broker-intelligence/identities/        → { identities, mailboxes }
 * POST /api/broker-intelligence/identities/verify/ → VerifyResult
 *
 * Addresses are always MASKED in these payloads (owner-private; the raw email is never returned). A verification
 * binds a broker-registered address to one of the member's OWN trading accounts ONLY on composed proof; it never
 * auto-verifies and is never a financial action. */
export type BrokerIdentity = {
  id: number;
  broker: string;
  email_masked: string;
  status: string;                      // PENDING | VERIFIED | UNRESOLVED | RETIRED
  verified: boolean;
  trading_account_id: number | null;
  connected_mailbox_id: number | null;
  connected_mailbox_masked: string | null;
};

export type ConnectedMailboxSummary = {
  id: number;
  email_masked: string;
  status: string;                      // PENDING | CONNECTED | REVOKED | ERROR
};

export type IdentityListResponse = {
  identities: BrokerIdentity[];
  mailboxes: ConnectedMailboxSummary[];
};

export type VerifyResult =
  | { ok: true; status: string; identity_id: number; identity: BrokerIdentity }
  | { ok: false; error: string; message: string };
