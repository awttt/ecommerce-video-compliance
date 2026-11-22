import { Alert, Table, Typography } from "antd";
import { useEffect, useState } from "react";
import { fetchMyRuleSubmissions, type CompanyRuleSubmission } from "./api";

/** 审核规则抽屉里看自己提交的审批状态。提交和刷新在抽屉底部。 */
export function RuleSubmissionStatus({ open, reloadToken }: { open: boolean; reloadToken: number }) {
  const [rows, setRows] = useState<CompanyRuleSubmission[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    fetchMyRuleSubmissions()
      .then((items) => {
        if (!cancelled) {
          setRows(items);
          setError("");
        }
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "读取提交记录失败");
      });
    return () => {
      cancelled = true;
    };
  }, [open, reloadToken]);

  return (
    <div className="rule-group">
      <Typography.Text strong>我的提交</Typography.Text>
      <Typography.Paragraph type="secondary">通过前不会改本机正在用的规则。通过后点右下角的刷新，才会同步到本机。</Typography.Paragraph>
      {error ? <Alert type="warning" showIcon message={error} style={{ marginBottom: 12 }} /> : null}
      <Table
        size="small"
        rowKey="id"
        pagination={false}
        dataSource={rows}
        locale={{ emptyText: "还没有提交" }}
        columns={[
          { title: "操作", dataIndex: "op_label", width: 72 },
          { title: "内容", dataIndex: "summary" },
          { title: "状态", dataIndex: "status_label", width: 80 },
          {
            title: "原因",
            dataIndex: "review_comment",
            render: (value: string, row: CompanyRuleSubmission) => (row.status === "rejected" ? value || "—" : "—"),
          },
          { title: "时间", dataIndex: "created_at", width: 140, render: (value: string) => (value ? value.slice(0, 16).replace("T", " ") : "—") },
        ]}
      />
    </div>
  );
}
