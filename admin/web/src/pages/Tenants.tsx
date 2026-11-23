import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Checkbox, Form, Input, InputNumber, Modal, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import { money, when } from "../format";
import type { Tenant } from "../types";
import { PageHead, PageLoading, RefreshButton, StatusTag } from "../ui";

export function Tenants() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [stop, setStop] = useState<Tenant | null>(null);
  const [alsoDisable, setAlsoDisable] = useState(false);
  const query = useQuery({ queryKey: ["tenants"], queryFn: () => api<Tenant[]>("/v1/tenants") });
  const loading = query.isLoading;

  const create = useMutation({
    mutationFn: (values: { name: string; admin_email: string; admin_name: string; admin_password?: string; seats: number }) =>
      api<{ admin: { name: string }; linked_existing?: boolean }>("/v1/tenants", { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (data) => {
      setOpen(false);
      message.success(
        data.linked_existing
          ? `公司已创建，管理员 ${data.admin.name} 已绑定原有中转站账号`
          : `公司和管理员 ${data.admin.name} 已创建`,
      );
      void client.invalidateQueries({ queryKey: ["tenants"] });
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "创建失败"),
  });

  const status = useMutation({
    mutationFn: (payload: { id: number; status: string; disable_members: boolean }) =>
      api(`/v1/tenants/${payload.id}/status`, {
        method: "PUT",
        body: JSON.stringify({ status: payload.status, disable_members: payload.disable_members }),
      }),
    onSuccess: () => {
      setStop(null);
      setAlsoDisable(false);
      message.success("公司状态已更新");
      void client.invalidateQueries({ queryKey: ["tenants"] });
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "更新失败"),
  });

  const head = (
    <PageHead
      title="租户管理"
      extra={
        <Space>
          <RefreshButton loading={query.isFetching} onClick={() => void query.refetch()} />
          <Button type="primary" onClick={() => setOpen(true)}>
            新建公司
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
      <Card>
        <Table
          rowKey="id"
          dataSource={query.data ?? []}
          loading={query.isFetching}
          pagination={false}
          scroll={{ x: 1100 }}
          columns={[
            {
              title: "公司",
              render: (_value, row) => (
                <div>
                  <b>{row.name}</b>
                  <div>
                    <Typography.Text type="secondary">关键词：公司自定义词库</Typography.Text>
                  </div>
                </div>
              ),
            },
            { title: "状态", render: (_value, row) => <StatusTag status={row.status} label={row.status_label} /> },
            { title: "成员 / 席位", render: (_value, row) => `${row.seats_used} / ${row.seat_quota}` },
            { title: "计费分组", dataIndex: "relay_group_name", render: (value: string) => value || "—" },
            { title: "公司余额", dataIndex: "pool_remaining", render: (value: number) => money(value) },
            { title: "本月消耗", dataIndex: "month_cost", render: (value: number) => money(value) },
            {
              title: "账户余额",
              dataIndex: "relay_balance",
              render: (value: number, row) =>
                row.relay_checked_at ? money(value) : <Typography.Text type="secondary">未同步</Typography.Text>,
            },
            {
              title: "与本地差额",
              render: (_value, row) => {
                if (!row.relay_checked_at || row.relay_diff === null) return "—";
                return row.relay_diff === 0 ? (
                  <Tag color="green">一致</Tag>
                ) : (
                  <Tag color="red">{row.relay_diff.toFixed(2)}</Tag>
                );
              },
            },
            { title: "创建时间", dataIndex: "created_at", render: (value: string) => when(value) },
            {
              title: "操作",
              render: (_value, row) => (
                <Space>
                  <Button type="link" size="small" onClick={() => navigate("/members")}>
                    成员
                  </Button>
                  {row.status === "active" ? (
                    <Button type="link" size="small" danger onClick={() => setStop(row)}>
                      停用
                    </Button>
                  ) : (
                    <Button type="link" size="small" onClick={() => status.mutate({ id: row.id, status: "active", disable_members: false })}>
                      启用
                    </Button>
                  )}
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Alert
        style={{ marginTop: 16 }}
        type="info"
        showIcon
        message="新建公司只绑定专属分组「电商审核专属-bot创建」。开通账号不再自动挂上其他分组。第二版再改成每家公司一个分组。"
      />
      <Modal title="新建公司" open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={(values) => create.mutate(values)} initialValues={{ seats: 10 }}>
          <Form.Item name="name" label="公司名称" rules={[{ required: true, message: "请填写公司名称" }]}>
            <Input placeholder="例如：锦程商贸" />
          </Form.Item>
          <Form.Item name="admin_name" label="管理员姓名">
            <Input placeholder="例如：李运营" />
          </Form.Item>
          <Form.Item name="admin_email" label="管理员邮箱" rules={[{ required: true, type: "email", message: "请填写邮箱" }]}>
            <Input placeholder="用于登录管理后台" />
          </Form.Item>
          <Form.Item
            name="admin_password"
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
          <Form.Item name="seats" label="席位数">
            <InputNumber min={1} max={10000} style={{ width: "100%" }} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={create.isPending}>
            创建公司和管理员
          </Button>
        </Form>
      </Modal>
      <Modal
        title={stop ? `停用 ${stop.name}` : "停用"}
        open={Boolean(stop)}
        onCancel={() => setStop(null)}
        okText="停用"
        okButtonProps={{ danger: true, loading: status.isPending }}
        onOk={() => stop && status.mutate({ id: stop.id, status: "disabled", disable_members: alsoDisable })}
      >
        <Typography.Paragraph>停用后，这家公司的成员不能再登录管理后台，也不能绑定设备。已经开通的账号仍可识别，除非同时禁用成员。</Typography.Paragraph>
        <Checkbox checked={alsoDisable} onChange={(event) => setAlsoDisable(event.target.checked)}>
          同时禁用全部成员（停用账号、吊销设备令牌、腾出席位）
        </Checkbox>
      </Modal>
    </>
  );
}
