import { useQuery } from "@tanstack/react-query";
import { Card, Table } from "antd";
import { useEffect, useState } from "react";
import { useOutletContext } from "react-router-dom";
import { api } from "../api";
import { AUDIT_PAGE_SIZE_DEFAULT, AUDIT_PAGE_SIZES, auditLogsPath, nextAuditPage } from "../auditPage";
import { when } from "../format";
import type { AuditPage, AuditRow, User } from "../types";
import { PageHead, PageLoading, RefreshButton, StatusTag } from "../ui";

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
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(AUDIT_PAGE_SIZE_DEFAULT);
  const query = useQuery({
    queryKey: ["audit", page, pageSize],
    queryFn: () => api<AuditPage>(auditLogsPath(page, pageSize)),
  });
  const total = query.data?.total ?? 0;
  useEffect(() => {
    const bounded = nextAuditPage(pageSize, page, pageSize, total);
    if (bounded.page !== page) setPage(bounded.page);
  }, [page, pageSize, total]);
  const head = (
    <PageHead
      title="审计日志"
      extra={<RefreshButton loading={query.isFetching} onClick={() => void query.refetch()} />}
    />
  );

  if (query.isLoading) {
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
          dataSource={query.data?.items ?? []}
          loading={query.isFetching}
          pagination={{
            current: page,
            pageSize,
            total,
            showSizeChanger: true,
            pageSizeOptions: AUDIT_PAGE_SIZES.map(String),
            onChange: (nextPage, nextSize) => {
              const next = nextAuditPage(pageSize, nextPage, nextSize, total);
              setPage(next.page);
              setPageSize(next.pageSize);
            },
          }}
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
