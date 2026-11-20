import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Checkbox, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { money, when } from "../format";
import type { Device, Member, RelayReport, Tenant, User } from "../types";
import { PageHead, StatusTag } from "../ui";

export function Members() {
  const user = useOutletContext<User>();
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [linkExisting, setLinkExisting] = useState(false);
  const [devicesOf, setDevicesOf] = useState<Member | null>(null);
  const [tenantId, setTenantId] = useState<number | undefined>(undefined);
  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api<Tenant[]>("/v1/tenants"),
    enabled: user.role === "platform_admin",
  });
  const scope = tenantId ?? (user.role === "platform_admin" ? undefined : user.tenant_id ?? undefined);
  const members = useQuery({
    queryKey: ["members", scope ?? "all"],
    queryFn: () => api<Member[]>(scope ? `/v1/members?tenant_id=${scope}` : "/v1/members"),
  });
  const devices = useQuery({
    queryKey: ["devices", devicesOf?.id],
    queryFn: () => api<Device[]>(`/v1/members/${devicesOf?.id}/devices`),
    enabled: Boolean(devicesOf),
  });

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["members"] });
    void client.invalidateQueries({ queryKey: ["devices"] });
  };

  const create = useMutation({
    mutationFn: (values: { email: string; name: string; password: string; role: string; link_existing: boolean }) =>
      api<Member>("/v1/members", {
        method: "POST",
        body: JSON.stringify({ ...values, tenant_id: scope ?? null }),
      }),
    onSuccess: (data) => {
      setOpen(false);
      setLinkExisting(false);
      message.success(`${data.name} 已创建，可从额度分配页划拨额度`);
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "创建失败"),
  });

  const act = useMutation({
    mutationFn: (payload: { id: number; action: "disable" | "enable" }) =>
      api(`/v1/members/${payload.id}/${payload.action}`, { method: "POST" }),
    onSuccess: () => {
      message.success("已更新成员");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "操作失败"),
  });

  const sync = useMutation({
    mutationFn: () =>
      api<RelayReport>(scope ? `/v1/relay/sync?tenant_id=${scope}` : "/v1/relay/sync", { method: "POST" }),
    onSuccess: (data) => {
      const bad = data.members.filter((item) => !item.ok).length;
      if (bad) message.warning(`已同步，${bad} 名成员的中转站余额或状态与本地不一致`);
      else message.success("已从中转站同步，余额和状态一致");
      void client.invalidateQueries({ queryKey: ["members"] });
      void client.invalidateQueries({ queryKey: ["tenants"] });
      void client.invalidateQueries({ queryKey: ["quota-summary"] });
      void client.invalidateQueries({ queryKey: ["relay-status"] });
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "同步失败"),
  });

  const revoke = useMutation({
    mutationFn: (id: number) => api(`/v1/devices/${id}/revoke`, { method: "POST" }),
    onSuccess: () => {
      message.success("这台设备已下线，中转站账号仍然可用");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "下线失败"),
  });

  return (
    <>
      <PageHead
        title="成员管理"
        description="创建时立即开通中转站账号，并绑定审核专属分组。"
        extra={
          <Space>
            {user.role === "platform_admin" ? (
              <Select
                allowClear
                placeholder="全部公司"
                style={{ width: 180 }}
                value={tenantId}
                onChange={setTenantId}
                options={(tenants.data ?? []).map((item) => ({ value: item.id, label: item.name }))}
              />
            ) : null}
            {user.relay_live ? (
              <Button onClick={() => sync.mutate()} loading={sync.isPending}>
                从中转站刷新
              </Button>
            ) : null}
            <Button type="primary" onClick={() => setOpen(true)} disabled={user.role === "platform_admin" && !scope}>
              邀请成员
            </Button>
          </Space>
        }
      />
      <Alert
        style={{ marginBottom: 16 }}
        type="warning"
        showIcon
        message="开通前请确认：中转站的后台模式必须关闭，自助注册也必须关闭。后台模式开启时，非 admin 账号登录会直接 403，客户端无法使用。"
      />
      <Card>
        <Table
          rowKey="id"
          dataSource={members.data ?? []}
          pagination={false}
          scroll={{ x: 980 }}
          columns={[
            {
              title: "成员",
              render: (_value, row) => (
                <div>
                  <b>{row.name}</b>
                  <div>
                    <Typography.Text type="secondary">{row.email}</Typography.Text>
                  </div>
                </div>
              ),
            },
            { title: "角色", dataIndex: "role_label" },
            { title: "中转站用户", dataIndex: "relay_user_id", render: (value: string) => value || "—" },
            { title: "可用分组", dataIndex: "group_name", render: (value: string) => value || "—" },
            {
              title: "状态",
              render: (_value, row) => <StatusTag status={row.status} label={row.overdraft ? "透支" : row.status_label} />,
            },
            { title: "余额", render: (_value, row) => (row.overdraft ? "¥0.00" : money(row.balance)) },
            {
              title: "中转站余额",
              render: (_value, row) => {
                if (!row.relay_checked_at) return <Typography.Text type="secondary">未同步</Typography.Text>;
                const diff = row.relay_diff ?? 0;
                const statusBad = row.relay_status && row.relay_status !== row.status;
                return (
                  <Space size={4} wrap>
                    <span>{money(row.relay_balance)}</span>
                    {diff === 0 ? <Tag color="green">一致</Tag> : <Tag color="red">差额 {diff.toFixed(2)}</Tag>}
                    {statusBad ? <Tag color="orange">状态 {row.relay_status}</Tag> : null}
                  </Space>
                );
              },
            },
            {
              title: "最近同步",
              dataIndex: "relay_checked_at",
              render: (value: string | null) => (value ? when(value) : "—"),
            },
            { title: "本月消耗", dataIndex: "month_cost", render: (value: number) => money(value) },
            {
              title: "设备",
              render: (_value, row) => (
                <Button type="link" size="small" onClick={() => setDevicesOf(row)}>
                  {row.devices} 台
                </Button>
              ),
            },
            {
              title: "操作",
              render: (_value, row) =>
                row.status === "active" ? (
                  <Popconfirm
                    title={`禁用 ${row.name}`}
                    description="会停用中转站账号、吊销密钥对应的登录、解绑全部设备，并立刻腾出一个席位。"
                    okText="禁用"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => act.mutate({ id: row.id, action: "disable" })}
                  >
                    <Button type="link" size="small" danger>
                      禁用
                    </Button>
                  </Popconfirm>
                ) : (
                  <Button type="link" size="small" onClick={() => act.mutate({ id: row.id, action: "enable" })}>
                    恢复
                  </Button>
                ),
            },
          ]}
        />
      </Card>
      <Alert
        style={{ marginTop: 16 }}
        type="info"
        showIcon
        message="账号会立即加入专属分组并创建审核 Key。额度需在额度分配页单独划拨。「从中转站刷新」只读余额和用量，不会改动本地账本。"
      />
      <Modal title="新建成员" open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" initialValues={{ role: "member", link_existing: false }} onFinish={(values) => create.mutate(values)}>
          <Form.Item name="name" label="姓名" rules={[{ required: true, message: "请填写姓名" }]}>
            <Input placeholder="例如：张剪辑" />
          </Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "请填写邮箱" }]}>
            <Input />
          </Form.Item>
          <Form.Item name="link_existing" valuePropName="checked">
            <Checkbox onChange={(event) => setLinkExisting(event.target.checked)}>
              该邮箱已经在中转站注册过，直接绑定已有账号
            </Checkbox>
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={linkExisting ? [] : [{ required: true, min: 8, message: "密码至少 8 位" }]}
            extra={linkExisting ? "绑定已有账号时不改密码，沿用中转站的密码。" : undefined}
          >
            <Input.Password
              autoComplete="new-password"
              disabled={linkExisting}
              placeholder={linkExisting ? "无需填写" : undefined}
            />
          </Form.Item>
          <Form.Item name="role" label="角色">
            <Select
              options={user.role === "platform_admin"
                ? [{ value: "member", label: "普通用户" }, { value: "tenant_admin", label: "公司管理员" }]
                : [{ value: "member", label: "普通用户" }]}
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={create.isPending}>
            创建账号
          </Button>
        </Form>
      </Modal>
      <Modal title={devicesOf ? `${devicesOf.name} 的设备` : "设备"} open={Boolean(devicesOf)} onCancel={() => setDevicesOf(null)} footer={null}>
        <Table
          rowKey="id"
          pagination={false}
          dataSource={devices.data ?? []}
          locale={{ emptyText: "没有设备" }}
          columns={[
            { title: "设备", dataIndex: "name" },
            { title: "版本", dataIndex: "client_version", render: (value: string) => value || "—" },
            { title: "状态", render: (_value, row) => <StatusTag status={row.active ? "active" : "disabled"} label={row.active ? "在线" : "已下线"} /> },
            {
              title: "",
              render: (_value, row) =>
                row.active ? (
                  <Popconfirm title="只下线这一台，不禁用账号" onConfirm={() => revoke.mutate(row.id)}>
                    <Button type="link" size="small">
                      下线
                    </Button>
                  </Popconfirm>
                ) : null,
            },
          ]}
        />
      </Modal>
    </>
  );
}
