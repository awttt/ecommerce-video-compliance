import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Shell } from "./App";
import "antd/dist/reset.css";
import "./styles.css";

/**
 * 颜色使用 Ant Design 默认色。这里只保留字号、圆角和控件高度。
 */
const theme = {
  token: {
    borderRadius: 8,
    borderRadiusLG: 14,
    borderRadiusSM: 6,
    fontSize: 14,
    fontSizeLG: 16,
    fontSizeSM: 12,
    lineHeight: 1.6,
    lineWidth: 1,
    controlHeight: 36,
    controlHeightLG: 44,
    controlHeightSM: 28,
    fontFamily:
      "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
  },
  components: {
    Button: {
      fontWeight: 500,
    },
    Card: {
      headerFontSize: 15,
      paddingLG: 24,
    },
    Table: {
      cellPaddingBlock: 12,
      headerBorderRadius: 8,
    },
    Steps: {
      iconSize: 26,
      titleLineHeight: 22,
    },
  },
};

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConfigProvider locale={zhCN} button={{ autoInsertSpace: false }} theme={theme}>
      <AntApp>
        <Shell />
      </AntApp>
    </ConfigProvider>
  </StrictMode>,
);
