import {
  AccountBookOutlined,
  AuditOutlined,
  BarChartOutlined,
  DashboardOutlined,
  DesktopOutlined,
  FileSearchOutlined,
  LogoutOutlined,
  SettingOutlined,
  TeamOutlined,
  UserOutlined,
  BankOutlined,
  WalletOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AutoComplete, Avatar, Badge, Dropdown, Input, Layout, Menu, Spin, Tag, Typography } from "antd";
import type { MenuProps } from "antd";
import { useEffect, useState, type ReactNode } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, clearSession } from "./api";
import type { Role, User } from "./types";

const { Header, Sider, Content } = Layout;

const APP_NAME = "御镜视频审核系统";

const GROUPS = ["工作台", "业务管理", "系统"];

const NAV: { key: string; label: string; group: string; roles: Role[]; icon: ReactNode }[] = [
  { key: "/", label: "总览", group: "工作台", roles: ["platform_admin", "tenant_admin", "member"], icon: <DashboardOutlined /> },
  { key: "/tenants", label: "租户管理", group: "业务管理", roles: ["platform_admin"], icon: <BankOutlined /> },
  { key: "/members", label: "成员管理", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <TeamOutlined /> },
  { key: "/quota", label: "额度分配", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <AccountBookOutlined /> },
  { key: "/shared-rules", label: "规则审核", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <FileSearchOutlined /> },
  { key: "/usage", label: "用量看板", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <BarChartOutlined /> },
  { key: "/audit", label: "审计日志", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <AuditOutlined /> },
  { key: "/recharge", label: "充值", group: "系统", roles: ["platform_admin", "tenant_admin", "member"], icon: <WalletOutlined /> },
  { key: "/settings", label: "接入设置", group: "系统", roles: ["platform_admin"], icon: <SettingOutlined /> },
  { key: "/account", label: "个人设置", group: "系统", roles: ["platform_admin", "tenant_admin", "member"], icon: <UserOutlined /> },
];

function menuLabel(item: (typeof NAV)[number], pendingRules: number) {
  const count = item.key === "/shared-rules" ? pendingRules : 0;
  if (count <= 0) return item.label;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      {item.label}
      <Badge count={count} size="small" />
    </span>
  );
}

export function AdminLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const client = useQueryClient();
  const [collapsed, setCollapsed] = useState(() => location.pathname === "/tools");
  const [searchText, setSearchText] = useState("");
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<User>("/v1/auth/me") });
  const search = useQuery({
    queryKey: ["search", searchText],
    queryFn: () => api<{ members: { name: string; email: string }[] }>(`/v1/search?q=${encodeURIComponent(searchText.trim())}`),
    enabled: me.data?.role !== "member" && searchText.trim().length > 0,
  });

  useEffect(() => {
    if (location.pathname === "/tools") setCollapsed(true);
  }, [location.pathname]);

  if (me.isLoading) {
    return (
      <div className="login-screen">
        <Spin />
      </div>
    );
  }
  if (!me.data) return <Navigate to="/login" replace />;

  const user = me.data;
  const toolsUrl = window.__EVC_TOOLS_URL__ ?? "";
  const toolsItem = { key: "/tools", label: "视频审核工具", icon: <DesktopOutlined /> };
  const visible = NAV.filter((item) => {
    if (item.key === "/recharge" && !toolsUrl) return false;
    return item.roles.includes(user.role);
  });
  const current =
    visible.find((item) => item.key === location.pathname) ??
    (toolsUrl && location.pathname === "/tools" ? toolsItem : undefined);
  if (!current && location.pathname !== "/") return <Navigate to="/" replace />;

  const items: NonNullable<MenuProps["items"]> = GROUPS.map((group) => {
    const children = visible
      .filter((item) => item.group === group)
      .map((item) => ({
        key: item.key,
        icon: item.icon,
        label: menuLabel(item, user.pending_rules ?? 0),
      }));
    if (children.length === 0) return null;
    return { type: "group" as const, label: collapsed ? undefined : group, children };
  }).filter(Boolean) as NonNullable<MenuProps["items"]>;

  // 从桌面工具整页跳过来时，用 iframe 把工具端嵌在这一页里；单独部署时没有这个入口。
  if (toolsUrl) {
    items.push({
      type: "group" as const,
      label: collapsed ? undefined : "工具",
      children: [{ key: "/tools", icon: <DesktopOutlined />, label: "视频审核工具" }],
    });
  }

  const options = (search.data?.members ?? []).map((item) => ({
    value: `member:${item.email}`,
    label: `${item.name} · ${item.email}`,
    path: user.role === "member" ? "/" : "/members",
  }));

  return (
    <Layout style={{ minHeight: "100%" }}>
      <Sider
        theme="light"
        width={220}
        collapsedWidth={64}
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        style={{ borderInlineEnd: "1px solid #f0f0f0" }}
      >
        <div className="brand" title={APP_NAME}>
          <div className="logo-mark">
            <img src="/logo.png" alt="" />
          </div>
          {collapsed ? null : (
            <div>
              <b>{APP_NAME}</b>
            </div>
          )}
        </div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={items}
          onClick={({ key }) => {
            if (key === "/tools") setCollapsed(true);
            navigate(key);
          }}
          style={{ borderInlineEnd: 0 }}
        />
      </Sider>
      <Layout>
        <Header style={{ display: "flex", alignItems: "center", gap: 16, borderBottom: "1px solid #f0f0f0" }}>
          <Typography.Text type="secondary">{APP_NAME} / {current?.label ?? "总览"}</Typography.Text>
          <div style={{ flex: 1 }} />
          <Tag color="blue" style={{ marginInlineEnd: 0 }}>
            {user.role === "platform_admin" ? "全平台" : user.tenant_name || user.role_label}
          </Tag>
          {user.role === "member" ? null : (
            <AutoComplete
              style={{ width: 220 }}
              options={options}
              value={searchText}
              onChange={setSearchText}
              onSelect={(_value, option) => {
                const path = (option as { path?: string }).path;
                if (path) navigate(path);
                setSearchText("");
              }}
            >
              <Input allowClear placeholder="搜索成员" />
            </AutoComplete>
          )}
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                { key: "name", label: `${user.name} · ${user.role_label}`, disabled: true },
                { key: "account", icon: <UserOutlined />, label: "个人设置" },
                { key: "logout", icon: <LogoutOutlined />, label: "退出登录" },
              ],
              onClick: ({ key }) => {
                if (key === "account") {
                  navigate("/account");
                  return;
                }
                if (key !== "logout") return;
                const refresh = localStorage.getItem("evc-admin-refresh") ?? "";
                void api("/v1/auth/logout", { method: "POST", body: JSON.stringify({ refresh_token: refresh }) }).catch(() => undefined);
                clearSession();
                client.clear();
                navigate("/login");
              },
            }}
          >
            <Avatar style={{ background: "#e6f4ff", color: "#1677ff", cursor: "pointer" }}>{user.name.slice(0, 1)}</Avatar>
          </Dropdown>
        </Header>
        <Content style={{ padding: location.pathname === "/tools" ? 0 : 24 }}>
          {location.pathname === "/tools" ? (
            <Outlet context={user} />
          ) : (
            <div className="page">
              <Outlet context={user} />
            </div>
          )}
        </Content>
      </Layout>
    </Layout>
  );
}

