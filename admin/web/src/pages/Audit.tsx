import { useQuery } from "@tanstack/react-query";
import { Card, Table } from "antd";
import { useOutletContext } from "react-router-dom";
import { api } from "../api";
import { when } from "../format";
import type { AuditRow, User } from "../types";
import { PageHead, StatusTag } from "../ui";

const statusMap: Record<string, string> = {
  active: "正常",
  disabled: "已停用",
  pending: "待审",
  approved: "已通过",
  rejected: "已驳回",
};

function changeText(row: AuditRow): string {
  const after = row.after ?? {};
  if (typeof after.amount === "number") return String(after.amount);
  if (typeof after.status === "string" && row.before && typeof row.before.status === "string") {
    const beforeStatus = statusMap[row.before.status] || row.before.status;
    const afterStatus = statusMap[after.status] || after.status;
    return `${beforeStatus} → ${afterStatus}`;
  }
  if (typeof after.seats === "number" && row.before && typeof row.before.seats === "number") {
    return `席位 ${row.before.seats} → ${after.seats}`;
  }
  return "—";
}

export function Audit() {
  const user = useOutletContext<User>();
  const platform = user.role === "platform_admin";
  const query = useQuery({ queryKey: ["audit"], queryFn: () => api<AuditRow[]>("/v1/audit-logs") });
  return (
    <>
      <PageHead title="审计日志" description="只追加，不修改不删除。额度操作和命中词审批都会留在这里。" />
      <Card>
        <Table
          rowKey="id"
          dataSource={query.data ?? []}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          scroll={{ x: 960 }}
          columns={[
            { title: "时间", dataIndex: "created_at", render: (value: string) => when(value), width: 140 },
            { title: "操作人", dataIndex: "actor_name" },
            { title: "角色", dataIndex: "role", render: (value: string, row) => <StatusTag status={value} label={row.role_label} /> },
            { title: "公司", dataIndex: "tenant_name" },
            ...(platform ? [{ title: "动作代码", dataIndex: "action" }] : []),
            { title: "动作", dataIndex: "summary" },
            { title: "对象", dataIndex: "object" },
            { title: "变更", render: (_value, row) => changeText(row) },
            { title: "IP", dataIndex: "ip" },
          ]}
        />
      </Card>
    </>
  );
}
