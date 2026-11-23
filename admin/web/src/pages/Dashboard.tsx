import { theme, Alert, Button, Card, Col, DatePicker, Radio, Row, Space, Statistic, Table, Typography } from "antd";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { consumptionTitle, rangeFor, type RangePreset } from "../dates";
import { deltaText, fromNow, money } from "../format";
import type { Consumption, Dashboard as DashboardData, User } from "../types";
import { PageHead } from "../ui";

const PRESETS: { label: string; value: RangePreset }[] = [
  { label: "今天", value: "today" },
  { label: "本周", value: "week" },
  { label: "本月", value: "month" },
  { label: "上月", value: "lastMonth" },
  { label: "近 7 天", value: "days7" },
  { label: "近 30 天", value: "days30" },
];

function Trend({ rows }: { rows: DashboardData["trend"] }) {
  const { token } = theme.useToken();
  const max = Math.max(...rows.map((row) => row.cost), 1);
  const dense = rows.length > 16;
  const labelStep = dense ? Math.ceil(rows.length / 8) : 1;
  return (
    <div>
      <div className="trend">
        {rows.map((row) => (
          <div className="trend-col" key={row.date}>
            {dense ? null : <span className="trend-value">{Math.round(row.cost)}</span>}
            <i style={{ height: Math.max(4, (row.cost / max) * 112), background: token.colorPrimary }} />
          </div>
        ))}
      </div>
      <div className="trend">
        {rows.map((row, index) => (
          <span className="trend-label" key={row.date} style={{ flex: 1, textAlign: "center" }}>
            {!dense || index % labelStep === 0 || index === rows.length - 1 ? row.date.slice(5) : ""}
          </span>
        ))}
      </div>
    </div>
  );
}

export function Dashboard() {
  const user = useOutletContext<User>();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["dashboard", user.id], queryFn: () => api<DashboardData>("/v1/dashboard") });
  const [preset, setPreset] = useState<RangePreset | "custom">("month");
  const [range, setRange] = useState<[string, string]>(() => rangeFor("month"));
  const trendRange = rangeFor("days7");
  const consumption = useQuery({
    queryKey: ["consumption", user.id, range[0], range[1]],
    placeholderData: keepPreviousData,
    queryFn: () => api<Consumption>(`/v1/overview/consumption?start=${range[0]}&end=${range[1]}`),
  });
  const trend = useQuery({
    queryKey: ["consumption", user.id, "days7", trendRange[0], trendRange[1]],
    queryFn: () => api<Consumption>(`/v1/overview/consumption?start=${trendRange[0]}&end=${trendRange[1]}`),
  });
  const data = query.data;
  const report = data?.reconcile;
  const spent = consumption.data;
  const member = user.role === "member";
  const spentTitle = consumptionTitle(preset);
  const spentValue = spent?.total ?? 0;
  const compared = spent?.comparison ? deltaText(spent.total, spent.comparison.previous_total, "较上月") : null;
  const spentHint = compared ?? (spent?.comparison ? "较上月 —" : spent ? `${spent.start} 至 ${spent.end}` : "按北京时间统计");

  function applyPreset(next: RangePreset) {
    setPreset(next);
    setRange(rangeFor(next));
  }

  return (
    <>
      <PageHead
        title="总览"
        description={member ? "这里只看你自己的余额和消耗。日期按北京时间。" : undefined}
        extra={
          <Button
            onClick={() => {
              void client.invalidateQueries({ queryKey: ["dashboard"] });
              void client.invalidateQueries({ queryKey: ["consumption"] });
            }}
          >
            刷新数据
          </Button>
        }
      />
      <Card style={{ marginBottom: 16 }} styles={{ body: { paddingBottom: 12 } }}>
        <Space wrap>
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            value={preset === "custom" ? "" : preset}
            options={PRESETS}
            onChange={(event) => applyPreset(event.target.value as RangePreset)}
          />
          <DatePicker.RangePicker
            allowClear={false}
            value={[dayjs(range[0]), dayjs(range[1])]}
            placeholder={["开始日期", "结束日期"]}
            onChange={(values) => {
              if (!values?.[0] || !values?.[1]) return;
              setPreset("custom");
              setRange([values[0].format("YYYY-MM-DD"), values[1].format("YYYY-MM-DD")]);
            }}
          />
          <Button type="primary" onClick={() => void consumption.refetch()}>
            查询
          </Button>
        </Space>
      </Card>
      {consumption.isError || trend.isError ? (
        <Alert
          style={{ marginBottom: 16 }}
          showIcon
          type="error"
          message={
            (consumption.error instanceof ApiError && consumption.error.message) ||
            (trend.error instanceof ApiError && trend.error.message) ||
            "查询消耗失败"
          }
        />
      ) : null}
      {report ? (
        <Alert
          style={{ marginBottom: 16 }}
          showIcon
          type={member ? "info" : report.ok ? "success" : "error"}
          message={member ? "你可以在后台直接审核视频" : report.ok ? "对账状态：正常" : report.overdraft ? "对账状态：不平，有成员透支" : "对账状态：不平"}
          description={
            member
              ? "上传、抽帧、画面和口播识别、剪辑和导出都在后台完成。识别会从你的账户余额扣费。"
              : `公司余额 ${money(report.balance)}，累计已消耗 ${money(report.cumulative_cost)}，公司总额 ${money(report.pool_total)}。${
                  report.overdraft_members.length ? `透支按 0 计入：${report.overdraft_members.join("、")}。` : ""
                }`
          }
        />
      ) : null}
      <Row gutter={[16, 16]}>
        <Col xs={24} sm={12} lg={member ? 12 : 8}>
          <Card>
            <Statistic title={member ? "我的余额" : "成员余额合计"} value={report?.balance ?? 0} precision={2} prefix="¥" />
            <Typography.Text type="secondary">当前还能用的额度</Typography.Text>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={member ? 12 : 8}>
          <Card>
            <Statistic title={member ? spentTitle : "已分配累计"} value={member ? spentValue : report?.allocated ?? 0} precision={2} prefix="¥" />
            <Typography.Text type="secondary">{member ? spentHint : `公司余额 ${money(report?.pool_remaining ?? 0)}`}</Typography.Text>
          </Card>
        </Col>
        {member ? null : (
          <Col xs={24} sm={12} lg={8}>
            <Card>
              <Statistic title={spentTitle} value={spentValue} precision={2} prefix="¥" />
              <Typography.Text type={compared ? "success" : "secondary"}>{spentHint}</Typography.Text>
            </Card>
          </Col>
        )}
      </Row>
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={14}>
          <Card
            title="近 7 日消耗"
            extra={<Typography.Text type="secondary">元</Typography.Text>}
          >
            <Trend rows={trend.data?.by_day ?? []} />
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title="最近操作">
            <Table
              size="middle"
              pagination={false}
              rowKey="id"
              dataSource={data?.recent ?? []}
              locale={{ emptyText: "还没有操作" }}
              columns={[
                { title: "动作", dataIndex: "summary" },
                { title: "时间", dataIndex: "created_at", width: 110, render: (value: string) => fromNow(value) },
              ]}
            />
          </Card>
        </Col>
      </Row>
      {member ? null : (
        <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
          {user.role === "platform_admin" ? (
            <Col xs={24} lg={10}>
              <Card title="按公司">
                <Table
                  size="middle"
                  rowKey="tenant_id"
                  pagination={{ pageSize: 8, hideOnSinglePage: true }}
                  dataSource={spent?.by_company ?? []}
                  locale={{ emptyText: "这个区间没有消耗" }}
                  columns={[
                    { title: "公司", dataIndex: "name" },
                    { title: "消耗", dataIndex: "cost", align: "right", render: (value: number) => money(value) },
                  ]}
                />
              </Card>
            </Col>
          ) : null}
          <Col xs={24} lg={user.role === "platform_admin" ? 14 : 24}>
            <Card title="按成员">
              <Table
                size="middle"
                rowKey="member_id"
                pagination={{ pageSize: 8, hideOnSinglePage: true }}
                dataSource={spent?.by_member ?? []}
                locale={{ emptyText: "这个区间没有消耗" }}
                columns={[
                  { title: "成员", dataIndex: "name" },
                  ...(user.role === "platform_admin" ? [{ title: "公司", dataIndex: "tenant_name" }] : []),
                  { title: "消耗", dataIndex: "cost", align: "right", render: (value: number) => money(value) },
                ]}
              />
            </Card>
          </Col>
        </Row>
      )}
    </>
  );
}