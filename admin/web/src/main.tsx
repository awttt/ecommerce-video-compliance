import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
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
