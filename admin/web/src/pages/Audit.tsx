import { useQuery } from "@tanstack/react-query";
import { Card, Table } from "antd";
import { api } from "../api";
import { when } from "../format";
import type { AuditRow } from "../types";
import { PageHead, StatusTag } from "../ui";

function changeText(row: AuditRow): string {
  const after = row.after ?? {};
  if (typeof after.amount === "number") return String(after.amount);
  if (typeof after.status === "string" && row.before && typeof row.before.status === "string") {
    return `${row.before.status} → ${after.status}`;
  }
  if (typeof after.seats === "number" && row.before && typeof row.before.seats === "number") {
    return `席位 ${row.before.seats} → ${after.seats}`;
  }
  return row.summary;
}

export function Audit() {
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
            { title: "动作", dataIndex: "action" },
            { title: "对象", dataIndex: "object" },
            { title: "变更", render: (_value, row) => changeText(row) },
            { title: "IP", dataIndex: "ip" },
          ]}
        />
      </Card>
    </>
  );
}
