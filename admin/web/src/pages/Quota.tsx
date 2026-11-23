import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Col, Form, Input, InputNumber, Popconfirm, Radio, Row, Select, Statistic, Table, Tag } from "antd";
import { useMemo, useRef, useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { money, when } from "../format";
import type { Member, QuotaRecord, Reconcile, RelayReport, Tenant, User } from "../types";
import { PageHead, StatusTag } from "../ui";

interface BatchResult {
  succeeded: number;
  failed: number;
  results: { member_id: number; member_name: string; ok: boolean; detail: string }[];
}

export function Quota() {
  const user = useOutletContext<User>();
  const platform = user.role === "platform_admin";
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const keyRef = useRef(crypto.randomUUID());
  const [form] = Form.useForm();
  const [failures, setFailures] = useState<{ member_name: string; detail: string }[]>([]);
  const [tenantId, setTenantId] = useState<number | undefined>(user.tenant_id ?? undefined);
  const direction = Form.useWatch("direction", form) ?? "add";
  const selectedIds = (Form.useWatch("member_ids", form) as number[] | undefined) ?? [];
  const amount = Form.useWatch("amount", form) as number | null | undefined;
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
  const ownerId = useMemo(() => {
    const admins = (members.data ?? []).filter((item) => item.role === "tenant_admin");
    admins.sort((left, right) => left.id - right.id);
    return admins[0]?.id;
  }, [members.data]);
  const choices = (members.data ?? []).filter((item) => item.id !== ownerId);
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
    mutationFn: (values: { member_ids: number[]; amount: number; remark?: string; direction: "add" | "subtract" }) =>
      api<BatchResult>("/v1/quotas/allocate-batch", {
        method: "POST",
        body: JSON.stringify({ ...values, idempotency_key: keyRef.current }),
      }),
    onSuccess: (data, variables) => {
      const missed = data.results.filter((item) => !item.ok);
      void client.invalidateQueries({ queryKey: ["quota-summary"] });
      void client.invalidateQueries({ queryKey: ["quota-records"] });
      void client.invalidateQueries({ queryKey: ["members"] });
      if (missed.length) {
        form.setFieldValue(
          "member_ids",
          missed.map((item) => item.member_id),
        );
        setFailures(missed.map((item) => ({ member_name: item.member_name || "成员", detail: item.detail })));
        message.warning(`完成 ${data.succeeded} 人，${data.failed} 人未完成`);
        return;
      }
      keyRef.current = crypto.randomUUID();
      form.setFieldValue("member_ids", []);
      setFailures([]);
      message.success(variables.direction === "subtract" ? "已从所选成员退回" : "已分配给所选成员");
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "分配失败"),
  });
  const reclaim = direction === "subtract";
  const selectedCount = selectedIds.length;
  const total = selectedCount > 0 && typeof amount === "number" ? amount * selectedCount : 0;

  return (
    <>
      <PageHead
        title="额度分配"
        description="可以一次选多名成员，按同一金额分配或退回。重复提交不会重复扣款。"
        extra={
          user.role === "platform_admin" ? (
            <Select
              placeholder="选择公司"
              style={{ width: 180 }}
              value={tenantId}
              onChange={(value) => {
                setTenantId(value);
                form.setFieldValue("member_ids", []);
                setFailures([]);
              }}
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
            <Form form={form} layout="vertical" initialValues={{ direction: "add", member_ids: [] }} onFinish={(values) => submit.mutate(values)}>
              <Form.Item name="direction" label="方向">
                <Radio.Group
                  options={[
                    { value: "add", label: "分配给成员" },
                    { value: "subtract", label: "从成员退回" },
                  ]}
                />
              </Form.Item>
              <Form.Item name="member_ids" label="成员" rules={[{ required: true, message: "请选择成员" }]}>
                <Select
                  mode="multiple"
                  maxCount={20}
                  allowClear
                  showSearch
                  optionFilterProp="label"
                  placeholder="可多选，一次最多 20 人"
                  options={choices.map((item) => ({
                    value: item.id,
                    label: `${item.name}（${item.email}）余额 ${money(item.balance)}${item.status === "active" ? "" : " · 已停用"}`,
                  }))}
                />
              </Form.Item>
              <Form.Item name="amount" label={reclaim ? "每人退回（元）" : "每人分配（元）"} rules={[{ required: true, message: "请填写金额" }]}>
                <InputNumber min={0.01} precision={2} style={{ width: "100%" }} />
              </Form.Item>
              <Form.Item name="remark" label="备注">
                <Input placeholder="例如：11 月额度" />
              </Form.Item>
              {failures.length ? (
                <Alert
                  style={{ marginBottom: 12 }}
                  type="warning"
                  showIcon
                  message="这些人还没完成，再点一次会继续处理"
                  description={failures.map((item) => `${item.member_name}：${item.detail}`).join("；")}
                />
              ) : null}
              <Popconfirm
                title={reclaim ? "批量退回" : "批量分配"}
                description={
                  selectedCount
                    ? `${reclaim ? "从" : "向"} ${selectedCount} 名成员各${reclaim ? "退回" : "分配"} ${money(typeof amount === "number" ? amount : 0)}，合计 ${money(total)}`
                    : "请先选择成员并填写金额"
                }
                okText="确认"
                cancelText="取消"
                disabled={!selectedCount || submit.isPending}
                onConfirm={() => form.submit()}
              >
                <Button type="primary" danger={reclaim} loading={submit.isPending} disabled={(!scope && user.role === "platform_admin") || !selectedCount}>
                  {reclaim ? "批量退回" : "批量分配"}
                </Button>
              </Popconfirm>
              <TypographyHint remaining={report?.pool_remaining} count={selectedCount} />
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

function TypographyHint({ remaining, count }: { remaining?: number; count: number }) {
  return (
    <div style={{ marginTop: 8 }}>
      <span style={{ color: "rgba(0,0,0,.45)" }}>
        当前可分配 {money(remaining ?? 0)}。已选 {count} 人，一次最多 20 人。公司管理员的余额就是公司余额，不能选本人。未完成的再提交一次不会重复扣已经成功的。
      </span>
    </div>
  );
}
