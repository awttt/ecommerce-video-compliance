import { Navigate } from "react-router-dom";

/**
 * 桌面工具整页跳过来时，控制台用 iframe 打开工具端。
 * 地址由本机壳注入；单独部署的云端后台没有这个地址。
 */
export function ToolsFrame() {
  const url = window.__EVC_TOOLS_URL__ ?? "";
  if (!url) return <Navigate to="/" replace />;
  return <iframe className="tools-frame" src={url} title="视频审核工具" allow="autoplay; fullscreen; clipboard-write" />;
}
