export type HostingRequestStatus =
  | "PENDING"
  | "IN_REVIEW"
  | "APPROVED"
  | "REJECTED"
  | "PROVISIONED"
  | "COMPLETED";

export type HostingRequest = {
  id: number;
  owner: number;
  owner_email: string;
  status: HostingRequestStatus;
  note: string | null;
  created_at: string;
  updated_at: string;
};

export type UserHostingSubscriptionStatus =
  | "TRIAL"
  | "ACTIVE"
  | "PAST_DUE"
  | "CANCELLED";

export type UserHostingSubscription = {
  id: number;
  user: number;
  plan: number;
  plan_name: string;
  billing_status: UserHostingSubscriptionStatus;
  vps_hostname?: string | null;
  vps_ip?: string | null;
  mt5_login?: string | null;
  created_at: string;
  updated_at: string;
};

export type VPSPlan = {
  id: number;
  name: string;
  description: string;
  provider: number;
  provider_name: string;
  cpu_cores: number;
  memory_mb: number;
  disk_gb: number;
  monthly_price_usd: string;
  is_shared: boolean;
  max_mt5_instances: number;
  is_user_visible: boolean;
};
