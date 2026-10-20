// Account-level broker-email contact (MULTI_ACCOUNT §A/§C/§4). Masked, owner-private payloads returned by
// /api/broker-intelligence/accounts/<id>/contacts[...]. NEVER a raw email address or secret.

export type BrokerContactStatus = "PENDING" | "VERIFIED" | "UNRESOLVED" | "RETIRED";

export interface BrokerContact {
  id: number;
  trading_account_id: number;
  broker: string;
  email_masked: string;
  broker_email_identity_id: number;
  connected_mailbox_id: number | null;
  connected_mailbox_masked: string | null;
  status: BrokerContactStatus;
  verified: boolean;
  verified_at: string | null;
}

export interface AccountContactsResponse {
  trading_account_id: number;
  contacts: BrokerContact[];
}

export interface CreateContactResponse {
  ok: boolean;
  contact: BrokerContact;
}

export interface VerifyContactResponse {
  verified: boolean;
  status: BrokerContactStatus;
  reason: string;
  contact: BrokerContact;
}
