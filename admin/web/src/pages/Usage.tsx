import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Col, Progress, Row, Select, Space, Statistic, Table } from "antd";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { api } from "../api";
import { money, tokenText } from "../format";
import type { Tenant, UsageMember, UsageSummary, User } from "../types";
import { PageHead, PageLoading, RefreshButton } from "../ui";

export function Usage() {
  const user = useOutletContext<User>();
  const platform = user.role === "platform_admin";
  const [tenantId, setTenantId] = useState<number | undefined>(undefined);
  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api<Tenant[]>("/v1/tenants"),
    enabled: user.role === "platform_admin",
  });
  const scope = user.role === "platform_admin" ? tenantId : user.tenant_id ?? undefined;
  const summary = useQuery({
    queryKey: ["usage", scope ?? "all"],
    queryFn: () => api<UsageSummary>(scope ? `/v1/usage/summary?tenant_id=${scope}` : "/v1/usage/summary"),
  });
  const members = useQuery({
    queryKey: ["usage-members", scope ?? "all"],
    queryFn: () => api<UsageMember[]>(scope ? `/v1/usage/by-member?tenant_id=${scope}` : "/v1/usage/by-member"),
  });
  const data = summary.data;
  const loading = summary.isLoading || members.isLoading || (platform && tenants.isLoading);
  const refreshing = summary.isFetching || members.isFetching || (platform && tenants.isFetching);
  const reload = () => {
    void summary.refetch();
    void members.refetch();
    if (platform) void tenants.refetch();
  };
  const head = (
    <PageHead
      title="用量看板"
      extra={
        <Space>
          {user.role === "platform_admin" ? (
            <Select
              allowClear
              placeholder="全部公司"
              style={{ width: 160 }}
              value={tenantId}
              onChange={setTenantId}
              options={(tenants.data ?? []).map((item) => ({ value: item.id, label: item.name }))}
            />
          ) : null}
          <RefreshButton loading={refreshing} onClick={reload} />
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
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={platform ? 6 : 8}>
          <Card>
            <Statistic title="请求数" value={data?.requests ?? 0} />
            <span style={{ color: "rgba(0,0,0,.45)" }}>本月累计</span>
          </Card>
        </Col>
        {platform ? (
          <Col xs={24} sm={12} lg={6}>
            <Card>
              <Statistic title="Token 消耗" value={tokenText(data?.tokens ?? 0)} />
              <span style={{ color: "rgba(0,0,0,.45)" }}>
                输入 {tokenText(data?.input_tokens ?? 0)} / 输出 {tokenText(data?.output_tokens ?? 0)}
              </span>
            </Card>
          </Col>
        ) : null}
        <Col xs={24} sm={12} lg={platform ? 6 : 8}>
          <Card>
            <Statistic title="实际扣费" value={data?.cost ?? 0} precision={2} prefix="¥" />
            <span style={{ color: "rgba(0,0,0,.45)" }}>本月累计</span>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={platform ? 6 : 8}>
          <Card>
            <Statistic title="人均消耗" value={data?.average ?? 0} precision={2} prefix="¥" />
            <span style={{ color: "rgba(0,0,0,.45)" }}>{data?.active_members ?? 0} 名有消耗的成员</span>
          </Card>
        </Col>
      </Row>
      <Card title="按成员" style={{ marginTop: 16 }}>
        <Table
          rowKey="id"
          pagination={false}
          loading={members.isFetching}
          dataSource={members.data ?? []}
          columns={[
            { title: "成员", dataIndex: "name" },
            ...(platform ? [{ title: "账号 ID", dataIndex: "relay_user_id" }] : []),
            { title: "请求数", dataIndex: "requests" },
            ...(platform ? [{ title: "Token", dataIndex: "tokens", render: (value: number) => tokenText(value) }] : []),
            { title: "消耗（元）", dataIndex: "cost", render: (value: number) => money(value) },
            {
              title: "占比",
              dataIndex: "share",
              render: (value: number) => (
                <Space>
                  <Progress percent={Math.round(value * 100)} showInfo={false} style={{ width: 120, marginBottom: 0 }} />
                  <span>{(value * 100).toFixed(1)}%</span>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      {platform ? (
        <Alert
          style={{ marginTop: 16 }}
          type="warning"
          showIcon
          message="口径提醒"
          description="usage_logs.cost 已经是按分组倍率算完的实际扣费，不能再乘一次倍率，否则对账对不上。定时刷新还没挂到进程里，当前数字来自已经落下的快照。"
        />
      ) : null}
    </>
  );
}
