import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { accessToken, saveSession } from "./api";
import "antd/dist/reset.css";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

const theme = {
  token: {
    borderRadius: 8,
    borderRadiusLG: 12,
    fontSize: 14,
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  },
  components: {
    Layout: {
      headerHeight: 56,
      headerPadding: "0 24px",
      bodyBg: "#f5f5f5",
      headerBg: "#ffffff",
    },
  },
};

/** 从桌面工具跳过来（带 ?sso=1）时，直接用工具端的登录换控制台会话。 */
async function singleSignOn() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("sso")) return;
  if (!accessToken()) {
    try {
      const response = await fetch("/sso", { method: "POST", credentials: "include" });
      if (response.ok) {
        const data = (await response.json()) as { access_token?: string; refresh_token?: string };
        if (data.access_token && data.refresh_token) saveSession(data.access_token, data.refresh_token);
      }
    } catch {
      // 单独部署的云端后台没有 /sso，回落成正常登录页。
    }
  }
  url.searchParams.delete("sso");
  window.history.replaceState(null, "", url.pathname + url.search + url.hash);
}

async function start() {
  await singleSignOn();
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={theme}>
          <AntApp>
            <BrowserRouter>
              <App />
            </BrowserRouter>
          </AntApp>
        </ConfigProvider>
      </QueryClientProvider>
    </StrictMode>,
  );
}

void start();
