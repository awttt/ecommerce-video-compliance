import type {
  AccountAction,
  AccountOptions,
  BatchResult,
  CutSpan,
  Job,
  RechargeInfo,
  RechargeOrder,
  RechargeOrders,
  Settings,
} from "./types";

async function readError(response: Response): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: unknown };
    if (typeof data.detail === "string") return data.detail;
    if (Array.isArray(data.detail)) {
      return data.detail
        .map((item) => (item && typeof item === "object" && "msg" in item ? String(item.msg) : ""))
        .filter(Boolean)
        .join("；");
    }
  } catch {
    return `请求失败（${response.status}）`;
  }
  return `请求失败（${response.status}）`;
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) throw new Error(await readError(response));
  return (await response.json()) as T;
}

export function getSettings(): Promise<Settings> {
  return request<Settings>("/api/settings");
}

export function saveSettings(body: Record<string, unknown>): Promise<Settings> {
  return request<Settings>("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function accountOptions(): Promise<AccountOptions> {
  return request<AccountOptions>("/api/account/options");
}

function accountPost(path: string, body?: Record<string, unknown>): Promise<AccountAction> {
  return request<AccountAction>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export function accountSendCode(email: string): Promise<AccountAction> {
  return accountPost("/api/account/send-code", { email });
}

export function accountRegister(email: string, password: string, verifyCode: string): Promise<AccountAction> {
  return accountPost("/api/account/register", { email, password, verify_code: verifyCode });
}

export function accountLogin(email: string, password: string): Promise<AccountAction> {
  return accountPost("/api/account/login", { email, password });
}

export function accountLogin2fa(totpCode: string): Promise<AccountAction> {
  return accountPost("/api/account/login/2fa", { totp_code: totpCode });
}

export function accountLogout(): Promise<AccountAction> {
  return accountPost("/api/account/logout");
}

export function accountRefresh(): Promise<AccountAction> {
  return accountPost("/api/account/refresh");
}

export function rechargeInfo(): Promise<RechargeInfo> {
  return request<RechargeInfo>("/api/account/recharge");
}

export function listRechargeOrders(): Promise<RechargeOrders> {
  return request<RechargeOrders>("/api/account/recharge/orders");
}

export function createRecharge(amount: number, paymentType: string): Promise<RechargeOrder> {
  return request<RechargeOrder>("/api/account/recharge/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ amount, payment_type: paymentType }),
  });
}

export function getRechargeOrder(orderId: number): Promise<RechargeOrder> {
  return request<RechargeOrder>(`/api/account/recharge/orders/${orderId}`);
}

export function cancelRechargeOrder(orderId: number): Promise<RechargeOrder> {
  return request<RechargeOrder>(`/api/account/recharge/orders/${orderId}/cancel`, { method: "POST" });
}

export function createJob(form: FormData): Promise<Job> {
  return request<Job>("/api/jobs", { method: "POST", body: form });
}

export function createJobs(form: FormData): Promise<BatchResult> {
  return request<BatchResult>("/api/jobs/batch", { method: "POST", body: form });
}

export function listJobs(): Promise<Job[]> {
  return request<Job[]>("/api/jobs");
}

export function getJob(id: string): Promise<Job> {
  return request<Job>(`/api/jobs/${id}`);
}

export function cancelJob(id: string): Promise<Job> {
  return request<Job>(`/api/jobs/${id}/cancel`, { method: "POST" });
}

export function deleteJob(id: string): Promise<void> {
  return request<void>(`/api/jobs/${id}`, { method: "DELETE" });
}

export function planEdit(id: string, paddingSec: number): Promise<Job> {
  return request<Job>(`/api/jobs/${id}/edit/plan`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ padding_sec: paddingSec }),
  });
}

export function saveCuts(id: string, cuts: CutSpan[]): Promise<Job> {
  return request<Job>(`/api/jobs/${id}/cuts`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cuts }),
  });
}

export function renderEdit(id: string): Promise<Job> {
  return request<Job>(`/api/jobs/${id}/cut/render`, { method: "POST" });
}

export function openDir(target: "exports" | "data"): Promise<{ path: string }> {
  return request<{ path: string }>("/api/system/open-dir", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target }),
  });
}

export async function downloadExport(id: string, fmt: "json" | "xlsx"): Promise<void> {
  const response = await fetch(`/api/jobs/${id}/export?fmt=${fmt}`);
  if (!response.ok) throw new Error(await readError(response));
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const matched = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const filename = matched ? decodeURIComponent(matched[1]) : `合规结果.${fmt}`;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
