/// <reference types="vite/client" />

interface Window {
  /** 桌面工具整页跳过来时，本机壳注入的工具端地址。单独部署时没有。 */
  __EVC_TOOLS_URL__?: string;
  /** pywebview 在桌面窗口里注入。浏览器里没有。 */
  pywebview?: {
    api?: {
      save_generated_image?: (url: string, filename: string) => Promise<{
        ok?: boolean;
        cancelled?: boolean;
        message?: string;
        bytes?: number;
        filename?: string;
      }>;
    };
  };
}
