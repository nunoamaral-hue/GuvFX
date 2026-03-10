// ---------------------------------------------------------------------------
// Billing API response types — Phase 1 (read-only)
// Matches backend serializers exactly. All nullable fields reflect backend.
// ---------------------------------------------------------------------------

export type Subscription = {
  current_plan: string | null;
  plan_status: string;
  viewer_mode: boolean;
  has_ever_paid: boolean;
  currency: string;
  trial_started_at: string | null;
  trial_expires_at: string | null;
  billing_cycle: string | null;
  current_period_started_at: string | null;
  current_period_ends_at: string | null;
  next_invoice_date: string | null;
  next_payment_due_date: string | null;
  last_invoice_date: string | null;
  last_payment_at: string | null;
  last_plan_change_at: string | null;
};

export type SubscriptionResponse = {
  subscription: Subscription | null;
};

export type Invoice = {
  invoice_number: string;
  plan_at_issue: string | null;
  billing_cycle_at_issue: string | null;
  period_start: string;
  period_end: string;
  issue_date: string;
  due_date: string;
  status: string;
  currency: string;
  subtotal_amount: string;
  tax_amount: string;
  total_amount: string;
  paid_at: string | null;
  voided_at: string | null;
  notes: string;
};

export type InvoicesResponse = {
  invoices: Invoice[];
};
