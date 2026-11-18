import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { App as AntApp, Button, Card, Col, Form, Input, InputNumber, Radio, Row, Select, Statistic, Table } from "antd";
import { useRef, useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { money, when } from "../format";
import type { Member, QuotaRecord, Reconcile, Tenant, User } from "../types";
import { PageHead, StatusTag } from "../ui";

export function Quota() {
  const user = useOutletContext<User>();
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const keyRef = useRef(crypto.randomUUID());
  const [tenantId, setTenantId] = useState<number | undefined>(user.tenant_id ?? undefined);
  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api<Tenant[]>("/v1/tenants"),
    enabled: user.role === "platform_admin",
  });
  const scope = user.role === "platform_admin" ? tenantId : user.tenant_id ?? undefined;
  const summary = useQuery({
    queryKey: ["quota-summary", scope ?? "all"],
    queryFn: () => api<Reconcile>(scope ? `/v1/quotas/summary?tenant_id=${scope}` : "/v1/quotas/summary"),
  });
  const members = useQuery({
    queryKey: ["members", scope ?? "all"],
    queryFn: () => api<Member[]>(scope ? `/v1/members?tenant_id=${scope}` : "/v1/members"),
  });
  const records = useQuery({
    queryKey: ["quota-records", scope ?? "all"],
    queryFn: () => api<QuotaRecord[]>(scope ? `/v1/quotas/records?tenant_id=${scope}` : "/v1/quotas/records"),
  });
  const report = summary.data;

  const submit = useMutation({
    mutationFn: (values: { member_id: number; amount: number; remark: string; direction: "add" | "subtract" }) =>
      api("/v1/quotas/allocate", {
        method: "POST",
        body: JSON.stringify({ ...values, idempotency_key: keyRef.current }),
      }),
    onSuccess: () => {
      keyRef.current = crypto.randomUUID();
      message.success("额度已记账。失败时用同一笔再次提交，不会重复动账。");
      void client.invalidateQueries({ queryKey: ["quota-summary"] });
      void client.invalidateQueries({ queryKey: ["quota-records"] });
      void client.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (error) => message.error(error instanceof ApiError ? `${error.message} 用同一个幂等键再提交一次，不会重复动账。` : "分配失败"),
  });

  return (
    <>
      <PageHead
        title="额度分配"
        description="从公司池划到成员的中转站余额。先占住幂等键，再调中转站；退回只做减法，不加回已消耗。"
        extra={
          user.role === "platform_admin" ? (
            <Select
              placeholder="选择公司"
              style={{ width: 180 }}
              value={tenantId}
              onChange={setTenantId}
              options={(tenants.data ?? []).filter((item) => item.status === "active").map((item) => ({ value: item.id, label: item.name }))}
            />
          ) : null
        }
      />
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={14}>
          <Card title="公司池">
            <Row gutter={16}>
              <Col span={12} md={6}>
                <Statistic title="成员余额合计" value={report?.balance ?? 0} precision={2} prefix="¥" />
              </Col>
              <Col span={12} md={6}>
                <Statistic title="已分配" value={report?.allocated ?? 0} precision={2} prefix="¥" />
              </Col>
              <Col span={12} md={6}>
                <Statistic title="可分配" value={report?.pool_remaining ?? 0} precision={2} prefix="¥" valueStyle={{ color: "#52c41a" }} />
              </Col>
              <Col span={12} md={6}>
                <Statistic title="本月消耗" value={report?.month_cost ?? 0} precision={2} prefix="¥" />
              </Col>
            </Row>
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title="划拨" extra="先校验公司池剩余">
            <Form layout="vertical" initialValues={{ direction: "add" }} onFinish={(values) => submit.mutate(values)}>
              <Form.Item name="direction" label="方向">
                <Radio.Group
                  options={[
                    { value: "add", label: "分配给成员" },
                    { value: "subtract", label: "退回公司池" },
                  ]}
                />
              </Form.Item>
              <Form.Item name="member_id" label="成员" rules={[{ required: true, message: "请选择成员" }]}>
                <Select
                  options={(members.data ?? []).map((item) => ({
                    value: item.id,
                    label: `${item.name}（${item.relay_user_id || "未开通"}）${item.status === "active" ? "" : " · 已停用"}`,
                  }))}
                />
              </Form.Item>
              <Form.Item name="amount" label="金额（元）" rules={[{ required: true, message: "请填写金额" }]}>
                <InputNumber min={0.01} precision={2} style={{ width: "100%" }} />
              </Form.Item>
              <Form.Item name="remark" label="备注">
                <Input placeholder="例如：11 月额度" />
              </Form.Item>
              <Button type="primary" htmlType="submit" loading={submit.isPending} disabled={!scope && user.role === "platform_admin"}>
                确认
              </Button>
              <TypographyHint remaining={report?.pool_remaining} />
            </Form>
          </Card>
        </Col>
      </Row>
      <Card title="分配记录" style={{ marginTop: 16 }}>
        <Table
          rowKey="id"
          dataSource={records.data ?? []}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          columns={[
            { title: "时间", dataIndex: "created_at", render: (value: string) => when(value) },
            { title: "成员", dataIndex: "member_name" },
            { title: "方向", dataIndex: "direction_label" },
            { title: "金额", dataIndex: "amount", render: (value: number) => money(value) },
            { title: "周期", dataIndex: "period" },
            { title: "操作人", dataIndex: "operator_name" },
            { title: "幂等键", dataIndex: "idempotency_key" },
            { title: "状态", render: (_value, row) => <StatusTag status={row.status} label={row.status_label} /> },
          ]}
        />
      </Card>
    </>
  );
}

function TypographyHint({ remaining }: { remaining?: number }) {
  return (
    <div style={{ marginTop: 8 }}>
      <span style={{ color: "rgba(0,0,0,.45)" }}>当前可分配 {money(remaining ?? 0)}。失败重试不会重复加钱。</span>
    </div>
  );
}
