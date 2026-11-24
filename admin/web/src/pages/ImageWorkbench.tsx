import { PictureOutlined, PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Button, Empty, Input, Modal, Popconfirm, Popover, Select, Switch } from "antd";
import { useEffect, useMemo, useState } from "react";
import { ApiError } from "../api";
import { deleteImageJob, downloadImageFile, generateImage, getImageStatus, listImageJobs } from "../imageApi";
import {
  estimateCostYuan,
  generationBlockReason,
  imageDownloadName,
  modelOptionLabel,
  modelSelectable,
  sizeEnabledFor,
  sizeLockedToSingle,
  sizeOptionLabel,
  type ImageJob,
  type ImageSizeOption,
} from "../imageWorkbench";
import { RefreshButton } from "../ui";

const PROMPT_CHIPS = [
  "黎明时分的宁静湖面，薄雾，写实",
  "温暖光线下在图书馆阅读的机器人，插画",
  "极简山峰海报，大胆图形，扁平设计",
  "夜晚的未来城市街道，霓虹倒影",
];

const ORIENTATION_OPTIONS = [
  { value: "", label: "全部方向" },
  { value: "square", label: "方图" },
  { value: "landscape", label: "横图" },
  { value: "portrait", label: "竖图" },
];

function sizeShape(value: string): { label: string; mark: string } | null {
  const match = /^(\d+)[x:](\d+)$/.exec(value);
  if (!match) return null;
  const width = Number(match[1]);
  const height = Number(match[2]);
  if (width === height) return { label: "方图", mark: "□" };
  if (width > height) return { label: "横图", mark: "▭" };
  return { label: "竖图", mark: "▯" };
}

export function ImageWorkbench() {
  const { message } = AntApp.useApp();
  const client = useQueryClient();
  const statusQuery = useQuery({
    queryKey: ["image-workbench-status"],
    queryFn: getImageStatus,
  });
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [size, setSize] = useState("auto");
  const [quality, setQuality] = useState("auto");
  const [count, setCount] = useState(1);
  const [references, setReferences] = useState<{ name: string; url: string }[]>([]);
  const [query, setQuery] = useState("");
  const [orientation, setOrientation] = useState("");
  const [preview, setPreview] = useState<ImageJob | null>(null);
  const [previewIndex, setPreviewIndex] = useState(0);
  const [savingUrl, setSavingUrl] = useState("");

  const jobs = useQuery({
    queryKey: ["image-workbench-jobs", query, orientation],
    queryFn: () => listImageJobs(query, orientation),
    enabled: statusQuery.isSuccess,
  });

  const status = statusQuery.data;
  const blocked = useMemo(() => {
    if (!status) return "请先点刷新读取生图配置";
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
  useEffect(() => {
    if (!status?.group_ready || model) return;
    const first = status.models.find((item) => item.selectable);
    if (first) setModel(first.id);
  }, [model, status]);

  const counts = status?.counts ?? [1, 2, 3, 4];
  const sizes = status?.sizes ?? [];
  const qualities = status?.qualities ?? [];
  const shapedSizes = sizes.filter((item) => sizeShape(item.value));

  const generate = useMutation({
    mutationFn: () =>
      generateImage({
        prompt: prompt.trim(),
        model,
        n: count,
        size,
        quality,
        references: references.map((item) => item.url),
        confirm: true,
      }),
    onSuccess: async (job) => {
      setPreview(job);
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
    const next = await statusQuery.refetch();
    if (next.isSuccess) await jobs.refetch();
  }

  function submit() {
    if (!canGenerate || generate.isPending) return;
    generate.mutate();
  }

  function openPreview(job: ImageJob, index: number) {
    setPreview(job);
    setPreviewIndex(index);
  }

  async function saveImage(url: string, promptText: string, index: number) {
    if (!url || savingUrl) return;
    setSavingUrl(url);
    try {
      await downloadImageFile(url, imageDownloadName(promptText, url, index + 1));
    } catch (error) {
      message.error(error instanceof ApiError ? error.message : "下载失败");
    } finally {
      setSavingUrl("");
    }
  }

  const statusError = statusQuery.isError
    ? statusQuery.error instanceof ApiError
      ? statusQuery.error.message
      : "读取生图状态失败"
    : "";
  const disabledReason = useMemo(() => {
    if (statusQuery.isLoading) return "";
    if (statusQuery.isError) return statusError;
    if (blocked) return blocked;
    if (prompt.trim().length === 0) return "请先填写画面描述";
    return "";
  }, [blocked, prompt, statusError, statusQuery.isError, statusQuery.isLoading]);
  const history = jobs.data ?? [];
  const sizeLabel = sizes.find((item) => item.value === size)?.label || (size === "auto" ? "自动" : size);
  const qualityLabel = qualities.find((item) => item.value === quality)?.label || (quality === "auto" ? "自动" : quality);

  return (
    <div className="image-workbench">
      <div className="ig-heading">
        <div>
          <h1>图片生成</h1>
        </div>
        <RefreshButton onClick={() => void refreshAll()} loading={statusQuery.isFetching || jobs.isFetching} />
      </div>

      {statusError ? <Alert type="error" showIcon message={statusError} /> : null}
      {statusQuery.isLoading ? <Alert type="info" showIcon message="正在读取模型和价格，读完后就可以选择。" /> : null}
      {status?.mock ? (
        <Alert type="warning" showIcon message="当前为 MOCK 预览" description="生成的是带 MOCK 标记的占位图，不会请求真实生图接口，也不会扣费。" />
      ) : null}
      {status && !status.group_ready ? (
        <Alert type="error" showIcon message={status.group_reason || "当前账号未开通生图专用分组"} />
      ) : null}

      <h2 className="ig-start">开始生成图片</h2>

      <form
        className="ig-composer"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <label className="sr-only" htmlFor="image-prompt">画面描述</label>
        <textarea
          id="image-prompt"
          className="ig-prompt"
          rows={4}
          maxLength={2000}
          placeholder="描述你想创建的图片"
          value={prompt}
          disabled={generate.isPending}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              submit();
            }
          }}
        />
        {prompt.trim() === "" ? (
          <div className="ig-chips">
            {PROMPT_CHIPS.map((chip) => (
              <button key={chip} type="button" className="ig-chip" disabled={generate.isPending} onClick={() => setPrompt(chip)}>
                {chip}
              </button>
            ))}
          </div>
        ) : null}

        <div className="ig-toolbar">
          <Select
            className="ig-token"
            disabled
            value={status?.key_name || undefined}
            placeholder="密钥：刷新后显示"
            options={status?.key_name ? [{ value: status.key_name, label: `密钥：${status.key_name}` }] : []}
          />
          <Select
            className="ig-model"
            placeholder="模型：选择模型"
            value={model || undefined}
            disabled={!status?.group_ready || generate.isPending}
            onChange={onModelChange}
            options={(status?.models ?? []).map((item) => ({
              value: item.id,
              label: `模型：${modelOptionLabel(item)}`,
              disabled: !modelSelectable(item),
            }))}
          />
          <Popover
            trigger="click"
            placement="bottomLeft"
            title="添加参考图"
            content={
              <div className="ig-pop">
                <p className="ig-pop-note">{status?.references.reason || "最多 4 张，仅 PNG、JPEG、WebP，单张不超过 4MB。"}</p>
                <input
                  className="ig-file"
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  multiple
                  disabled={!status?.references.enabled || generate.isPending || references.length >= 4}
                  onChange={(event) => {
                    void addReferences(event.target.files, references, setReferences);
                    event.target.value = "";
                  }}
                />
                <ul className="ig-refs">
                  {references.map((item) => (
                    <li key={item.url}>
                      <img src={item.url} alt={item.name} />
                      <button type="button" onClick={() => setReferences((current) => current.filter((entry) => entry.url !== item.url))}>
                        移除
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            }
          >
            <Button icon={<PlusOutlined />} disabled={!status?.references.enabled || generate.isPending}>
              {references.length > 0 ? `参考图 ${references.length}` : "添加参考图"}
            </Button>
          </Popover>
          <SizePicker
            label={sizeLabel}
            sizes={sizes}
            shaped={shapedSizes}
            value={size}
            model={model}
            disabled={!status?.group_ready || generate.isPending}
            onChange={onSizeChange}
          />
          <QualityPicker
            label={qualityLabel}
            options={qualities}
            value={quality}
            locked={locked}
            disabled={!status?.group_ready || generate.isPending}
            onChange={setQuality}
          />
          <Select
            className="ig-count"
            value={count}
            disabled={!status?.group_ready || generate.isPending || locked}
            onChange={(value) => setCount(Number(value))}
            options={counts.map((value) => ({ value, label: `×${value}`, disabled: locked && value !== 1 }))}
          />
          <Button className="ig-generate" type="primary" htmlType="submit" loading={generate.isPending} disabled={!canGenerate}>
            生成
          </Button>
        </div>

        {disabledReason && !generate.isPending ? <p className="ig-wait">{disabledReason}</p> : null}

        {status ? (
          <p className="ig-group">
            <span>模型分组</span>：{status.group_name || "跟随用户分组"}
            {status.pricing ? ` · 预估 ¥${estimateCostYuan(status.pricing, count)}（${count} 张 × ${status.pricing.display}）` : ""}
          </p>
        ) : null}
        {status && !status.mock ? (
          <p className="ig-group">真实生图：点击生成会请求网关并按实账扣费。</p>
        ) : null}

        <div className="ig-async">
          <Switch size="small" checked={false} disabled />
          <label>{status?.async_generation.reason || "异步生成（避免网关超时）"}</label>
          <span className="ig-shortcut">Ctrl/⌘ + Enter</span>
        </div>
        {generate.error ? (
          <Alert
            type="error"
            showIcon
            message={generate.error instanceof ApiError ? generate.error.message : "生成失败"}
            description={
              generate.error instanceof ApiError && generate.error.status >= 500
                ? "请求已经发到生图服务。失败不能确认是否扣费，再次提交前请核对账单。"
                : "这次没有发到生图服务，不会扣费。"
            }
          />
        ) : null}
        {generate.isPending ? <p className="ig-wait">停止等待只会结束这次等待。上游仍可能继续处理并扣费。</p> : null}
        {locked ? <p className="ig-wait">该尺寸为上游已验证尺寸，仅 1 张、默认质量。</p> : null}
      </form>

      <section className="ig-history" aria-label="作品">
        <div className="ig-history-bar">
          <h2>生成结果</h2>
          <Input
            allowClear
            className="ig-search"
            placeholder="按描述或模型搜索"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <Select
            className="ig-orientation"
            value={orientation}
            onChange={setOrientation}
            options={ORIENTATION_OPTIONS}
          />
        </div>
        <p className="ig-note">点刷新后从服务端读取历史。未生成的记录不会出现在这里。</p>
        {history.length === 0 && !generate.isPending ? (
          <Empty className="ig-empty" description="还没有历史记录。生成的图片会显示在这里。" />
        ) : null}
        {history.length > 0 ? (
          <Popconfirm title="清空当前列表中的生成记录？" onConfirm={() => void clearHistory(history, client, preview, setPreview)}>
            <Button className="ig-clear" size="small">清空历史</Button>
          </Popconfirm>
        ) : null}
        <ul className="ig-grid">
          {generate.isPending
            ? Array.from({ length: count }, (_, index) => (
              <li key={`pending-${index}`} className="ig-tile ig-tile-pending">
                <div className="ig-tile-image ig-tile-loading" />
                <p>正在生成图片…</p>
              </li>
            ))
            : null}
          {history.flatMap((job) =>
            (job.images.length > 0 ? job.images : [{ url: "", mock: job.mock }]).map((image, index) => (
              <li key={`${job.id}-${index}`} className="ig-tile">
                <button type="button" className="ig-tile-open" onClick={() => openPreview(job, index)}>
                  {image.url ? (
                    <img src={image.url} alt={job.prompt} />
                  ) : (
                    <span className="ig-missing"><PictureOutlined /> 图片不可用</span>
                  )}
                </button>
                <div className="ig-tile-meta">
                  <p className="ig-tile-title" title={job.prompt}>{job.prompt}</p>
                  <p className="ig-tile-sub">{job.model} · 请求尺寸：{job.size || "默认尺寸"}</p>
                  <div className="ig-tile-actions">
                    <Button
                      type="link"
                      size="small"
                      disabled={!image.url}
                      loading={savingUrl === image.url}
                      onClick={() => void saveImage(image.url, job.prompt, index)}
                    >
                      下载
                    </Button>
                    <Button type="link" size="small" onClick={() => reuseJob(job, setPrompt, onModelChange, onSizeChange, setQuality, setCount)}>
                      复用
                    </Button>
                    <Button
                      type="link"
                      size="small"
                      danger
                      onClick={() => {
                        void deleteImageJob(job.id).then(() => {
                          if (preview?.id === job.id) setPreview(null);
                          return client.invalidateQueries({ queryKey: ["image-workbench-jobs"] });
                        });
                      }}
                    >
                      删除
                    </Button>
                  </div>
                </div>
              </li>
            )),
          )}
        </ul>
      </section>

      <Modal
        open={preview != null}
        title="图片预览"
        footer={null}
        onCancel={() => setPreview(null)}
        width={720}
      >
        {preview ? (
          <div className="ig-preview">
            {preview.images[previewIndex]?.url ? (
              <img src={preview.images[previewIndex].url} alt={preview.prompt} />
            ) : (
              <p>图片不可用</p>
            )}
            <p>{preview.prompt}</p>
            <div className="ig-preview-actions">
              <Button
                type="primary"
                disabled={!preview.images[previewIndex]?.url}
                loading={savingUrl === preview.images[previewIndex]?.url}
                onClick={() => void saveImage(preview.images[previewIndex]?.url || "", preview.prompt, previewIndex)}
              >
                下载到本地
              </Button>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}

function addReferences(
  files: FileList | null,
  current: { name: string; url: string }[],
  setReferences: (value: { name: string; url: string }[]) => void,
) {
  if (!files) return;
  const room = 4 - current.length;
  const next = [...current];
  const readers = Array.from(files).slice(0, room).map(
    (file) =>
      new Promise<{ name: string; url: string } | null>((resolve) => {
        if (!["image/png", "image/jpeg", "image/webp"].includes(file.type) || file.size > 4 * 1024 * 1024) {
          resolve(null);
          return;
        }
        const reader = new FileReader();
        reader.onload = () => resolve({ name: file.name, url: String(reader.result || "") });
        reader.onerror = () => resolve(null);
        reader.readAsDataURL(file);
      }),
  );
  void Promise.all(readers).then((items) => {
    for (const item of items) {
      if (item?.url) next.push(item);
    }
    setReferences(next.slice(0, 4));
  });
}

function SizePicker(props: {
  label: string;
  sizes: ImageSizeOption[];
  shaped: ImageSizeOption[];
  value: string;
  model: string;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  function choose(value: string) {
    props.onChange(value);
    setOpen(false);
  }
  return (
    <Popover
      trigger="click"
      placement="bottomLeft"
      title="尺寸"
      open={open}
      onOpenChange={setOpen}
      content={
        <div className="ig-pop">
          <div className="ig-pop-chips">
            {props.sizes.map((item) => (
              <button
                key={item.value}
                type="button"
                className={item.value === props.value ? "ig-pop-chip is-on" : "ig-pop-chip"}
                disabled={!sizeEnabledFor(item, props.model)}
                onClick={() => choose(item.value)}
              >
                {sizeOptionLabel(item)}
              </button>
            ))}
          </div>
          {props.shaped.length > 0 ? (
            <div className="ig-shapes">
              {props.shaped.map((item) => {
                const shape = sizeShape(item.value);
                return (
                  <button
                    key={item.value}
                    type="button"
                    className={item.value === props.value ? "ig-shape is-on" : "ig-shape"}
                    disabled={!sizeEnabledFor(item, props.model)}
                    onClick={() => choose(item.value)}
                  >
                    <span aria-hidden="true">{shape?.mark}</span>
                    <span>{shape?.label}</span>
                    <span>{item.value}</span>
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      }
    >
      <Button disabled={props.disabled}>{props.label}</Button>
    </Popover>
  );
}

function QualityPicker(props: {
  label: string;
  options: { value: string; label: string; enabled: boolean; note?: string }[];
  value: string;
  locked: boolean;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover
      trigger="click"
      placement="bottomLeft"
      title="质量"
      open={open}
      onOpenChange={setOpen}
      content={
        <div className="ig-pop-chips">
          {props.options.map((item) => (
            <button
              key={item.value}
              type="button"
              className={item.value === props.value ? "ig-pop-chip is-on" : "ig-pop-chip"}
              disabled={!item.enabled || (props.locked && item.value !== "auto")}
              onClick={() => {
                props.onChange(item.value);
                setOpen(false);
              }}
            >
              {item.note && !item.enabled ? `${item.label} · 禁用` : item.label}
            </button>
          ))}
        </div>
      }
    >
      <Button disabled={props.disabled}>{props.label}</Button>
    </Popover>
  );
}

function reuseJob(
  job: ImageJob,
  setPrompt: (value: string) => void,
  onModelChange: (value: string) => void,
  onSizeChange: (value: string) => void,
  setQuality: (value: string) => void,
  setCount: (value: number) => void,
) {
  setPrompt(job.prompt);
  if (job.model) onModelChange(job.model);
  if (job.size) onSizeChange(job.size);
  if (job.quality) setQuality(job.quality);
  if (job.n) setCount(job.n);
}

async function clearHistory(
  jobs: ImageJob[],
  client: ReturnType<typeof useQueryClient>,
  preview: ImageJob | null,
  setPreview: (job: ImageJob | null) => void,
) {
  await Promise.all(jobs.map((job) => deleteImageJob(job.id)));
  if (preview && jobs.some((job) => job.id === preview.id)) setPreview(null);
  await client.invalidateQueries({ queryKey: ["image-workbench-jobs"] });
}
