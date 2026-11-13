import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Shell } from "./App";
import "antd/dist/reset.css";
import "./styles.css";

/**
 * 全局设计规范 —— 「简白」
 * 单一强调色（墨绿）+ 中性暖白底色 + 克制的边框与阴影。
 * 所有颜色只在这里和 styles.css 中定义，组件内不再散落硬编码色值。
 */
const theme = {
  token: {
    colorPrimary: "#1F6F5B",
    colorInfo: "#1F6F5B",
    colorSuccess: "#2E7D62",
    colorWarning: "#B0791F",
    colorError: "#B4453C",
    colorText: "#1C2621",
    colorTextSecondary: "#5F6D65",
    colorTextTertiary: "#8B968F",
    colorTextQuaternary: "#B0B9B2",
    colorBorder: "#E4EAE4",
    colorBorderSecondary: "#EFF2EF",
    colorBgLayout: "#F7F8F6",
    colorBgContainer: "#FFFFFF",
    colorBgElevated: "#FFFFFF",
    colorFillAlter: "#F7F9F7",
    colorFillSecondary: "#F1F4F1",
    colorFillTertiary: "#F5F7F5",
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
      primaryShadow: "none",
      defaultShadow: "none",
      dangerShadow: "none",
    },
    Card: {
      headerBg: "transparent",
      headerFontSize: 15,
      paddingLG: 24,
    },
    Table: {
      headerBg: "#F8FAF8",
      headerColor: "#6E7A73",
      headerSplitColor: "transparent",
      rowHoverBg: "#F4F8F5",
      borderColor: "#EDF1ED",
      cellPaddingBlock: 12,
      headerBorderRadius: 8,
    },
    Segmented: {
      trackBg: "#F1F4F1",
      itemSelectedBg: "#FFFFFF",
    },
    Tag: {
      defaultBg: "#F2F5F2",
      defaultColor: "#5F6D65",
    },
    Progress: {
      defaultColor: "#1F6F5B",
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
