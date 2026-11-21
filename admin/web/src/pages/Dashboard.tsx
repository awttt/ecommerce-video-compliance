import { theme, Alert, Button, Card, Col, Row, Statistic, Table, Typography } from "antd";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useOutletContext } from "react-router-dom";
import { api } from "../api";
import { deltaText, fromNow, money } from "../format";
import type { Dashboard as DashboardData, User } from "../types";
import { CategoryTag, PageHead, SourceTag, StatusTag } from "../ui";

function Trend({ rows }: { rows: DashboardData["trend"] }) {
  const { token } = theme.useToken();
  const max = Math.max(...rows.map((row) => row.cost), 1);
  return (
    <div>
      <div className="trend">
        {rows.map((row) => (
          <div className="trend-col" key={row.date}>
            <span className="trend-value">{Math.round(row.cost)}</span>
            <i style={{ height: Math.max(4, (row.cost / max) * 112), background: token.colorPrimary }} />
          </div>
        ))}
      </div>
      <div className="trend">
        {rows.map((row) => (
          <span className="trend-label" key={row.date} style={{ flex: 1, textAlign: "center" }}>
            {row.date.slice(5)}
          </span>
        ))}
      </div>
    </div>
  );
}

export function Dashboard() {
  const user = useOutletContext<User>();
  const navigate = useNavigate();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["dashboard", user.id], queryFn: () => api<DashboardData>("/v1/dashboard") });
  const data = query.data;
  const report = data?.reconcile;
  const delta = report ? deltaText(report.month_cost, report.previous_month_cost) : null;
  const member = user.role === "member";

  return (
    <>
      <PageHead
        title="总览"
        description={member ? "这里只看你自己的余额、消耗和提交。" : "对账使用累计消耗；本月消耗单独展示。消耗数据按最近一次同步更新。"}
        extra={<Button onClick={() => void client.invalidateQueries({ queryKey: ["dashboard"] })}>刷新数据</Button>}
      />
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
        <Col xs={24} sm={12} lg={6}>
          <Card>
            <Statistic title={member ? "我的余额" : "成员余额合计"} value={report?.balance ?? 0} precision={2} prefix="¥" />
            <Typography.Text type="secondary">当前还能用的额度</Typography.Text>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card>
            <Statistic title={member ? "本月消耗" : "已分配累计"} value={member ? report?.month_cost ?? 0 : report?.allocated ?? 0} precision={2} prefix="¥" />
            <Typography.Text type="secondary">{member ? "本月累计" : `公司余额 ${money(report?.pool_remaining ?? 0)}`}</Typography.Text>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card>
            <Statistic title={member ? "待审提交" : "本月消耗"} value={member ? data?.pending_keywords ?? 0 : report?.month_cost ?? 0} precision={member ? 0 : 2} prefix={member ? undefined : "¥"} />
            <Typography.Text type={delta && !member ? "success" : "secondary"}>{member ? "等待管理员处理" : delta || "与上月对比"}</Typography.Text>
          </Card>
        </Col>
        <Col xs={24} sm={12} lg={6}>
          <Card>
            <Statistic title="待审关键词" value={data?.pending_keywords ?? 0} />
            <Typography.Text type="secondary">其中 {data?.custom_pending ?? 0} 条公司自定义词</Typography.Text>
          </Card>
        </Col>
      </Row>
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} lg={14}>
          <Card title="近 7 日消耗" extra={<Typography.Text type="secondary">单位：元</Typography.Text>}>
            <Trend rows={data?.trend ?? []} />
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
      <Card title="待处理" style={{ marginTop: 16 }}>
        <Table
          rowKey="id"
          pagination={false}
          dataSource={data?.pending ?? []}
          locale={{ emptyText: "没有待审命中词" }}
          columns={[
            { title: "状态", dataIndex: "status", width: 90, render: (_value, row) => <StatusTag status={row.status} label={row.status_label} /> },
            {
              title: "命中词",
              render: (_value, row) => (
                <span>
                  <SourceTag value={row.source} /> <CategoryTag value={row.category} /> {row.text}
                </span>
              ),
            },
            { title: "公司", dataIndex: "tenant_name" },
            { title: "时间", dataIndex: "created_at", render: (value: string) => fromNow(value) },
            {
              title: "",
              align: "right",
              render: () =>
                user.role === "member" ? null : (
                  <Button size="small" type="primary" onClick={() => navigate("/keywords")}>
                    去审批
                  </Button>
                ),
            },
          ]}
        />
      </Card>
    </>
  );
}