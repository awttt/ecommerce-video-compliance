export interface UpdateStatus {
  current: string;
  latest: string;
  available: boolean;
  phase: string;
  received?: number;
  total?: number;
  message?: string;
  can_apply?: boolean;
  check_state?: string;
}

export interface UpdateNotice {
  level: "success" | "info" | "warning" | "error";
  text: string;
}

/** 控制台只读工具端的更新清单：只检查，不下载、不安装、不重启。 */
export async function readUpdate(refresh = false): Promise<UpdateStatus> {
  const response = await fetch(refresh ? "/api/update?refresh=1" : "/api/update", {
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(response.status === 404 ? "只能在桌面客户端里检查更新" : "暂时无法检查更新");
  }
  return (await response.json()) as UpdateStatus;
}

/** 桌面端开始下载安装包。是否真能装由工具端判断，这里只转发。 */
export async function startUpdate(): Promise<UpdateStatus> {
  const response = await fetch("/api/update", { method: "POST", headers: { accept: "application/json" } });
  const body = (await response.json().catch(() => null)) as (UpdateStatus & { detail?: string }) | null;
  if (!response.ok) {
    throw new Error(body?.detail || "更新失败，请稍后再试");
  }
  return body as UpdateStatus;
}

/** 有更新、正在下载安装或上次失败时才显示提示条。 */
export function updateVisible(info: UpdateStatus | null): boolean {
  if (!info) return false;
  return info.available || info.phase === "downloading" || info.phase === "installing" || info.phase === "error";
}

export function updateNotice(info: UpdateStatus): UpdateNotice | null {
  if (info.phase === "downloading" || info.phase === "installing" || info.check_state === "busy") {
    return { level: "info", text: "已有更新正在进行，不会再次下载或安装" };
  }
  if (info.check_state === "failed") {
    if (info.available && info.latest) {
      return { level: "warning", text: `这次没有连上更新服务，之前发现的 ${info.latest} 还在。` };
    }
    return { level: "error", text: "暂时无法检查更新，不能确定是不是最新版本" };
  }
  if (info.available && info.latest) {
    return { level: "info", text: `发现新版本 ${info.latest}，当前是 ${info.current}。` };
  }
  if (info.check_state === "current") {
    return { level: "success", text: `已是最新版本 ${info.current}` };
  }
  return null;
}
