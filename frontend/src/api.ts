import type {
  AccountAction,
  AccountOptions,
  BatchResult,
  CutSpan,
  Job,
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

export function getHealth(): Promise<{ ok: boolean; mcp?: boolean }> {
  return request<{ ok: boolean; mcp?: boolean }>("/api/health");
}

/** 管理控制台在另一个本机端口，地址由后端给。 */
export function getConsoleInfo(): Promise<{ enabled: boolean; url: string }> {
  return request<{ enabled: boolean; url: string }>("/api/console");
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

export function createJob(form: FormData): Promise<Job> {
  return request<Job>("/api/jobs", { method: "POST", body: form });
}

export function createJobs(form: FormData): Promise<BatchResult> {
  return request<BatchResult>("/api/jobs/batch", { method: "POST", body: form });
}

export function listJobs(): Promise<Job[]> {
  return request<Job[]>("/api/jobs");
}

export function getQueue(): Promise<{ paused: boolean }> {
  return request<{ paused: boolean }>("/api/queue");
}

export function pauseQueue(): Promise<{ paused: boolean }> {
  return request<{ paused: boolean }>("/api/queue/pause", { method: "POST" });
}

export function resumeQueue(): Promise<{ paused: boolean }> {
  return request<{ paused: boolean }>("/api/queue/resume", { method: "POST" });
}

export function clearQueue(): Promise<{ ok: boolean; deleted: number }> {
  return request<{ ok: boolean; deleted: number }>("/api/queue", { method: "DELETE" });
}

export function deleteAllJobs(): Promise<{ ok: boolean; deleted: number }> {
  return request<{ ok: boolean; deleted: number }>("/api/jobs", { method: "DELETE" });
}

export function getJob(id: string): Promise<Job> {
  return request<Job>(`/api/jobs/${id}`);
}

export function cancelJob(id: string): Promise<Job> {
  return request<Job>(`/api/jobs/${id}/cancel`, { method: "POST" });
}

export function retryFailedFrames(id: string): Promise<Job> {
  return request<Job>(`/api/jobs/${id}/retry-frames`, { method: "POST" });
}

export function retrySpeech(id: string, part: "transcribe" | "judge"): Promise<Job> {
  return request<Job>(`/api/jobs/${id}/retry-speech`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ part }),
  });
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
  await saveDownload(response, `合规结果.${fmt}`);
}

export async function downloadBatchExport(ids: string[]): Promise<void> {
  const response = await fetch("/api/jobs/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ids }),
  });
  if (!response.ok) throw new Error(await readError(response));
  await saveDownload(response, "批量合规结果.xlsx");
}

async function saveDownload(response: Response, fallback: string): Promise<void> {
  const blob = await response.blob();
  const disposition = response.headers.get("Content-Disposition") || "";
  const matched = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  const filename = matched ? decodeURIComponent(matched[1]) : fallback;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}
