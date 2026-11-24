import { HistoryOutlined, PictureOutlined, SendOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useMemo, useState } from "react";
import { ApiError } from "../api";
import { deleteImageJob, generateImage, getImageStatus, listImageJobs } from "../imageApi";
import {
  estimateCostYuan,
  generationBlockReason,
  modelOptionLabel,
  modelSelectable,
  sizeEnabledFor,
  sizeLockedToSingle,
  sizeOptionLabel,
  type ImageJob,
} from "../imageWorkbench";
import { PageHead, PageLoading, RefreshButton } from "../ui";

const { Paragraph, Text } = Typography;
const { TextArea } = Input;

const PROMPT_CHIPS = ["红色礼盒 白底 商品主图", "清新蓝调 数码配件场景图", "国潮插画 中秋礼盒海报", "极简白底 厨房用品细节图"];

const ORIENTATION_OPTIONS = [
  { value: "", label: "全部" },
  { value: "square", label: "方图" },
  { value: "landscape", label: "横图" },
  { value: "portrait", label: "竖图" },
  { value: "auto", label: "未指定" },
];

function jobTime(created: string): string {
  const value = created.replace("T", " ").replace("Z", "");
  return value.length >= 16 ? value.slice(0, 16) : value;
}

export function ImageWorkbench() {
  const client = useQueryClient();
  const statusQuery = useQuery({ queryKey: ["image-workbench-status"], queryFn: getImageStatus });
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [size, setSize] = useState("auto");
  const [quality, setQuality] = useState("auto");
  const [count, setCount] = useState(1);
  const [query, setQuery] = useState("");
  const [orientation, setOrientation] = useState("");
  const [active, setActive] = useState<ImageJob | null>(null);

  const jobs = useQuery({
    queryKey: ["image-workbench-jobs", query, orientation],
    queryFn: () => listImageJobs(query, orientation),
    enabled: statusQuery.isSuccess,
  });

  const status = statusQuery.data;
  const blocked = useMemo(() => {
    if (!status) return "正在读取生图状态";
    return generationBlockReason({
      groupReady: status.group_ready,
      model,
      models: status.models,
      size,
      sizes: status.sizes,
      quality,
      qualities: status.qualities,
      n: count,
    });
  }, [count, model, quality, size, status]);
  const canGenerate = Boolean(status) && prompt.trim().length > 0 && blocked === "";
  const selectedSize = status?.sizes.find((item) => item.value === size);
  const locked = sizeLockedToSingle(selectedSize, model);

  const generate = useMutation({
    mutationFn: () =>
      generateImage({
        prompt: prompt.trim(),
        model,
        n: count,
        size,
        quality,
        confirm: true,
      }),
    onSuccess: async (job) => {
      setActive(job);
      await client.invalidateQueries({ queryKey: ["image-workbench-jobs"] });
    },
  });

  function onModelChange(next: string) {
    setModel(next);
    setSize((current) => {
      if (!status) return current;
      const option = status.sizes.find((item) => item.value === current);
      if (!option || !sizeEnabledFor(option, next)) return "auto";
      return current;
    });
  }

  function onSizeChange(next: string) {
    setSize(next);
    const option = status?.sizes.find((item) => item.value === next);
    if (sizeLockedToSingle(option, model)) {
      setQuality("auto");
      setCount(1);
    }
  }

  async function refreshAll() {
    await statusQuery.refetch();
    await jobs.refetch();
  }

  if (statusQuery.isLoading) return <PageLoading tip="正在读取生图状态…" />;
  if (!status) {
    const message = statusQuery.error instanceof ApiError ? statusQuery.error.message : "读取生图状态失败";
    return <Alert type="error" showIcon message={message} />;
  }

  const submitDisabled = !canGenerate || generate.isPending;
  const result = active;

  return (
    <div className="image-workbench">
      <PageHead
        title="生图工作台"
        description="同步生成电商配图。价格和可用尺寸由服务端下发，预估费用不是上游成本。"
        extra={<RefreshButton onClick={() => void refreshAll()} loading={statusQuery.isFetching || jobs.isFetching} />}
      />

      {status.mock ? (
        <Alert
          className="wb-alert"
          type="warning"
          showIcon
          message="当前为 MOCK 预览"
          description="生成的是带 MOCK 标记的占位图，不会请求真实生图接口，也不会扣费。真实出图默认关闭。"
        />
      ) : (
        <Alert
          className="wb-alert"
          type="info"
          showIcon
          message="真实生图开关已打开"
          description="只有已核验组合会同步请求网关。扣费以网关实账为准。"
        />
      )}
      {!status.group_ready ? (
        <Alert className="wb-alert" type="error" showIcon message={status.group_reason || "当前账号未开通生图专用分组"} />
      ) : null}

      <div className="image-workbench-grid">
        <Card className="wb-card" title={<span><PictureOutlined /> 生成</span>}>
          <div className="wb-field">
            <label className="wb-label">画面描述</label>
            <TextArea
              rows={4}
              maxLength={2000}
              placeholder="描述想要的电商配图，例如商品、场景、风格…"
              value={prompt}
              disabled={!status.group_ready}
              onChange={(event) => setPrompt(event.target.value)}
            />
            <div className="wb-chips">
              {PROMPT_CHIPS.map((chip) => (
                <Button key={chip} size="small" disabled={!status.group_ready} onClick={() => setPrompt(chip)}>
                  {chip}
                </Button>
              ))}
            </div>
            <div className="wb-hint-row">
              <Text type="secondary">示例可以直接填入描述</Text>
              <Text type="secondary">{prompt.length}/2000</Text>
            </div>
          </div>

          <div className="wb-field">
            <label className="wb-label">模型</label>
            <Select
              style={{ width: "100%" }}
              placeholder="选择生图模型"
              value={model || undefined}
              disabled={!status.group_ready}
              onChange={onModelChange}
              options={status.models.map((item) => ({
                value: item.id,
                label: modelOptionLabel(item),
                disabled: !modelSelectable(item),
              }))}
            />
          </div>

          <div className="wb-field">
            <label className="wb-label">生图分组</label>
            <Input readOnly value={status.group_name} />
            <Paragraph className="wb-switch-hint" type="secondary">
              {status.group_ready ? "当前账号已开通该分组，只读，不能在这里改分组或密钥。" : "只读。当前账号没有这个分组时不能生成，也不会改用审核分组。"}
            </Paragraph>
          </div>

          <div className="wb-field">
            <label className="wb-label">尺寸</label>
            <Select
              style={{ width: "100%" }}
              value={size}
              disabled={!status.group_ready}
              onChange={onSizeChange}
              options={status.sizes.map((item) => ({
                value: item.value,
                label: sizeOptionLabel(item),
                disabled: !sizeEnabledFor(item, model),
              }))}
            />
            {locked ? (
              <Paragraph className="wb-switch-hint" type="secondary">
                该尺寸为上游已验证尺寸，仅 n=1、默认质量。本站网关仍待验证。
              </Paragraph>
            ) : null}
          </div>

          <div className="wb-field">
            <label className="wb-label">质量</label>
            <Segmented
              block
              value={quality}
              disabled={!status.group_ready}
              onChange={(value) => setQuality(String(value))}
              options={status.qualities.map((item) => ({
                value: item.value,
                label: item.note && !item.enabled ? `${item.label} · 禁用` : item.label,
                disabled: !item.enabled || (locked && item.value !== "auto"),
              }))}
            />
          </div>

          <div className="wb-field">
            <label className="wb-label">张数</label>
            <Segmented
              block
              value={count}
              disabled={!status.group_ready}
              onChange={(value) => setCount(Number(value))}
              options={status.counts.map((value) => ({
                value,
                label: String(value),
                disabled: locked && value !== 1,
              }))}
            />
            <Paragraph className="wb-switch-hint" type="secondary">
              预估费用 ¥{estimateCostYuan(status.pricing, count)}（{count} 张 × {status.pricing.display}）。{status.pricing.note}
            </Paragraph>
          </div>

          <div className="wb-switches">
            <div className="wb-switch-row">
              <div>
                <Text>参考图 / edits</Text>
                <Paragraph className="wb-switch-hint" type="secondary">{status.references.reason}</Paragraph>
              </div>
              <Switch checked={false} disabled />
            </div>
            <div className="wb-switch-row">
              <div>
                <Text>异步生成</Text>
                <Paragraph className="wb-switch-hint" type="secondary">{status.async_generation.reason}</Paragraph>
              </div>
              <Switch checked={false} disabled />
            </div>
          </div>

          <Tooltip title={submitDisabled && !generate.isPending ? blocked || "请先填写画面描述" : ""}>
            <Button
              type="primary"
              size="large"
              block
              className="wb-submit"
              icon={<SendOutlined />}
              loading={generate.isPending}
              disabled={submitDisabled}
              onClick={() => generate.mutate()}
            >
              生成图片
            </Button>
          </Tooltip>
          {generate.error ? (
            <Alert
              style={{ marginTop: 12 }}
              type="error"
              showIcon
              message={generate.error instanceof ApiError ? generate.error.message : "生成失败"}
            />
          ) : null}
        </Card>

        <Card className="wb-card" title="结果">
          {result ? (
            <>
              <Space wrap>
                {result.mock ? <Tag color="warning">MOCK</Tag> : <Tag color="success">网关结果</Tag>}
                <Tag>{result.model}</Tag>
                <Tag>{result.size}</Tag>
                <Tag>{result.n} 张</Tag>
                <Tag>预估 ¥{result.est_cost.toFixed(2)}</Tag>
              </Space>
              <Paragraph className="wb-switch-hint">{result.message}</Paragraph>
              <Paragraph>{result.prompt}</Paragraph>
              <div className="wb-thumbs">
                {result.images.map((image) => (
                  <a key={image.url} href={image.url} target="_blank" rel="noreferrer">
                    <img className="wb-thumb" src={image.url} alt={image.mock ? "MOCK 预览图" : "生成结果"} />
                  </a>
                ))}
              </div>
            </>
          ) : (
            <Empty description="生成后的图片会显示在这里" />
          )}
          <Paragraph className="wb-footnote" type="secondary">
            {status.capability_note}
          </Paragraph>
        </Card>
      </div>

      <Card
        className="wb-card"
        title={<span><HistoryOutlined /> 历史</span>}
      >
        <div className="wb-filter">
          <Input.Search
            allowClear
            placeholder="按描述或模型搜索"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <Segmented value={orientation} onChange={(value) => setOrientation(String(value))} options={ORIENTATION_OPTIONS} />
        </div>
        <Table<ImageJob>
          rowKey="id"
          size="middle"
          loading={jobs.isFetching}
          dataSource={jobs.data ?? []}
          pagination={false}
          locale={{ emptyText: "还没有生成记录" }}
          onRow={(record) => ({ onClick: () => setActive(record) })}
          columns={[
            { title: "时间", dataIndex: "created_at", render: (value: string) => jobTime(value) },
            { title: "模型", dataIndex: "model", ellipsis: true },
            { title: "尺寸", dataIndex: "size" },
            { title: "张数", dataIndex: "n", width: 72 },
            {
              title: "状态",
              dataIndex: "status",
              render: (_value: string, record) => (
                <Space>
                  <Tag>{record.status === "done" ? "完成" : record.status}</Tag>
                  {record.mock ? <Tag color="warning">MOCK</Tag> : null}
                </Space>
              ),
            },
            {
              title: "预估费用",
              dataIndex: "est_cost",
              render: (value: number) => `¥${value.toFixed(2)}`,
            },
            {
              title: "",
              width: 72,
              render: (_value: unknown, record) => (
                <Button
                  type="link"
                  danger
                  onClick={(event) => {
                    event.stopPropagation();
                    void deleteImageJob(record.id).then(() => {
                      if (active?.id === record.id) setActive(null);
                      return client.invalidateQueries({ queryKey: ["image-workbench-jobs"] });
                    });
                  }}
                >
                  删除
                </Button>
              ),
            },
          ]}
        />
      </Card>
    </div>
  );
}
