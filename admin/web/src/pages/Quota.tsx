import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Col, Form, Input, InputNumber, Radio, Row, Select, Statistic, Table, Tag } from "antd";
import { useRef, useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { money, when } from "../format";
import type { Member, QuotaRecord, Reconcile, RelayReport, Tenant, User } from "../types";
import { PageHead, StatusTag } from "../ui";

export function Quota() {
  const user = useOutletContext<User>();
  const platform = user.role === "platform_admin";
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
  const relay = useQuery({
    queryKey: ["relay-status", scope ?? "all"],
    queryFn: () => api<RelayReport>(scope ? `/v1/relay/status?tenant_id=${scope}` : "/v1/relay/status"),
    enabled: platform,
  });
  const sync = useMutation({
    mutationFn: () =>
      api<RelayReport>(scope ? `/v1/relay/sync?tenant_id=${scope}` : "/v1/relay/sync", { method: "POST" }),
    onSuccess: (data) => {
      const bad = data.members.filter((item) => !item.ok).length;
      if (bad) message.warning(`已刷新，${bad} 名成员有差额`);
      else message.success("已刷新余额");
      void client.invalidateQueries({ queryKey: ["relay-status"] });
      void client.invalidateQueries({ queryKey: ["quota-summary"] });
      void client.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "同步失败"),
  });
  const relayData = relay.data;

  const submit = useMutation({
    mutationFn: (values: { member_id: number; amount: number; remark: string; direction: "add" | "subtract" }) =>
      api("/v1/quotas/allocate", {
        method: "POST",
        body: JSON.stringify({ ...values, idempotency_key: keyRef.current }),
      }),
    onSuccess: () => {
      keyRef.current = crypto.randomUUID();
      message.success("额度已记账。");
      void client.invalidateQueries({ queryKey: ["quota-summary"] });
      void client.invalidateQueries({ queryKey: ["quota-records"] });
      void client.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "分配失败"),
  });

  return (
    <>
      <PageHead
        title="额度分配"
        description="把公司余额分给成员，或从成员收回。重复提交不会重复扣款。"
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
                <Statistic title="公司余额" value={report?.balance ?? 0} precision={2} prefix="¥" />
              </Col>
              <Col span={12} md={6}>
                <Statistic title="公司总额" value={report?.pool_total ?? 0} precision={2} prefix="¥" />
              </Col>
              <Col span={12} md={6}>
                <Statistic title="可划拨" value={report?.pool_remaining ?? 0} precision={2} prefix="¥" valueStyle={{ color: "#52c41a" }} />
              </Col>
              <Col span={12} md={6}>
                <Statistic title="累计已消耗" value={report?.cumulative_cost ?? 0} precision={2} prefix="¥" />
              </Col>
            </Row>
          </Card>
          {platform ? (
            <Card
              title="余额核对"
              style={{ marginTop: 16 }}
              extra={
                user.relay_live ? (
                  <Button size="small" onClick={() => sync.mutate()} loading={sync.isPending}>
                    刷新余额
                  </Button>
                ) : (
                  <Tag>本地演示</Tag>
                )
              }
            >
              {relayData?.synced ? (
                <>
                  <Row gutter={16}>
                    <Col span={12} md={6}>
                      <Statistic title="账号服务余额合计" value={relayData.totals.relay_balance} precision={2} prefix="¥" />
                    </Col>
                    <Col span={12} md={6}>
                      <Statistic
                        title="与本地差额"
                        value={relayData.totals.diff}
                        precision={2}
                        prefix="¥"
                        valueStyle={{ color: relayData.totals.diff === 0 ? "#52c41a" : "#cf1322" }}
                      />
                    </Col>
                    <Col span={12} md={6}>
                      <Statistic title="本月消耗" value={relayData.totals.month_cost} precision={2} prefix="¥" />
                    </Col>
                    <Col span={12} md={6}>
                      <Statistic title="累计消耗" value={relayData.totals.lifetime_cost} precision={2} prefix="¥" />
                    </Col>
                  </Row>
                  <Alert
                    style={{ marginTop: 12 }}
                    type={relayData.ok ? "success" : "warning"}
                    showIcon
                    message={`最近同步 ${when(relayData.checked_at)}`}
                    description={
                      relayData.ok
                        ? "账号服务余额和账号状态与本地一致。"
                        : `${relayData.members.filter((item) => !item.ok).length} 名成员存在余额差额或状态不一致，请在成员管理页查看。`
                    }
                  />
                </>
              ) : (
                <Alert
                  type="info"
                  showIcon
                  message="还没有从账号服务同步过"
                  description="点右上角「刷新余额」，只读取余额和用量，不会改动本地账本，也不会重复加款。"
                />
              )}
            </Card>
          ) : null}
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
                    label: `${item.name}（${item.email}）${item.status === "active" ? "" : " · 已停用"}`,
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
            ...(platform ? [{ title: "幂等键", dataIndex: "idempotency_key" }] : []),
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
      <span style={{ color: "rgba(0,0,0,.45)" }}>当前可分配 {money(remaining ?? 0)}。重复提交不会重复扣款。</span>
    </div>
  );
}
