/**
 * 本地充值接口助手（来自工具端，无需 Authorization 头）
 */

function detailOf(data: unknown): string {
  if (!data || typeof data !== "object") return "请求失败";
  const detail = (data as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (item && typeof item === "object" && "msg" in item ? String(item.msg) : ""))
      .filter(Boolean)
      .join("；") || "请求失败";
  }
  return "请求失败";
}

async function parse<T>(response: Response): Promise<T> {
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error(detailOf(data));
  return data as T;
}

export interface LocalBalance {
  logged_in: boolean;
  email: string;
  balance: number | null;
}

export interface RechargeInfo {
  balance_disabled: boolean;
  global_min: number;
  global_max: number;
  recharge_fee_rate: number;
  balance_recharge_multiplier: number;
  bonus_mode: string;
  bonus_notice: string;
  bonus_tiers: Array<{ min_amount: number; bonus_percent: number }>;
  help_text: string;
  methods: Array<{
    payment_type: string;
    display_name: string;
    currency: string;
    fee_rate: number;
    daily_limit: number;
    single_min: number;
    single_max: number;
  }>;
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

export async function localBalance(): Promise<LocalBalance> {
  const response = await fetch("/api/account/balance");
  return parse<LocalBalance>(response);
}

export async function rechargeInfo(): Promise<RechargeInfo> {
  const response = await fetch("/api/account/recharge");
  return parse<RechargeInfo>(response);
}

export async function listRechargeOrders(): Promise<RechargeOrders> {
  const response = await fetch("/api/account/recharge/orders");
  return parse<RechargeOrders>(response);
}

export async function createRecharge(amount: number, paymentType: string): Promise<RechargeOrder> {
  const response = await fetch("/api/account/recharge/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amount, payment_type: paymentType }),
  });
  return parse<RechargeOrder>(response);
}

export async function getRechargeOrder(orderId: number): Promise<RechargeOrder> {
  const response = await fetch(`/api/account/recharge/orders/${orderId}`);
  return parse<RechargeOrder>(response);
}

export async function cancelRechargeOrder(orderId: number): Promise<RechargeOrder> {
  const response = await fetch(`/api/account/recharge/orders/${orderId}/cancel`, { method: "POST" });
  return parse<RechargeOrder>(response);
}
