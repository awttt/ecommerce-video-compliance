import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { accessToken, rememberedEmail, rememberEmail, restoreSession, saveSession, takeExpiredMark } from "./api";
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

type SignedInUser = { email?: string; role?: string };

/** 桌面窗口每次都带 ?sso=1 打开。普通员工这时进审核工具，总览仍可从菜单打开。 */
function memberOpensTools(role: string, url: URL) {
  return role === "member" && Boolean(window.__EVC_TOOLS_URL__) && (url.pathname === "/" || url.pathname === "");
}

async function signedInRole(): Promise<string> {
  if (!accessToken()) return "";
  try {
    const response = await fetch("/v1/auth/me", {
      credentials: "include",
      headers: { Authorization: `Bearer ${accessToken()}` },
    });
    if (!response.ok) return "";
    const data = (await response.json()) as SignedInUser;
    return data.role ?? "";
  } catch {
    return "";
  }
}

/** 从桌面工具跳过来（带 ?sso=1）时，直接用工具端的登录换控制台会话。 */
async function singleSignOn() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("sso")) return;
  let role = "";
  // 满 7 天必须重新输入密码，不再用工具端会话把登录态续上。
  if (!takeExpiredMark() && !accessToken()) {
    try {
      const response = await fetch("/sso", { method: "POST", credentials: "include" });
      if (response.ok) {
        const data = (await response.json()) as {
          access_token?: string;
          refresh_token?: string;
          user?: SignedInUser;
        };
        if (data.access_token && data.refresh_token) {
          saveSession(data.access_token, data.refresh_token, data.user?.email ?? "");
          role = data.user?.role ?? "";
        }
      }
    } catch {
      // 单独部署的云端后台没有 /sso，回落成正常登录页。
    }
  } else if (accessToken()) {
    role = await signedInRole();
  }
  if (memberOpensTools(role, url)) url.pathname = "/tools";
  url.searchParams.delete("sso");
  window.history.replaceState(null, "", url.pathname + url.search + url.hash);
}

/** 刷新令牌恢复的会话没有走过登录表单，这里把当前邮箱留给下次登录。 */
async function rememberSignedInEmail() {
  if (rememberedEmail() || !accessToken()) return;
  try {
    const response = await fetch("/v1/auth/me", {
      credentials: "include",
      headers: { Authorization: `Bearer ${accessToken()}` },
    });
    if (!response.ok) return;
    const data = (await response.json()) as { email?: string };
    rememberEmail(data.email ?? "");
  } catch {
    // 记不住邮箱不影响这次登录。
  }
}

async function start() {
  await restoreSession();
  await singleSignOn();
  await rememberSignedInEmail();
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
