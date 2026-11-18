import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Table, Typography } from "antd";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { money } from "../format";
import type { Device, Invite, Member, Tenant, User } from "../types";
import { PageHead, StatusTag } from "../ui";

export function Members() {
  const user = useOutletContext<User>();
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [link, setLink] = useState("");
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
  const invites = useQuery({
    queryKey: ["invites", scope ?? "all"],
    queryFn: () => api<Invite[]>(scope ? `/v1/invites?tenant_id=${scope}` : "/v1/invites"),
  });
  const devices = useQuery({
    queryKey: ["devices", devicesOf?.id],
    queryFn: () => api<Device[]>(`/v1/members/${devicesOf?.id}/devices`),
    enabled: Boolean(devicesOf),
  });

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["members"] });
    void client.invalidateQueries({ queryKey: ["invites"] });
    void client.invalidateQueries({ queryKey: ["devices"] });
  };

  const invite = useMutation({
    mutationFn: (values: { email: string; name: string; role: string; initial_amount: number }) =>
      api<Invite>("/v1/members/invite", {
        method: "POST",
        body: JSON.stringify({ ...values, tenant_id: scope ?? null }),
      }),
    onSuccess: (data) => {
      setOpen(false);
      if (data.invite_token) setLink(`${location.origin}/invite/${data.invite_token}`);
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "邀请失败"),
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
        description="每个成员对应一个中转站用户。邀请邮件里只有链接，没有密码。"
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
      {(invites.data ?? []).length > 0 ? (
        <Card title="待接受的邀请" style={{ marginTop: 16 }}>
          <Table
            rowKey="id"
            pagination={false}
            dataSource={invites.data}
            columns={[
              { title: "姓名", dataIndex: "name" },
              { title: "邮箱", dataIndex: "email" },
              { title: "角色", dataIndex: "role_label" },
              { title: "状态", render: () => <StatusTag status="invited" label="待接受" /> },
            ]}
          />
        </Card>
      ) : null}
      <Alert
        style={{ marginTop: 16 }}
        type="info"
        showIcon
        message="对方设好密码后才创建中转站账号，并且只分配专属分组。分组不在可用列表里时，客户端不建画面密钥，提示联系管理员。"
      />
      <Modal title="邀请成员" open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" initialValues={{ role: "member", initial_amount: 0 }} onFinish={(values) => invite.mutate(values)}>
          <Form.Item name="name" label="姓名" rules={[{ required: true, message: "请填写姓名" }]}>
            <Input placeholder="例如：张剪辑" />
          </Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "请填写邮箱" }]}>
            <Input />
          </Form.Item>
          <Form.Item name="role" label="角色">
            <Select
              options={[
                { value: "member", label: "普通用户" },
                { value: "tenant_admin", label: "公司管理员" },
              ]}
            />
          </Form.Item>
          <Form.Item name="initial_amount" label="开通后分配的额度（元）">
            <InputNumber min={0} style={{ width: "100%" }} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={invite.isPending}>
            生成邀请链接
          </Button>
        </Form>
      </Modal>
      <Modal title="邀请链接" open={Boolean(link)} onCancel={() => setLink("")} footer={null}>
        <Typography.Paragraph copyable>{link}</Typography.Paragraph>
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
