import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { money, when } from "../format";
import type { Member, RelayReport, Tenant, User } from "../types";
import { PageHead, PageLoading, RefreshButton, StatusTag } from "../ui";
import { ImportMembers } from "./ImportMembers";

export function Members() {
  const user = useOutletContext<User>();
  const platform = user.role === "platform_admin";
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
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
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["members"] });
  };
  const loading = members.isLoading || (platform && tenants.isLoading);
  const refreshing = members.isFetching || tenants.isFetching;
  const reload = () => {
    void members.refetch();
    if (platform) void tenants.refetch();
  };

  const create = useMutation({
    mutationFn: (values: { email: string; name: string; password?: string; role: string }) =>
      api<Member & { linked_existing?: boolean }>("/v1/members", {
        method: "POST",
        body: JSON.stringify({ ...values, tenant_id: scope ?? null }),
      }),
    onSuccess: (data) => {
      setOpen(false);
      message.success(
        data.linked_existing
          ? `${data.name} 已绑定原有中转站账号，沿用原来的密码`
          : `${data.name} 已创建，可从额度分配页划拨额度`,
      );
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
      const failed = data.failed?.length ?? 0;
      const bad = data.members.filter((item) => !item.ok).length;
      if (failed) message.warning(`已刷新，还有 ${failed} 人暂时没读到，请再点一次`);
      else if (bad) message.warning(`已刷新，${bad} 名成员的余额与记录不一致`);
      else message.success("已刷新，余额一致");
      void client.invalidateQueries({ queryKey: ["members"] });
      void client.invalidateQueries({ queryKey: ["tenants"] });
      void client.invalidateQueries({ queryKey: ["quota-summary"] });
      void client.invalidateQueries({ queryKey: ["relay-status"] });
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "同步失败"),
  });

  const head = (
    <PageHead
      title="成员管理"
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
          <RefreshButton loading={refreshing} onClick={reload} />
          {user.relay_live ? (
            <Button onClick={() => sync.mutate()} loading={sync.isPending}>
              刷新余额
            </Button>
          ) : null}
          <Button
            onClick={() => {
              if (platform && !scope) {
                message.warning("请先选择公司");
                return;
              }
              setImportOpen(true);
            }}
          >
            导入
          </Button>
          <Button
            type="primary"
            onClick={() => {
              if (platform && !scope) {
                message.warning("请先选择公司");
                return;
              }
              setOpen(true);
            }}
          >
            邀请成员
          </Button>
        </Space>
      }
    />
  );

  if (loading) {
    return (
      <>
        {head}
        <PageLoading />
      </>
    );
  }

  return (
    <>
      {head}
      {platform ? (
        <Alert
          style={{ marginBottom: 16 }}
          type="warning"
          showIcon
          message="开通前请确认：后台模式和自助注册都已关闭。后台模式开启时，非 admin 账号会直接 403，客户端无法使用。"
        />
      ) : null}
      <Card>
        <Table
          rowKey="id"
          dataSource={members.data ?? []}
          loading={members.isFetching}
          pagination={false}
          scroll={{ x: platform ? 980 : 760 }}
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
            ...(platform ? [{ title: "账号 ID", dataIndex: "relay_user_id", render: (value: string) => value || "—" }] : []),
            ...(platform ? [{ title: "可用分组", dataIndex: "group_name", render: (value: string) => value || "—" }] : []),
            {
              title: "状态",
              render: (_value, row) => <StatusTag status={row.status} label={row.overdraft ? "透支" : row.status_label} />,
            },
            { title: "余额", render: (_value, row) => (row.overdraft ? "¥0.00" : money(row.balance)) },
            ...(platform
              ? [
                  {
                    title: "账户余额",
                    render: (_value: any, row: Member) => {
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
                ]
              : []),
            { title: "本月消耗", dataIndex: "month_cost", render: (value: number) => money(value) },
            {
              title: "操作",
              render: (_value, row) =>
                row.status === "active" ? (
                  <Popconfirm
                    title={`禁用 ${row.name}`}
                    description="会停用这个账号、让已登录的设备全部下线，并立刻腾出一个席位。"
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
        message="账号创建后，额度需要在额度分配页单独划拨。「刷新余额」只读取最新余额和用量，不会改动分配记录。"
      />
      <ImportMembers
        open={importOpen}
        tenantId={scope}
        memberEmails={(members.data ?? []).map((item) => item.email)}
        onClose={() => setImportOpen(false)}
      />
      <Modal title="新建成员" open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" initialValues={{ role: "member" }} onFinish={(values) => create.mutate(values)}>
          <Form.Item name="name" label="姓名" rules={[{ required: true, message: "请填写姓名" }]}>
            <Input placeholder="例如：张剪辑" />
          </Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true, type: "email", message: "请填写邮箱" }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            extra="系统会按邮箱自动识别：已在中转站的账号直接绑定，沿用原密码，这里可以不填；还没有账号时，会用这个初始密码开通并完成绑定。"
            rules={[
              {
                validator: async (_rule, value: string) => {
                  if (!value) return;
                  if (value.length < 8) throw new Error("密码至少 8 位");
                },
              },
            ]}
          >
            <Input.Password autoComplete="new-password" placeholder="新账号必填，已有账号可留空" />
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
    </>
  );
}
