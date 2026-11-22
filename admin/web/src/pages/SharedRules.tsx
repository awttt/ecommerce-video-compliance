import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Card, Form, Input, Modal, Select, Table, Tabs } from "antd";
import { useState } from "react";
import { useOutletContext } from "react-router-dom";
import { ApiError, api } from "../api";
import { when } from "../format";
import type { SharedRule, User } from "../types";
import { CategoryTag, PageHead, StatusTag } from "../ui";

const CATEGORIES = ["价格", "规格", "品牌", "夸大宣传"];

export function SharedRules() {
  const user = useOutletContext<User>();
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const reviewer = user.permissions.includes("shared_rule.review");
  const [tab, setTab] = useState(reviewer ? "pending" : "mine");
  const [open, setOpen] = useState(false);
  const [rejectId, setRejectId] = useState<number | null>(null);

  const mine = useQuery({
    queryKey: ["shared-rules", "mine"],
    queryFn: () => api<SharedRule[]>("/v1/shared-rules/mine"),
  });
  const approved = useQuery({
    queryKey: ["shared-rules", "approved"],
    queryFn: () => api<SharedRule[]>("/v1/shared-rules/approved"),
  });
  const pending = useQuery({
    queryKey: ["shared-rules", "pending"],
    queryFn: () => api<SharedRule[]>("/v1/shared-rules/pending"),
    enabled: reviewer,
  });

  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["shared-rules"] });
  };

  const submit = useMutation({
    mutationFn: (values: { word: string; category: string; description: string }) =>
      api<SharedRule>("/v1/shared-rules", { method: "POST", body: JSON.stringify(values) }),
    onSuccess: () => {
      setOpen(false);
      message.success("已提交，等待平台管理员审批");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "提交失败"),
  });

  const review = useMutation({
    mutationFn: (payload: { id: number; action: "approve" | "reject"; remark?: string }) =>
      api(`/v1/shared-rules/${payload.id}/${payload.action}`, {
        method: "POST",
        body: JSON.stringify({ remark: payload.remark ?? "", force: false }),
      }),
    onSuccess: () => {
      setRejectId(null);
      message.success("已写入审批结果");
      refresh();
    },
    onError: (error) => message.error(error instanceof ApiError ? error.message : "审批失败"),
  });

  const own = mine.data ?? [];
  const passed = approved.data ?? [];
  const waiting = pending.data ?? [];

  return (
    <>
      <PageHead
        title="共享违规词"
        description={
          reviewer
            ? "普通成员提交的违规词先放在待审。通过后所有公司都能看到，检测时也会用上。"
            : "提交后由平台管理员审批。通过的词所有公司和成员都能看到，检测时也会用上。"
        }
        extra={
          <Button type="primary" onClick={() => setOpen(true)}>
            提交违规词
          </Button>
        }
      />
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
                        locale={{ emptyText: "没有待审违规词" }}
                        columns={[
                          { title: "违规词", dataIndex: "word", render: (value: string) => <b>{value}</b> },
                          { title: "类别", dataIndex: "category", render: (value: string) => <CategoryTag value={value} /> },
                          { title: "说明", dataIndex: "description", render: (value: string) => value || "—" },
                          { title: "提交人", dataIndex: "member_name" },
                          { title: "公司", dataIndex: "tenant_name", render: (value: string) => value || "—" },
                          { title: "时间", dataIndex: "created_at", render: (value: string) => when(value) },
                          {
                            title: "操作",
                            render: (_value, row) => (
                              <>
                                <Button type="link" size="small" onClick={() => review.mutate({ id: row.id, action: "approve" })}>
                                  通过
                                </Button>
                                <Button type="link" size="small" danger onClick={() => setRejectId(row.id)}>
                                  驳回
                                </Button>
                              </>
                            ),
                          },
                        ]}
                      />
                    ),
                  },
                ]
              : [
                  {
                    key: "mine",
                    label: `我的提交 ${own.length}`,
                    children: <OwnTable rows={own} />,
                  },
                ]),
            {
              key: "approved",
              label: `已通过 ${passed.length}`,
              children: (
                <Table
                  rowKey="id"
                  pagination={false}
                  dataSource={passed}
                  locale={{ emptyText: "还没有已通过的共享违规词" }}
                  columns={[
                    { title: "违规词", dataIndex: "word", render: (value: string) => <b>{value}</b> },
                    { title: "类别", dataIndex: "category", render: (value: string) => <CategoryTag value={value} /> },
                    { title: "说明", dataIndex: "description", render: (value: string) => value || "—" },
                    { title: "提交人", dataIndex: "member_name" },
                    { title: "公司", dataIndex: "tenant_name", render: (value: string) => value || "—" },
                    { title: "通过时间", dataIndex: "reviewed_at", render: (value: string) => (value ? when(value) : "—") },
                  ]}
                />
              ),
            },
          ]}
        />
      </Card>
      {reviewer ? (
        <Card style={{ marginTop: 16 }} title="我的提交">
          <OwnTable rows={own} />
        </Card>
      ) : null}
      <Alert
        style={{ marginTop: 16 }}
        type="info"
        showIcon
        message="已通过的词在所有公司之间共用。待审和驳回只有提交人自己和平台管理员看得到。"
      />
      <Modal title="提交违规词" open={open} onCancel={() => setOpen(false)} footer={null} destroyOnClose>
        <Form layout="vertical" initialValues={{ category: "价格" }} onFinish={(values) => submit.mutate(values)}>
          <Form.Item name="word" label="违规词" rules={[{ required: true, message: "请填写违规词" }]} extra="1 到 40 字。空格会去掉。已在审核或已通过的词不能再交一次。">
            <Input placeholder="例如：全网最低" />
          </Form.Item>
          <Form.Item name="category" label="类别">
            <Select options={CATEGORIES.map((item) => ({ value: item, label: item }))} />
          </Form.Item>
          <Form.Item name="description" label="说明">
            <Input.TextArea rows={3} maxLength={200} placeholder="可选，告诉审批人为什么要加这条" />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={submit.isPending}>
            提交
          </Button>
        </Form>
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

function OwnTable({ rows }: { rows: SharedRule[] }) {
  return (
    <Table
      rowKey="id"
      pagination={false}
      dataSource={rows}
      locale={{ emptyText: "还没有提交" }}
      columns={[
        { title: "违规词", dataIndex: "word", render: (value: string) => <b>{value}</b> },
        { title: "类别", dataIndex: "category", render: (value: string) => <CategoryTag value={value} /> },
        {
          title: "状态",
          render: (_value, row) => <StatusTag status={row.status} label={row.status_label} />,
        },
        {
          title: "原因",
          dataIndex: "review_comment",
          render: (value: string, row) => (row.status === "rejected" ? value || "—" : "—"),
        },
        { title: "时间", dataIndex: "created_at", render: (value: string) => when(value) },
      ]}
    />
  );
}
