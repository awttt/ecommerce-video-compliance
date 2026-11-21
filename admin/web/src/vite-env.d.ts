/// <reference types="vite/client" />

interface Window {
  /** 桌面工具整页跳过来时，本机壳注入的工具端地址。单独部署时没有。 */
  __EVC_TOOLS_URL__?: string;
}
