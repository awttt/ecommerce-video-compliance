import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Checkbox, Form, Input, Modal, Select, Space, Table, Tabs, Typography } from "antd";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { when } from "../format";
import type { Tenant, User } from "../types";
import { PageHead, PageLoading, RefreshButton } from "../ui";

const CATEGORIES = ["价格", "规格", "品牌", "夸大宣传"];
const GROUPS: { source: string; category: string; title: string }[] = [
  { source: "口播", category: "价格", title: "口播 · 价格" },
  { source: "口播", category: "规格", title: "口播 · 规格" },
  { source: "口播", category: "品牌", title: "口播 · 品牌" },
  { source: "画面", category: "价格", title: "画面 · 价格" },
  { source: "画面", category: "规格", title: "画面 · 规格" },
  { source: "画面", category: "品牌", title: "画面 · 品牌" },
];

interface Phrase {
  text: string;
  category: string;
  source: string;
}

interface RuleDocument {
  tenant_id: number;
  categories: string[];
  phrases: Phrase[];
  ignore_phrases: Array<string | { text: string }>;
  note: string;
  version: number;
  updated_at: string | null;
}

interface RuleSubmission {
  id: number;
  member_id: number;
  member_name: string;
  tenant_name: string;
  op_label: string;
  target: string;
  summary: string;
  before: Record<string, string> | null;
  after: Record<string, string> | null;
  status: string;
  status_label: string;
  review_comment: string;
  created_at: string;
}

function groupKey(source: string, category: string) {
  return `${source}:${category}`;
}

function draftsFrom(phrases: Phrase[]) {
  const buckets = new Map<string, string[]>();
  for (const group of GROUPS) buckets.set(groupKey(group.source, group.category), []);
  for (const item of phrases) {
    const lines = buckets.get(groupKey(item.source === "画面" ? "画面" : "口播", item.category));
    if (lines && item.text) lines.push(item.text);
  }
  return Object.fromEntries([...buckets].map(([key, lines]) => [key, lines.join("\n")]));
}

function phrasesFrom(drafts: Record<string, string>): { phrases: Phrase[]; error: string } {
  const phrases: Phrase[] = [];
  const seen = new Set<string>();
  for (const group of GROUPS) {
    for (const line of (drafts[groupKey(group.source, group.category)] ?? "").split(/\n+/)) {
      const text = line.replace(/\s+/g, "");
      if (!text) continue;
      if ([...text].length > 40) return { phrases: [], error: `「${line.trim()}」超过 40 个字` };
      const key = `${group.source}:${group.category}:${text}`;
      if (seen.has(key)) continue;
      seen.add(key);
      phrases.push({ text, category: group.category, source: group.source });
    }
  }
  if (phrases.length > 300) return { phrases: [], error: "额外命中词最多 300 条" };
  return { phrases, error: "" };
}

function ignoreLines(items: RuleDocument["ignore_phrases"]): string {
  return items.map((item) => (typeof item === "string" ? item : item.text)).filter(Boolean).join("\n");
}

function sideText(target: string, value: Record<string, string> | null): string {
  if (!value) return "无";
  if (target === "phrase") return `${value.source || ""} · ${value.category || ""} · ${value.text || ""}`;
  if (target === "category") return value.name || "无";
  return value.text || "空";
}

function ChangePreview({ row }: { row: RuleSubmission }) {
  return (
    <div>
      <Typography.Paragraph>{row.summary}</Typography.Paragraph>
      <Typography.Paragraph>提交人：{row.member_name || "—"}</Typography.Paragraph>
      {row.before ? <Typography.Paragraph>原来：{sideText(row.target, row.before)}</Typography.Paragraph> : null}
      {row.after ? <Typography.Paragraph>改成：{sideText(row.target, row.after)}</Typography.Paragraph> : null}
    </div>
  );
}

export function SharedRules() {
  const user = useOutletContext<User>();
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const reviewer = user.permissions.includes("shared_rule.review");
  const [tenantId, setTenantId] = useState<number | undefined>(user.tenant_id ?? undefined);
  const [tab, setTab] = useState(reviewer ? "pending" : "current");
  const [rejectId, setRejectId] = useState<number | null>(null);
  const [viewing, setViewing] = useState<RuleSubmission | null>(null);
  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api<Tenant[]>("/v1/tenants"),
    enabled: user.role === "platform_admin",
  });
  const scope = user.role === "platform_admin" ? tenantId : user.tenant_id;
  const document = useQuery({
    queryKey: ["company-rules", scope ?? "none"],
    queryFn: () => api<RuleDocument>(scope ? `/v1/company-rules?tenant_id=${scope}` : "/v1/company-rules"),
    enabled: scope != null,
  });
  const pending = useQuery({
    queryKey: ["company-rules", "pending"],
    queryFn: () => api<RuleSubmission[]>("/v1/company-rules/submissions/pending"),
    enabled: reviewer,
  });

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["company-rules"] });
    void client.invalidateQueries({ queryKey: ["me"] });
  };

  const save = useMutation({
    mutationFn: (values: { categories: string[]; drafts: Record<string, string>; ignore_text: string; note: string }) => {
      const parsed = phrasesFrom(values.drafts);
      if (parsed.error) return Promise.reject(new Error(parsed.error));
      return api<RuleDocument>("/v1/company-rules", {
        method: "PUT",
        body: JSON.stringify({
          tenant_id: scope ?? null,
          categories: values.categories,
          phrases: parsed.phrases,
          ignore_phrases: values.ignore_text.split(/\n+/).map((item) => item.trim()).filter(Boolean),
          note: values.note,
        }),
      });
    },
    onSuccess: () => {
      message.success("已写入规则表，审核工具点刷新后才会同步到本机");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : error instanceof Error ? error.message : "保存失败"),
  });

  const review = useMutation({
    mutationFn: (payload: { id: number; action: "approve" | "reject"; remark?: string }) =>
      api(`/v1/company-rules/submissions/${payload.id}/${payload.action}`, {
        method: "POST",
        body: JSON.stringify({ remark: payload.remark ?? "" }),
      }),
    onSuccess: () => {
      setRejectId(null);
      message.success("已写入审批结果");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "审批失败"),
  });

  const waiting = pending.data ?? [];
  const current = document.data;
  const loading =
    (user.role === "platform_admin" && tenants.isLoading) ||
    (scope != null && document.isLoading) ||
    (reviewer && pending.isLoading);
  const refreshing =
    (user.role === "platform_admin" && tenants.isFetching) ||
    (scope != null && document.isFetching) ||
    (reviewer && pending.isFetching);
  const reload = () => {
    if (user.role === "platform_admin") void tenants.refetch();
    if (scope != null) void document.refetch();
    if (reviewer) void pending.refetch();
  };
  const head = (
    <PageHead
      title="规则审核"
      extra={
        <Space>
          {user.role === "platform_admin" ? (
            <Select
              placeholder="选择公司"
              style={{ width: 180 }}
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
      <Card>
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            ...(reviewer
              ? [
                  {
                    key: "pending",
                    label: `待审 ${waiting.length}`,
                    children: (
                      <Table
                        rowKey="id"
                        pagination={false}
                        dataSource={waiting}
                        locale={{ emptyText: "没有待审规则" }}
                        columns={[
                          { title: "提交人", dataIndex: "member_name" },
                          { title: "操作", dataIndex: "op_label", width: 72 },
                          { title: "内容", dataIndex: "summary" },
                          { title: "时间", dataIndex: "created_at", width: 160, render: (value: string) => when(value) },
                          {
                            title: "操作",
                            render: (_value, row) =>
                              row.member_id === user.id ? (
                                "请由另一位公司管理员审批"
                              ) : (
                                <Space>
                                  <Button type="link" size="small" onClick={() => setViewing(row)}>
                                    查看
                                  </Button>
                                  <Button type="link" size="small" onClick={() => review.mutate({ id: row.id, action: "approve" })}>
                                    通过
                                  </Button>
                                  <Button type="link" size="small" danger onClick={() => setRejectId(row.id)}>
                                    驳回
                                  </Button>
                                </Space>
                              ),
                          },
                        ]}
                      />
                    ),
                  },
                ]
              : []),
            {
              key: "current",
              label: "当前规则",
              children: !scope ? (
                <Alert type="info" showIcon message="先选择公司" />
              ) : current ? (
                <Form
                  key={`${scope}-${current.version}`}
                  layout="vertical"
                  initialValues={{
                    categories: current.categories,
                    drafts: draftsFrom(current.phrases),
                    ignore_text: ignoreLines(current.ignore_phrases),
                    note: current.note,
                  }}
                  onFinish={(values) => save.mutate(values)}
                >
                  <Typography.Paragraph type="secondary">版本 {current.version}。直接保存会立刻更新这张表，不经过审批。</Typography.Paragraph>
                  <Form.Item name="categories" label="启用类别">
                    <Checkbox.Group options={CATEGORIES} />
                  </Form.Item>
                  {GROUPS.map((group) => (
                    <Form.Item key={groupKey(group.source, group.category)} label={group.title}>
                      <Form.Item name={["drafts", groupKey(group.source, group.category)]} noStyle>
                        <Input.TextArea rows={4} placeholder="一行一个" />
                      </Form.Item>
                    </Form.Item>
                  ))}
                  <Form.Item name="ignore_text" label="忽略词" extra="一行一个，最多 30 条。">
                    <Input.TextArea rows={3} placeholder="包邮" />
                  </Form.Item>
                  <Form.Item name="note" label="补充说明" extra="最多 400 字。不能放宽「禁止推测」。">
                    <Input.TextArea rows={3} maxLength={400} showCount />
                  </Form.Item>
                  {user.role === "member" ? null : (
                    <Button type="primary" htmlType="submit" loading={save.isPending}>
                      保存
                    </Button>
                  )}
                </Form>
              ) : (
                <Alert type="warning" showIcon message="规则还没有读出来" />
              ),
            },
          ]}
        />
      </Card>
      <Modal title="提交内容" open={viewing !== null} onCancel={() => setViewing(null)} footer={null} width={640}>
        {viewing ? <ChangePreview row={viewing} /> : null}
      </Modal>
      <Modal title="驳回" open={rejectId !== null} onCancel={() => setRejectId(null)} footer={null} destroyOnClose>
        <Form
          layout="vertical"
          onFinish={(values: { remark: string }) => rejectId && review.mutate({ id: rejectId, action: "reject", remark: values.remark })}
        >
          <Form.Item name="remark" label="原因" rules={[{ required: true, message: "请填写驳回原因" }]}>
            <Input.TextArea rows={3} maxLength={200} />
          </Form.Item>
          <Button danger type="primary" htmlType="submit" loading={review.isPending}>
            驳回
          </Button>
        </Form>
      </Modal>
    </>
  );
}
