import type { Role } from "./types";

/** 视频审核工具在侧栏里的路径。生图工作台必须排在它的正下方，并且同属「工具」分组。 */
export const VIDEO_TOOLS_PATH = "/tools";
export const IMAGE_WORKBENCH_PATH = "/image-workbench";
export const TOOL_GROUP = "工具";

const IMAGE_ROLES: Role[] = ["platform_admin", "tenant_admin", "member"];

export interface ToolNavItem {
  key: string;
  label: string;
  group: typeof TOOL_GROUP;
}

export function toolMenuEntries(toolsEnabled: boolean, role: Role): ToolNavItem[] {
  const items: ToolNavItem[] = [];
  if (toolsEnabled) {
    items.push({ key: VIDEO_TOOLS_PATH, label: "视频审核工具", group: TOOL_GROUP });
  }
  if (IMAGE_ROLES.includes(role)) {
    items.push({ key: IMAGE_WORKBENCH_PATH, label: "生图工作台", group: TOOL_GROUP });
  }
  return items;
}
