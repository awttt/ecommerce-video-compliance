import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Checkbox, Form, Input, InputNumber, Modal, Space, Table, Typography } from "antd";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import { money, when } from "../format";
import type { Tenant } from "../types";
import { PageHead, StatusTag } from "../ui";

export function Tenants() {
  const { message } = AntApp.useApp();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [stop, setStop] = useState<Tenant | null>(null);
  const [alsoDisable, setAlsoDisable] = useState(false);
  const [link, setLink] = useState("");
  const query = useQuery({ queryKey: ["tenants"], queryFn: () => api<Tenant[]>("/v1/tenants") });

  const create = useMutation({
    mutationFn: (values: { name: string; admin_email: string; admin_name: string; pool: number; seats: number }) =>
      api<{ invite_token: string }>("/v1/tenants", { method: "POST", body: JSON.stringify(values) }),
    onSuccess: (data) => {
      setOpen(false);
      setLink(`${location.origin}/invite/${data.invite_token}`);
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

  return (
    <>
      <PageHead
        title="租户管理"
        description="一个租户是一家入驻公司。中转站不感知公司，只保存对应的用户和余额。"
        extra={
          <Button type="primary" onClick={() => setOpen(true)}>
            新建公司
          </Button>
        }
      />
      <Card>
        <Table
          rowKey="id"
          dataSource={query.data ?? []}
          pagination={false}
          scroll={{ x: 960 }}
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
            { title: "中转站分组", dataIndex: "relay_group_name", render: (value: string) => value || "—" },
            { title: "公司池", dataIndex: "pool_total", render: (value: number) => money(value) },
            { title: "本月消耗", dataIndex: "month_cost", render: (value: number) => money(value) },
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
        message="新建公司只绑定专属分组「电商审核专属-bot创建」。中转站建号不再自动挂上其他分组。第二版再改成每家公司一个分组。"
      />
      <Modal title="新建公司" open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={(values) => create.mutate(values)} initialValues={{ pool: 5000, seats: 10 }}>
          <Form.Item name="name" label="公司名称" rules={[{ required: true, message: "请填写公司名称" }]}>
            <Input placeholder="例如：锦程商贸" />
          </Form.Item>
          <Form.Item name="admin_name" label="管理员姓名">
            <Input placeholder="例如：李运营" />
          </Form.Item>
          <Form.Item name="admin_email" label="管理员邮箱" rules={[{ required: true, type: "email", message: "请填写邮箱" }]}>
            <Input placeholder="用于登录管理后台" />
          </Form.Item>
          <Form.Item name="pool" label="初始公司池（元）">
            <InputNumber min={0} style={{ width: "100%" }} />
          </Form.Item>
          <Form.Item name="seats" label="席位数">
            <InputNumber min={1} max={10000} style={{ width: "100%" }} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={create.isPending}>
            创建并生成邀请链接
          </Button>
        </Form>
      </Modal>
      <Modal title="邀请链接" open={Boolean(link)} onCancel={() => setLink("")} footer={null}>
        <Typography.Paragraph>邮件发送还没接上，请把链接交给对方。链接 7 天内有效，对方自己设密码。</Typography.Paragraph>
        <Typography.Paragraph copyable>{link}</Typography.Paragraph>
      </Modal>
      <Modal
        title={stop ? `停用 ${stop.name}` : "停用"}
        open={Boolean(stop)}
        onCancel={() => setStop(null)}
        okText="停用"
        okButtonProps={{ danger: true, loading: status.isPending }}
        onOk={() => stop && status.mutate({ id: stop.id, status: "disabled", disable_members: alsoDisable })}
      >
        <Typography.Paragraph>停用后，这家公司的成员不能再登录管理后台，也不能绑定设备。已经在中转站里的账号仍可识别，除非同时禁用成员。</Typography.Paragraph>
        <Checkbox checked={alsoDisable} onChange={(event) => setAlsoDisable(event.target.checked)}>
          同时禁用全部成员（停用中转站账号、吊销设备令牌、腾出席位）
        </Checkbox>
      </Modal>
    </>
  );
}
