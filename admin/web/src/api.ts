export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

const ACCESS = "evc-admin-access";
const REFRESH = "evc-admin-refresh";

export function accessToken(): string {
  return sessionStorage.getItem(ACCESS) ?? "";
}

export function saveSession(access: string, refresh: string) {
  sessionStorage.setItem(ACCESS, access);
  localStorage.setItem(REFRESH, refresh);
}

export function clearSession() {
  sessionStorage.removeItem(ACCESS);
  localStorage.removeItem(REFRESH);
}

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
  if (!response.ok) throw new ApiError(detailOf(data), response.status);
  return data as T;
}

function headersOf(init: RequestInit): Headers {
  const headers = new Headers(init.headers);
  const token = accessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return headers;
}

let refreshing: Promise<boolean> | null = null;

async function refreshOnce(): Promise<boolean> {
  const refresh = localStorage.getItem(REFRESH);
  if (!refresh) return false;
  if (!refreshing) {
    refreshing = fetch("/v1/auth/refresh", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }),
    })
      .then(async (response) => {
        if (!response.ok) return false;
        const data = (await response.json()) as { access_token: string; refresh_token: string };
        saveSession(data.access_token, data.refresh_token);
        return true;
      })
      .catch(() => false)
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...init, headers: headersOf(init), credentials: "include" });
  if (response.status === 401 && !path.startsWith("/v1/auth/")) {
    const ok = await refreshOnce();
    if (ok) {
      const retry = await fetch(path, { ...init, headers: headersOf(init), credentials: "include" });
      return parse<T>(retry);
    }
    clearSession();
    if (!location.pathname.startsWith("/login") && !location.pathname.startsWith("/invite")) {
      location.assign("/login");
    }
    throw new ApiError("请先登录", 401);
  }
  return parse<T>(response);
}

export async function download(path: string, filename: string) {
  const response = await fetch(path, { headers: headersOf({}), credentials: "include" });
  if (!response.ok) {
    const text = await response.text();
    let message = "下载失败";
    try {
      message = detailOf(text ? JSON.parse(text) : null);
    } catch {
      message = "下载失败";
    }
    throw new ApiError(message, response.status);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
