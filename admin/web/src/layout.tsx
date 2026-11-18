import {
  AccountBookOutlined,
  ApartmentOutlined,
  AuditOutlined,
  BarChartOutlined,
  DashboardOutlined,
  EditOutlined,
  LogoutOutlined,
  PlayCircleOutlined,
  SettingOutlined,
  TeamOutlined,
  BankOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AutoComplete, Avatar, Badge, Dropdown, Input, Layout, Menu, Spin, Tag, Typography } from "antd";
import type { MenuProps } from "antd";
import { useState, type ReactNode } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, clearSession } from "./api";
import type { Role, Submission, User } from "./types";

const { Header, Sider, Content } = Layout;

const GROUPS = ["工作台", "业务管理", "系统"];

const NAV: { key: string; label: string; group: string; roles: Role[]; icon: ReactNode }[] = [
  { key: "/", label: "总览", group: "工作台", roles: ["platform_admin", "tenant_admin", "member"], icon: <DashboardOutlined /> },
  { key: "/flow", label: "流程图与权限", group: "工作台", roles: ["platform_admin", "tenant_admin", "member"], icon: <ApartmentOutlined /> },
  { key: "/review", label: "视频审核", group: "业务管理", roles: ["platform_admin", "tenant_admin", "member"], icon: <PlayCircleOutlined /> },
  { key: "/tenants", label: "租户管理", group: "业务管理", roles: ["platform_admin"], icon: <BankOutlined /> },
  { key: "/members", label: "成员管理", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <TeamOutlined /> },
  { key: "/quota", label: "额度分配", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <AccountBookOutlined /> },
  { key: "/keywords", label: "关键词审批", group: "业务管理", roles: ["platform_admin", "tenant_admin", "member"], icon: <EditOutlined /> },
  { key: "/usage", label: "用量看板", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <BarChartOutlined /> },
  { key: "/audit", label: "审计日志", group: "业务管理", roles: ["platform_admin", "tenant_admin"], icon: <AuditOutlined /> },
  { key: "/settings", label: "接入设置", group: "系统", roles: ["platform_admin"], icon: <SettingOutlined /> },
];

function menuLabel(item: (typeof NAV)[number], role: Role, pending: number) {
  const text = item.key === "/keywords" && role === "member" ? "我的提交" : item.label;
  if (item.key !== "/keywords" || pending <= 0) return text;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      {text}
      <Badge count={pending} size="small" />
    </span>
  );
}

export function AdminLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const client = useQueryClient();
  const [collapsed, setCollapsed] = useState(false);
  const [searchText, setSearchText] = useState("");
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<User>("/v1/auth/me") });
  const search = useQuery({
    queryKey: ["search", searchText],
    queryFn: () => api<{ members: { name: string; email: string }[]; keywords: Submission[] }>(`/v1/search?q=${encodeURIComponent(searchText.trim())}`),
    enabled: searchText.trim().length > 0,
  });

  if (me.isLoading) {
    return (
      <div className="login-screen">
        <Spin />
      </div>
    );
  }
  if (!me.data) return <Navigate to="/login" replace />;

  const user = me.data;
  const visible = NAV.filter((item) => item.roles.includes(user.role));
  const current = visible.find((item) => item.key === location.pathname);
  if (!current && location.pathname !== "/") return <Navigate to="/" replace />;

  const items: MenuProps["items"] = GROUPS.map((group) => {
    const children = visible
      .filter((item) => item.group === group)
      .map((item) => ({
        key: item.key,
        icon: item.icon,
        label: menuLabel(item, user.role, user.pending_keywords),
      }));
    if (children.length === 0) return null;
    return { type: "group" as const, label: collapsed ? undefined : group, children };
  }).filter(Boolean) as MenuProps["items"];

  const options = [
    ...(search.data?.members ?? []).map((item) => ({
      value: `member:${item.email}`,
      label: `${item.name} · ${item.email}`,
      path: user.role === "member" ? "/review" : "/members",
    })),
    ...(search.data?.keywords ?? []).map((item) => ({
      value: `keyword:${item.id}`,
      label: `${item.text} · ${item.status_label}`,
      path: "/keywords",
    })),
  ];

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
        <div className="brand">
          <div className="logo-mark">
            <img src="/logo.png" alt="" />
          </div>
          {collapsed ? null : (
            <div>
              <b>御镜</b>
              <small>管理后台</small>
            </div>
          )}
        </div>
        <Menu
          mode="inline"
          selectedKeys={[location.pathname]}
          items={items}
          onClick={({ key }) => navigate(key)}
          style={{ borderInlineEnd: 0 }}
        />
      </Sider>
      <Layout>
        <Header style={{ display: "flex", alignItems: "center", gap: 16, borderBottom: "1px solid #f0f0f0" }}>
          <Typography.Text type="secondary">御镜 / {current?.label ?? "总览"}</Typography.Text>
          <div style={{ flex: 1 }} />
          <Tag color="blue" style={{ marginInlineEnd: 0 }}>
            {user.role === "platform_admin" ? "全平台" : user.tenant_name || user.role_label}
          </Tag>
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
            <Input allowClear placeholder="搜索成员 / 命中词" />
          </AutoComplete>
          <Dropdown
            trigger={["click"]}
            menu={{
              items: [
                { key: "name", label: `${user.name} · ${user.role_label}`, disabled: true },
                { key: "logout", icon: <LogoutOutlined />, label: "退出登录" },
              ],
              onClick: ({ key }) => {
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
        <Content style={{ padding: 24 }}>
          <div className="page">
            <Outlet context={user} />
          </div>
        </Content>
      </Layout>
    </Layout>
  );
}

