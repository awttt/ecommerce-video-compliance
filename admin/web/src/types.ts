export type Role = "platform_admin" | "tenant_admin" | "member";

export interface SharedRule {
  id: number;
  word: string;
  category: string;
  description: string;
  member_id: number;
  member_name: string;
  tenant_id: number | null;
  tenant_name: string;
  status: "pending" | "approved" | "rejected";
  status_label: string;
  reviewer_id: number | null;
  reviewer_name: string;
  review_comment: string;
  created_at: string;
  reviewed_at: string | null;
}

export interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  role_label: string;
  tenant_id: number | null;
  tenant_name: string;
  permissions: string[];
  pending_rules?: number;
  relay_live: boolean;
}

export interface Reconcile {
  ok: boolean;
  balance: number;
  month_cost: number;
  cumulative_cost: number;
  allocated: number;
  pool_remaining: number;
  pool_total: number;
  previous_month_cost: number;
  overdraft: boolean;
  overdraft_members: string[];
  tenants?: { tenant_id: number; name: string; pool_remaining: number; pool_total: number }[];
}

export interface RelayMemberRow {
  member_id: number;
  name: string;
  email: string;
  relay_user_id: string;
  local_balance: number;
  relay_balance: number;
  diff: number;
  local_status: string;
  relay_status: string;
  month_cost: number;
  lifetime_cost: number;
  checked_at: string | null;
  ok: boolean;
}

export interface RelayReport {
  mode: "local" | "remote";
  synced: boolean;
  checked_at: string | null;
  ok: boolean;
  members: RelayMemberRow[];
  missing?: string[];
  failed?: string[];
  totals: {
    local_balance: number;
    relay_balance: number;
    diff: number;
    month_cost: number;
    lifetime_cost: number;
    members: number;
  };
}

export interface TrendPoint {
  date: string;
  cost: number;
}

export interface ConsumptionComparison {
  label: string;
  previous_start: string;
  previous_end: string;
  previous_total: number;
}

export interface ConsumptionCompany {
  tenant_id: number;
  name: string;
  cost: number;
}

export interface ConsumptionMember {
  member_id: number;
  name: string;
  tenant_id: number | null;
  tenant_name: string;
  cost: number;
}

export interface Consumption {
  start: string;
  end: string;
  timezone: string;
  total: number;
  comparison: ConsumptionComparison | null;
  by_day: TrendPoint[];
  by_company: ConsumptionCompany[];
  by_member: ConsumptionMember[];
}

export interface AuditPage {
  items: AuditRow[];
  total: number;
  page: number;
  page_size: number;
}

export interface AuditRow {
  id: number;
  created_at: string;
  actor_name: string;
  role: string;
  role_label: string;
  tenant_name: string;
  action: string;
  object: string;
  summary: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  ip: string;
}

export interface Dashboard {
  reconcile: Reconcile;
  trend: TrendPoint[];
  recent: AuditRow[];
}

export interface Tenant {
  id: number;
  name: string;
  status: string;
  status_label: string;
  seats_used: number;
  seat_quota: number;
  relay_group_name: string;
  pool_total: number;
  pool_remaining: number;
  allocated: number;
  month_cost: number;
  relay_balance: number;
  relay_diff: number | null;
  relay_checked_at: string | null;
  created_at: string;
}

export interface Member {
  id: number;
  tenant_id: number | null;
  tenant_name: string;
  email: string;
  name: string;
  role: Role;
  role_label: string;
  relay_user_id: string;
  group_name: string;
  status: string;
  status_label: string;
  balance: number;
  relay_balance: number;
  relay_diff: number | null;
  relay_status: string;
  relay_checked_at: string | null;
  overdraft: boolean;
  month_cost: number;
  lifetime_cost: number;
  month_requests: number;
  month_input_tokens: number;
  month_output_tokens: number;
  created_at: string;
}

export interface Invite {
  id: number;
  email: string;
  name: string;
  role_label: string;
  expires_at: string;
  invite_token?: string;
}

export interface QuotaRecord {
  id: number;
  member_name: string;
  amount: number;
  direction: string;
  direction_label: string;
  period: string;
  remark: string;
  idempotency_key: string;
  operator_name: string;
  status: string;
  status_label: string;
  created_at: string;
}

export interface Finding {
  时间点: string;
  类别: string;
  来源: string;
  原文: string;
  位置: string;
  置信度: string;
  判定理由: string;
  备注: string;
  start_sec: number;
  end_sec: number;
  sentence_start?: number | null;
  sentence_end?: number | null;
}

export interface UsageSummary {
  requests: number;
  input_tokens: number;
  output_tokens: number;
  tokens: number;
  cost: number;
  active_members: number;
  average: number;
}

export interface UsageMember {
  id: number;
  name: string;
  relay_user_id: string;
  requests: number;
  tokens: number;
  cost: number;
  share: number;
}

export interface SettingCheck {
  key: string;
  label: string;
  ok: boolean;
  state: string;
  detail: string;
}

export interface Settings {
  relay_base_url: string;
  relay_gateway_url: string;
  group_name: string;
  group_id: string;
  admin_key_configured: boolean;
  backend_mode_enabled: boolean;
  registration_enabled: boolean;
  email_verify_ok: boolean;
  timezone: string;
  checks: SettingCheck[];
}

export interface RechargeMethod {
  payment_type: string;
  display_name: string;
  currency: string;
  fee_rate: number;
  daily_limit: number;
  single_min: number;
  single_max: number;
}

export interface RechargeBonusTier {
  min_amount: number;
  bonus_percent: number;
}

export interface RechargeInfo {
  balance_disabled: boolean;
  global_min: number;
  global_max: number;
  recharge_fee_rate: number;
  balance_recharge_multiplier: number;
  bonus_mode: string;
  bonus_notice: string;
  bonus_tiers: RechargeBonusTier[];
  help_text: string;
  methods: RechargeMethod[];
  message: string;
}

export interface RechargeOrder {
  order_id: number;
  amount: number;
  pay_amount: number;
  fee_rate: number;
  bonus_amount: number;
  status: string;
  result_type: string;
  payment_type: string;
  pay_url: string;
  qr_code: string;
  currency: string;
  expires_at: string;
  created_at: string;
  message: string;
  settings?: { account_balance: number | null } | null;
}

export interface RechargeOrders {
  orders: RechargeOrder[];
}