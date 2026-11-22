import { Alert, Button, Form, Input, Select, Table, Typography } from "antd";
import { useEffect, useState } from "react";

interface SharedRule {
  id: number;
  word: string;
  category: string;
  description: string;
  status: string;
  status_label: string;
  review_comment: string;
}

const CATEGORIES = ["价格", "规格", "品牌", "夸大宣传"];

async function readError(response: Response): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: unknown };
    if (typeof data.detail === "string") return data.detail;
  } catch {
    return "请求失败";
  }
  return "请求失败";
}

async function load(path: string): Promise<SharedRule[]> {
  const response = await fetch(path);
  if (!response.ok) throw new Error(await readError(response));
  const data = (await response.json()) as SharedRule[];
  return Array.isArray(data) ? data : [];
}

/** 视频审核工具里提交和查看全公司共用的违规词。已通过的词检测时自动生效。 */
export function SharedRulesPanel() {
  const [approved, setApproved] = useState<SharedRule[]>([]);
  const [mine, setMine] = useState<SharedRule[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const [passed, own] = await Promise.all([load("/api/shared-rules/approved"), load("/api/shared-rules/mine")]);
    setApproved(passed);
    setMine(own);
  }

  useEffect(() => {
    void refresh().catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "读取共享违规词失败");
    });
  }, []);

  return (
    <div className="rule-group">
      <Typography.Text strong>共享违规词</Typography.Text>
      <Typography.Paragraph type="secondary">
        提交后由本公司管理员审批。通过的词所有人都能看到，之后的检测会自动带上，不用写进上面的本机规则。
      </Typography.Paragraph>
      {error ? <Alert type="warning" showIcon message={error} style={{ marginBottom: 12 }} /> : null}
      <Form
        layout="vertical"
        initialValues={{ category: "价格" }}
        onFinish={async (values: { word: string; category: string; description?: string }) => {
          setBusy(true);
          setError("");
          try {
            const response = await fetch("/api/shared-rules", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                word: values.word,
                category: values.category,
                description: values.description ?? "",
              }),
            });
            if (!response.ok) throw new Error(await readError(response));
            await refresh();
          } catch (reason) {
            setError(reason instanceof Error ? reason.message : "提交失败");
          } finally {
            setBusy(false);
          }
        }}
      >
        <Form.Item name="word" label="违规词" rules={[{ required: true, message: "请填写违规词" }]}>
          <Input placeholder="例如：全网最低" />
        </Form.Item>
        <Form.Item name="category" label="类别">
          <Select options={CATEGORIES.map((item) => ({ value: item, label: item }))} />
        </Form.Item>
        <Form.Item name="description" label="说明">
          <Input.TextArea rows={2} maxLength={200} placeholder="可选" />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={busy}>
          提交
        </Button>
      </Form>
      <Typography.Text strong>我的提交</Typography.Text>
      <Table
        size="small"
        rowKey="id"
        pagination={false}
        dataSource={mine}
        locale={{ emptyText: "还没有提交" }}
        columns={[
          { title: "词", dataIndex: "word" },
          { title: "状态", dataIndex: "status_label" },
          {
            title: "原因",
            dataIndex: "review_comment",
            render: (value: string, row: SharedRule) => (row.status === "rejected" ? value || "—" : "—"),
          },
        ]}
      />
      <Typography.Text strong>已通过（所有人可见）</Typography.Text>
      <Table
        size="small"
        rowKey="id"
        pagination={false}
        dataSource={approved}
        locale={{ emptyText: "还没有已通过的共享违规词" }}
        columns={[
          { title: "词", dataIndex: "word" },
          { title: "类别", dataIndex: "category" },
          { title: "说明", dataIndex: "description", render: (value: string) => value || "—" },
        ]}
      />
    </div>
  );
}
