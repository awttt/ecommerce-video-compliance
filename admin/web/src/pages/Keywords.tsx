import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Checkbox,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Tooltip,
  Typography,
} from "antd";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { when } from "../format";
import type { RulesPayload, Submission, Tenant, User } from "../types";
import { CategoryTag, PageHead, SourceTag, StatusTag } from "../ui";

const CATEGORIES = ["价格", "规格", "品牌", "夸大宣传"];

export function Keywords() {
  const user = useOutletContext<User>();
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const [tab, setTab] = useState("pending");
  const [open, setOpen] = useState(false);
  const [rejectId, setRejectId] = useState<number | null>(null);
  const [forceId, setForceId] = useState<number | null>(null);
  const [tenantId, setTenantId] = useState<number | undefined>(user.tenant_id ?? undefined);
  const [category, setCategory] = useState("价格");
  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api<Tenant[]>("/v1/tenants"),
    enabled: user.role === "platform_admin",
  });
  const scope = user.role === "platform_admin" ? tenantId : user.tenant_id;
  const submissions = useQuery({
    queryKey: ["submissions"],
    queryFn: () => api<Submission[]>("/v1/rules/submissions"),
  });
  const rules = useQuery({
    queryKey: ["rules", scope ?? "self"],
    queryFn: () => api<RulesPayload>(scope ? `/v1/rules?since=0&tenant_id=${scope}` : "/v1/rules?since=0"),
    enabled: user.role !== "member",
  });

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["submissions"] });
    void client.invalidateQueries({ queryKey: ["rules"] });
    void client.invalidateQueries({ queryKey: ["me"] });
    void client.invalidateQueries({ queryKey: ["dashboard"] });
  };

  const submit = useMutation({
    mutationFn: (values: { text: string; category: string; source: string; scope: string }) =>
      api("/v1/rules/submissions", {
        method: "POST",
        body: JSON.stringify({ ...values, tenant_id: scope ?? null }),
      }),
    onSuccess: () => {
      setOpen(false);
      message.success("已提交，等待审批");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "提交失败"),
  });

  const review = useMutation({
    mutationFn: (payload: { id: number; action: "approve" | "reject"; remark?: string; force?: boolean }) =>
      api(`/v1/rules/submissions/${payload.id}/${payload.action}`, {
        method: "POST",
        body: JSON.stringify({ remark: payload.remark ?? "", force: Boolean(payload.force) }),
      }),
    onSuccess: () => {
      setRejectId(null);
      setForceId(null);
      message.success("已写入审批结果");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "审批失败"),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api(`/v1/rules/entries/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      message.success("已软删除，客户端下次增量拉取时不再包含它");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "删除失败"),
  });

  const saveRules = useMutation({
    mutationFn: (values: { categories: string[]; ignore_text: string; note: string }) =>
      api("/v1/rules/settings", {
        method: "PUT",
        body: JSON.stringify({
          tenant_id: scope ?? null,
          categories: values.categories,
          ignore_phrases: values.ignore_text.split(/\n+/).map((item) => item.trim()).filter(Boolean),
          note: values.note,
        }),
      }),
    onSuccess: () => {
      message.success("公司规则已保存");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "保存失败"),
  });

  const rows = submissions.data ?? [];
  const pending = rows.filter((item) => item.status === "pending");
  const rejected = rows.filter((item) => item.status === "rejected");
  const canReview = user.role !== "member";

  return (
    <>
      <PageHead
        title={user.role === "member" ? "我的提交" : "关键词审批"}
        description="一条词要带类别和来源。全局词由平台管理员审，公司词由公司管理员审。不能审自己提交的词。"
        extra={
          <Space>
            {user.role === "platform_admin" ? (
              <Select
                placeholder="公司规则所属公司"
                style={{ width: 180 }}
                value={tenantId}
                onChange={setTenantId}
                options={(tenants.data ?? []).map((item) => ({ value: item.id, label: item.name }))}
              />
            ) : null}
            <Button type="primary" onClick={() => setOpen(true)}>
              新增命中词
            </Button>
          </Space>
        }
      />
      <Card>
        <Tabs
          activeKey={tab}
          onChange={setTab}
          items={[
            {
              key: "pending",
              label: `待审 ${pending.length}`,
              children: (
                <SubmissionTable
                  rows={pending}
                  userId={user.id}
                  role={user.role}
                  onApprove={(id) => review.mutate({ id, action: "approve" })}
                  onReject={setRejectId}
                  onForce={setForceId}
                />
              ),
            },
            {
              key: "approved",
              label: `已通过 ${rules.data?.phrases.length ?? rows.filter((item) => item.status === "approved").length}`,
              children: canReview ? (
                <Table
                  rowKey="id"
                  pagination={false}
                  dataSource={rules.data?.phrases ?? []}
                  columns={[
                    { title: "原文", dataIndex: "text", render: (value: string) => <b>{value}</b> },
                    { title: "类别", dataIndex: "category", render: (value: string) => <CategoryTag value={value} /> },
                    { title: "来源", dataIndex: "source", render: (value: string) => <SourceTag value={value} /> },
                    { title: "词库", dataIndex: "scope_label", render: (value: string) => <StatusTag status={value === "全局" ? "processing" : "pending"} label={value} /> },
                    { title: "版本", dataIndex: "version" },
                    {
                      title: "操作",
                      render: (_value, row) =>
                        row.scope === "global" && user.role !== "platform_admin" ? (
                          <Typography.Text type="secondary">全局词</Typography.Text>
                        ) : (
                          <Popconfirm title="软删除后不再下发" onConfirm={() => remove.mutate(row.id)}>
                            <Button type="link" size="small" danger>
                              删除
                            </Button>
                          </Popconfirm>
                        ),
                    },
                  ]}
                />
              ) : (
                <SubmissionTable rows={rows.filter((item) => item.status === "approved")} userId={user.id} role={user.role} />
              ),
            },
            {
              key: "rejected",
              label: `已驳回 ${rejected.length}`,
              children: <SubmissionTable rows={rejected} userId={user.id} role={user.role} />,
            },
            ...(canReview
              ? [
                  {
                    key: "settings",
                    label: "公司规则",
                    children: (
                      <Form
                        layout="vertical"
                        key={`${scope ?? "none"}-${rules.data?.version ?? 0}`}
                        initialValues={{
                          categories: rules.data?.categories ?? CATEGORIES,
                          ignore_text: (rules.data?.ignore_phrases ?? []).join("\n"),
                          note: rules.data?.note ?? "",
                        }}
                        onFinish={(values) => saveRules.mutate(values)}
                      >
                        <Form.Item name="categories" label="启用的类别">
                          <Checkbox.Group options={CATEGORIES} />
                        </Form.Item>
                        <Form.Item name="ignore_text" label="忽略词" extra="一行一个，最多 30 条。原文和忽略词完全一致，或原文只是忽略词里的一段时丢掉。更长的句子里夹着忽略词，不因此整句丢掉。">
                          <Input.TextArea rows={4} placeholder="包邮" />
                        </Form.Item>
                        <Form.Item name="note" label="补充说明" extra="最多 400 字，追加在系统提示末尾。不能放宽「禁止推测」。">
                          <Input.TextArea rows={4} maxLength={400} showCount />
                        </Form.Item>
                        <Button type="primary" htmlType="submit" loading={saveRules.isPending} disabled={!scope}>
                          保存规则
                        </Button>
                      </Form>
                    ),
                  },
                ]
              : []),
          ]}
        />
      </Card>
      <Alert
        style={{ marginTop: 16 }}
        type="info"
        showIcon
        message={rules.data?.speculation_policy || "只报告画面里看得见的文字，或口播里连续说出来的原话。禁止推测优先于补充说明。"}
        description="通过后版本号 +1。客户端用 GET /v1/rules?since=版本 拉取，和内置词合并。词里的 X 仍表示数字。夸大宣传只看口播里的绝对化用语和疗效承诺，不判断真假。画面不报这一类。"
      />
      <Modal title="新增命中词" open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form
          layout="vertical"
          initialValues={{ category: "价格", source: "口播", scope: user.role === "platform_admin" ? "global" : "tenant" }}
          onValuesChange={(changed) => {
            if (changed.category) setCategory(changed.category);
          }}
          onFinish={(values) => submit.mutate(values)}
        >
          <Form.Item name="text" label="原文" rules={[{ required: true, message: "请填写原文" }]} extra="1 到 40 字。口播词按原话连续出现才命中，X 表示数字。">
            <Input placeholder="例如：家居好物" />
          </Form.Item>
          <Form.Item name="category" label="类别">
            <Select options={CATEGORIES.map((item) => ({ value: item, label: item }))} />
          </Form.Item>
          <Form.Item name="source" label="来源">
            <Select
              options={(category === "夸大宣传" ? ["口播"] : ["口播", "画面"]).map((item) => ({ value: item, label: item }))}
            />
          </Form.Item>
          {user.role === "platform_admin" ? (
            <Form.Item name="scope" label="作用域">
              <Select
                options={[
                  { value: "global", label: "全局" },
                  { value: "tenant", label: "当前选中的公司" },
                ]}
              />
            </Form.Item>
          ) : null}
          <Button type="primary" htmlType="submit" loading={submit.isPending}>
            提交
          </Button>
        </Form>
      </Modal>
      <Modal title="驳回" open={rejectId !== null} onCancel={() => setRejectId(null)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={(values: { remark: string }) => rejectId && review.mutate({ id: rejectId, action: "reject", remark: values.remark })}>
          <Form.Item name="remark" label="备注" rules={[{ required: true, message: "请填写驳回原因" }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <Button danger type="primary" htmlType="submit" loading={review.isPending}>
            驳回
          </Button>
        </Form>
      </Modal>
      <Modal title="强制通过" open={forceId !== null} onCancel={() => setForceId(null)} footer={null} destroyOnClose>
        <Form layout="vertical" onFinish={(values: { remark: string }) => forceId && review.mutate({ id: forceId, action: "approve", remark: values.remark, force: true })}>
          <Typography.Paragraph>这是你自己提交的词。平台管理员可以带备注强制通过，这一步会写入审计。</Typography.Paragraph>
          <Form.Item name="remark" label="备注" rules={[{ required: true, message: "请填写备注" }]}>
            <Input.TextArea rows={3} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={review.isPending}>
            强制通过
          </Button>
        </Form>
      </Modal>
    </>
  );
}

function SubmissionTable({
  rows,
  userId,
  role,
  onApprove,
  onReject,
  onForce,
}: {
  rows: Submission[];
  userId: number;
  role: User["role"];
  onApprove?: (id: number) => void;
  onReject?: (id: number) => void;
  onForce?: (id: number) => void;
}) {
  return (
    <Table
      rowKey="id"
      pagination={false}
      dataSource={rows}
      scroll={{ x: 880 }}
      columns={[
        { title: "原文", dataIndex: "text", render: (value: string) => <b>{value}</b> },
        { title: "类别", dataIndex: "category", render: (value: string) => <CategoryTag value={value} /> },
        { title: "来源", dataIndex: "source", render: (value: string) => <SourceTag value={value} /> },
        { title: "提交人", dataIndex: "member_name" },
        { title: "词库", dataIndex: "scope_label" },
        { title: "时间", dataIndex: "created_at", render: (value: string) => when(value) },
        { title: "状态", render: (_value, row) => <StatusTag status={row.status} label={row.status_label} /> },
        { title: "备注", dataIndex: "remark", render: (value: string) => value || "—" },
        {
          title: "操作",
          render: (_value, row) => {
            if (row.status !== "pending" || !onApprove || !onReject || !onForce) return null;
            if (role === "member") return null;
            const mine = row.member_id === userId;
            if (mine && role !== "platform_admin") {
              return (
                <Tooltip title="不能审批自己提交的词">
                  <Typography.Text type="secondary">不可自审</Typography.Text>
                </Tooltip>
              );
            }
            if (mine) {
              return (
                <Button type="link" size="small" onClick={() => onForce(row.id)}>
                  强制通过
                </Button>
              );
            }
            if (row.scope === "global" && role !== "platform_admin") return null;
            return (
              <Space>
                <Popconfirm title="通过后版本号 +1" onConfirm={() => onApprove(row.id)}>
                  <Button type="link" size="small">
                    通过
                  </Button>
                </Popconfirm>
                <Button type="link" size="small" danger onClick={() => onReject(row.id)}>
                  驳回
                </Button>
              </Space>
            );
          },
        },
      ]}
    />
  );
}
