export interface UpdateView {
  current: string;
  latest: string;
  available: boolean;
  phase: string;
  can_apply: boolean;
  message?: string;
  check_state?: string;
  check_message?: string;
}

export interface UpdateNotice {
  level: "success" | "info" | "warning" | "error";
  text: string;
  confirmInstall: boolean;
}

/** 手动检查只解释现有更新状态。确认安装仍走原来的「立即更新」。 */
export function manualUpdateNotice(info: UpdateView): UpdateNotice {
  if (info.phase === "downloading" || info.phase === "installing" || info.check_state === "busy") {
    return {
      level: "info",
      text: "已有更新正在进行，不会再次下载或安装",
      confirmInstall: false,
    };
  }
  if (info.check_state === "failed") {
    if (info.available && info.latest) {
      return {
        level: "warning",
        text: info.can_apply
          ? `这次没有连上更新服务，不能把它当成已是最新。之前发现的 ${info.latest} 还在，确认后才会下载安装。`
          : `这次没有连上更新服务。之前发现的 ${info.latest} 还在。只有 Windows 安装版可以在这里直接更新。`,
        confirmInstall: info.can_apply,
      };
    }
    return {
      level: "error",
      text: "暂时无法检查更新，不能确定是不是最新版本",
      confirmInstall: false,
    };
  }
  if (info.phase === "error") {
    return {
      level: "error",
      text: info.message || "更新失败，请稍后再试",
      confirmInstall: Boolean(info.available && info.can_apply),
    };
  }
  if (info.available && info.check_state === "available") {
    return {
      level: "info",
      text: info.can_apply
        ? `发现新版本 ${info.latest}，当前是 ${info.current}。确认后才会下载安装。`
        : `发现新版本 ${info.latest}。只有 Windows 安装版可以在这里直接更新。`,
      confirmInstall: info.can_apply,
    };
  }
  if (info.check_state === "current") {
    return {
      level: "success",
      text: `已是最新版本 ${info.current}`,
      confirmInstall: false,
    };
  }
  return { level: "info", text: "", confirmInstall: false };
}
