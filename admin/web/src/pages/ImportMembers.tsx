import { useQueryClient } from "@tanstack/react-query";
import { App as AntApp, Button, Input, Modal, Space, Table, Tag, Typography } from "antd";
import { useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";
import {
  downloadImportTemplate,
  issueField,
  issuesFor,
  rowsFromWorkbook,
  type ImportDraft,
} from "../importMembers";
import type { Member } from "../types";

const MAX_FILE = 2 * 1024 * 1024;

export function ImportMembers({
  open,
  tenantId,
  memberEmails,
  onClose,
}: {
  open: boolean;
  tenantId?: number;
  memberEmails: string[];
  onClose: () => void;
}) {
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [rows, setRows] = useState<ImportDraft[]>([]);
  const [fileName, setFileName] = useState("");
  const [password, setPassword] = useState("123456");
  const [sending, setSending] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const passwordIssue = password.length > 0 && (password.length < 6 || password.length > 72) ? "密码需要在 6 到 72 位之间" : "";

  useEffect(() => {
    if (open) return;
    setRows([]);
    setFileName("");
    setPassword("123456");
    setSending(false);
    setProgress({ done: 0, total: 0 });
  }, [open]);

  const issues = issuesFor(rows, { members: memberEmails });
  const problemCount = Object.keys(issues).length;
  const pending = rows.filter((row) => row.status !== "ok");
  const canSend = rows.length > 0 && problemCount === 0 && !passwordIssue && !sending && pending.length > 0;

  function patch(key: string, partial: Partial<ImportDraft>) {
    setRows((current) =>
      current.map((row) => {
        if (row.key !== key || row.status === "ok") return row;
        return { ...row, ...partial, status: "ready", message: "", token: "" };
      }),
    );
  }

  function remove(key: string) {
    setRows((current) => current.filter((row) => row.key !== key));
  }

  async function load(file: File) {
    if (sending) return;
    const lower = file.name.toLowerCase();
    if (!lower.endsWith(".xlsx") && !lower.endsWith(".xls")) {
      message.error("请选择 xlsx 表格");
      return;
    }
    if (file.size > MAX_FILE) {
      message.error("表格太大，请只保留要邀请的人");
      return;
    }
    try {
      const next = await rowsFromWorkbook(await file.arrayBuffer());
      setRows(next);
      setFileName(file.name);
      setProgress({ done: 0, total: 0 });
    } catch (error) {
      message.error(error instanceof Error ? error.message : "表格读不出来");
    }
  }

  async function submit() {
    if (!canSend) return;
    const queue = rows.filter((row) => row.status !== "ok");
    setSending(true);
    setProgress({ done: 1, total: queue.length });
    let succeeded = 0;
    let failed = 0;
    let stop = false;
    for (let index = 0; index < queue.length; index += 1) {
      const row = queue[index];
      setProgress({ done: index + 1, total: queue.length });
      if (stop) {
        setRows((current) =>
          current.map((item) =>
            item.key === row.key ? { ...item, status: "skipped", message: "席位已满，没有继续导入" } : item,
          ),
        );
        failed += 1;
        continue;
      }
      setRows((current) =>
        current.map((item) => (item.key === row.key ? { ...item, status: "sending", message: "" } : item)),
      );
      try {
        const data = await api<Member & { linked_existing?: boolean }>("/v1/members", {
          method: "POST",
          body: JSON.stringify({
            email: row.email.trim(),
            name: row.name.trim(),
            password,
            role: "member",
            tenant_id: tenantId ?? null,
          }),
        });
        const done = data.linked_existing ? "已绑定原有中转站账号" : "已开通";
        setRows((current) =>
          current.map((item) =>
            item.key === row.key ? { ...item, status: "ok", message: done, token: "" } : item,
          ),
        );
        succeeded += 1;
      } catch (error) {
        const detail = error instanceof ApiError ? error.message : "导入失败";
        setRows((current) =>
          current.map((item) => (item.key === row.key ? { ...item, status: "fail", message: detail } : item)),
        );
        failed += 1;
        if (detail.includes("席位已满")) stop = true;
      }
    }
    setSending(false);
    if (succeeded > 0) void client.invalidateQueries({ queryKey: ["members"] });
    if (failed === 0) message.success(`已导入 ${succeeded} 人`);
    else if (succeeded === 0) message.error(`${failed} 人没有导入成功`);
    else message.warning(`已导入 ${succeeded} 人，${failed} 人没有成功`);
  }

  return (
    <Modal
      title="导入"
      open={open}
      width={880}
      onCancel={() => {
        if (!sending) onClose();
      }}
      maskClosable={!sending}
      keyboard={!sending}
      destroyOnClose
      footer={
        <Space>
          <Button onClick={onClose} disabled={sending}>
            取消
          </Button>
          <Button type="primary" disabled={!canSend} loading={sending} onClick={() => void submit()}>
            {sending ? `正在导入 ${progress.done}/${progress.total}` : "确定"}
          </Button>
        </Space>
      }
    >
      <Space style={{ marginBottom: rows.length > 0 ? 12 : 0 }}>
        <Button onClick={() => fileRef.current?.click()} disabled={sending}>
          选择导入表
        </Button>
        <Button onClick={() => void downloadImportTemplate()} disabled={sending}>
          导入模板
        </Button>
        <input
          ref={fileRef}
          type="file"
          accept=".xlsx,.xls,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void load(file);
          }}
        />
        {fileName ? <Typography.Text type="secondary">{fileName}</Typography.Text> : null}
      </Space>
      <div style={{ margin: "12px 0" }}>
        <Typography.Text>初始密码</Typography.Text>
        <Input.Password
          style={{ marginTop: 4 }}
          autoComplete="new-password"
          placeholder="新账号必填，已有账号可留空"
          value={password}
          disabled={sending}
          status={passwordIssue ? "error" : undefined}
          onChange={(event) => setPassword(event.target.value)}
        />
        {passwordIssue ? <Typography.Text type="danger">{passwordIssue}</Typography.Text> : null}
      </div>
      {rows.length > 0 ? (
        <>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
            共 {rows.length} 人
            {problemCount > 0 ? `，其中 ${problemCount} 人需要先修改或删除` : ""}
            {sending ? `。正在导入第 ${progress.done} 个，共 ${progress.total} 个，一次只导入一条。` : ""}
          </Typography.Paragraph>
          <Table
            rowKey="key"
            size="small"
            dataSource={rows}
            pagination={false}
            scroll={{ y: 360 }}
            columns={[
              { title: "职位", dataIndex: "title", width: 100, render: (value: string) => value || "—" },
              {
                title: "姓名",
                width: 140,
                render: (_value, row) => (
                  <Input
                    value={row.name}
                    disabled={sending || row.status === "ok"}
                    status={issueField(issues[row.key] ?? "") === "name" ? "error" : undefined}
                    onChange={(event) => patch(row.key, { name: event.target.value })}
                  />
                ),
              },
              {
                title: "邮箱",
                render: (_value, row) => (
                  <Input
                    value={row.email}
                    disabled={sending || row.status === "ok"}
                    status={issueField(issues[row.key] ?? "") === "email" ? "error" : undefined}
                    onChange={(event) => patch(row.key, { email: event.target.value })}
                  />
                ),
              },
              {
                title: "结果",
                width: 200,
                render: (_value, row) => {
                  const issue = issues[row.key];
                  if (row.status === "ok") return <Tag color="success">{row.message}</Tag>;
                  if (row.status === "sending") return <Tag color="processing">导入中</Tag>;
                  if (row.status === "fail") return <Typography.Text type="danger">{row.message}</Typography.Text>;
                  if (row.status === "skipped") return <Typography.Text type="secondary">{row.message}</Typography.Text>;
                  if (issue) return <Typography.Text type="danger">{issue}</Typography.Text>;
                  return <Typography.Text type="secondary">待邀请</Typography.Text>;
                },
              },
              {
                title: "",
                width: 72,
                render: (_value, row) =>
                  row.status === "ok" ? null : (
                    <Button type="link" size="small" danger disabled={sending} onClick={() => remove(row.key)}>
                      删除
                    </Button>
                  ),
              },
            ]}
          />
        </>
      ) : null}
    </Modal>
  );
}
