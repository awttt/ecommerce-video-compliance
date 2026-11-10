import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Shell } from "./App";
import "antd/dist/reset.css";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: "#1f6f5b", borderRadius: 8 } }}>
      <AntApp>
        <Shell />
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
);
