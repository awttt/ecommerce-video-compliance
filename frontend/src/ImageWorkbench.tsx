import {
  DownloadOutlined,
  PictureOutlined,
  PlusOutlined,
} from "@ant-design/icons";
import { Alert, Button, Checkbox, Select } from "antd";
import { useEffect, useRef, useState } from "react";

import {
  type ImageBatch,
  type ImageStatus,
  createImageBatch,
  deleteImageBatch,
  downloadImageFile,
  getImageStatus,
  listImageBatches,
} from "./api";
import {
  EXAMPLE_PROMPTS,
  SIZE_OPTIONS,
  type ImageOrientation,
  type ImageSizeId,
  buildImagePayload,
  filterBatches,
  isGenerateShortcut,
} from "./imageWorkbench";

const FILTERS: { id: ImageOrientation; label: string }[] = [
  { id: "all", label: "全部" },
  { id: "square", label: "方形" },
  { id: "landscape", label: "横向" },
  { id: "portrait", label: "纵向" },
];

export function ImageWorkbench() {
  const [status, setStatus] = useState<ImageStatus | null>(null);
  const [statusError, setStatusError] = useState("");
  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState("");
  const [size, setSize] = useState<ImageSizeId>("auto");
  const [count, setCount] = useState(1);
  const [sizeOpen, setSizeOpen] = useState(false);
  const [references, setReferences] = useState<File[]>([]);
  const [batches, setBatches] = useState<ImageBatch[]>([]);
  const [query, setQuery] = useState("");
  const [orientation, setOrientation] = useState<ImageOrientation>("all");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const lock = useRef(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let stopped = false;
    getImageStatus()
      .then((next) => {
        if (stopped) return;
        setStatus(next);
        setModel(next.models[0]?.id ?? "");
      })
      .catch((reason: Error) => {
        if (!stopped) setStatusError(reason.message);
      });
    listImageBatches()
      .then((rows) => {
        if (!stopped) setBatches(rows);
      })
      .catch(() => undefined);
    return () => {
      stopped = true;
    };
  }, []);

  const enabled = Boolean(status?.enabled);
  const visible = filterBatches(batches, query, orientation);
  const sizeLabel = SIZE_OPTIONS.find((item) => item.id === size)?.label ?? "自动";

  async function submit() {
    if (lock.current || pending || !enabled || !prompt.trim()) return;
    lock.current = true;
    setPending(true);
    setError("");
    try {
      const payload = buildImagePayload({ prompt, model, size, count });
      const form = new FormData();
      form.append("prompt", payload.prompt);
      form.append("model", payload.model);
      form.append("size", payload.size);
      form.append("count", String(payload.count));
      form.append("quality", payload.quality);
      form.append("async_generation", "false");
      if (status?.reference_local) {
        references.forEach((file) => form.append("references", file));
      }
      const created = await createImageBatch(form);
      setBatches((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setReferences([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生成失败");
    } finally {
      lock.current = false;
      setPending(false);
    }
  }

  return (
    <div className="page image-page">
      <header className="image-heading">
        <h1>生图</h1>
        <p>把文字提示变成图片。结果保存在本机，只有当前登录账号可以查看和下载。</p>
      </header>

      {status?.preview ? (
        <Alert
          type="warning"
          showIcon
          className="image-alert"
          message="预览模拟，不是真实上游生图"
          description={status.reason}
        />
      ) : null}
      {status && !status.enabled ? (
        <Alert type="info" showIcon className="image-alert" message="生图已禁用" description={status.reason} />
      ) : null}
      {statusError ? <Alert type="error" showIcon className="image-alert" message={statusError} /> : null}

      <section className="image-card" aria-label="开始生图">
        <h2>开始生图</h2>
        <textarea
          data-testid="image-prompt"
          className="image-prompt"
          rows={4}
          maxLength={2000}
          placeholder="描述你想生成的图片"
          value={prompt}
          disabled={pending}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if (isGenerateShortcut(event)) {
              event.preventDefault();
              void submit();
            }
          }}
        />
        <div className="image-chips">
          {EXAMPLE_PROMPTS.map((example) => (
            <button
              key={example}
              type="button"
              className="image-chip"
              disabled={pending}
              onClick={() => setPrompt(example)}
            >
              {example}
            </button>
          ))}
        </div>

        <div className="image-toolbar">
          <label className="image-field">
            <span>模型</span>
            <Select
              data-testid="image-model"
              className="image-model"
              placeholder="没有可用模型"
              disabled={!enabled || pending || (status?.models.length ?? 0) === 0}
              value={model || undefined}
              options={(status?.models ?? []).map((item) => ({ value: item.id, label: item.label }))}
              onChange={setModel}
            />
          </label>
          <Button
            icon={<PictureOutlined />}
            disabled={!status?.reference_local || pending}
            onClick={() => fileRef.current?.click()}
          >
            添加参考图{references.length ? `（${references.length}）` : ""}
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            multiple
            hidden
            onChange={(event) => {
              const picked = Array.from(event.target.files ?? []).slice(0, 4);
              setReferences(picked);
              event.target.value = "";
            }}
          />
          <div className="image-size">
            <Button data-testid="image-size" disabled={pending} onClick={() => setSizeOpen((open) => !open)}>
              {sizeLabel}
            </Button>
            {sizeOpen ? (
              <div className="image-size-panel" role="dialog" aria-label="尺寸">
                <div className="image-size-label">尺寸</div>
                <div className="image-size-row">
                  {SIZE_OPTIONS.map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      className={option.id === size ? "is-active" : ""}
                      onClick={() => {
                        setSize(option.id);
                        setSizeOpen(false);
                      }}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
                <div className="image-size-cards">
                  {SIZE_OPTIONS.filter((option) => option.orientation !== "auto").map((option) => (
                    <button
                      key={option.id}
                      type="button"
                      className={option.id === size ? "is-active" : ""}
                      onClick={() => {
                        setSize(option.id);
                        setSizeOpen(false);
                      }}
                    >
                      <i className={`image-ratio ${option.orientation}`} />
                      <strong>{option.orientation === "square" ? "方形" : option.orientation === "landscape" ? "横向" : "纵向"}</strong>
                      <span>{option.label}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
          <Button disabled title="参考页上画质为自动且不能改。公开文档没有确认这个参数，不会发给上游。">
            自动
          </Button>
          <Select
            data-testid="image-count"
            className="image-count"
            disabled={pending}
            value={count}
            options={[1, 2, 3, 4].map((value) => ({ value, label: `×${value}` }))}
            onChange={setCount}
          />
          <Button
            type="primary"
            className="image-generate"
            data-testid="image-generate"
            loading={pending}
            disabled={!enabled || pending || !prompt.trim()}
            onClick={() => void submit()}
          >
            生成
          </Button>
        </div>
        <div className="image-meta">
          <span className="image-group" data-testid="image-group">
            专用分组：{status?.group_label ?? "未配置"}
          </span>
          <Checkbox disabled checked={false}>
            异步生成（避免网关超时）
          </Checkbox>
          <span className="image-shortcut">Ctrl/⌘ + Enter</span>
        </div>
        <p className="image-note">{status?.notice}</p>
        {error ? <Alert type="error" showIcon message={error} /> : null}
      </section>

      <section className="image-results" aria-label="生成结果">
        <div className="image-results-head">
          <h2>生成结果</h2>
          <div className="image-results-tools">
            <input
              data-testid="image-search"
              value={query}
              placeholder="搜索提示词或模型"
              onChange={(event) => setQuery(event.target.value)}
            />
            <div className="image-filters" role="tablist">
              {FILTERS.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={orientation === item.id ? "is-active" : ""}
                  onClick={() => setOrientation(item.id)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>
        </div>
        <p className="image-note">结果存在本机，只给当前登录账号下载。参考页的一小时过期和浏览器本地保存没有照搬。</p>
        {pending ? <div className="image-pending">正在生成本地预览…</div> : null}
        {visible.length === 0 && !pending ? (
          <div className="image-empty" data-testid="image-empty">
            <PictureOutlined />
            <strong>{batches.length === 0 ? "还没有历史" : "没有符合筛选的结果"}</strong>
            <span>{batches.length === 0 ? "生成的图片会显示在这里" : "换一个搜索词或方向"}</span>
          </div>
        ) : (
          <div className="image-grid">
            {visible.map((batch) => (
              <article key={batch.id} className="image-result" data-testid="image-result">
                {batch.files[0] ? (
                  <img src={batch.files[0].url} alt={batch.prompt} />
                ) : (
                  <div className="image-result-missing">没有图片</div>
                )}
                {batch.preview ? <em className="image-badge">预览模拟</em> : null}
                <p>{batch.prompt}</p>
                <span>
                  {batch.model_label} · {batch.size_label} · ×{batch.count}
                  {batch.upstream_sent ? "" : " · 未发送上游"}
                </span>
                {batch.references.length ? <span>参考图 {batch.references.length} 张只存在本机</span> : null}
                <div className="image-result-actions">
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    disabled={batch.files.length === 0}
                    onClick={() => void downloadImageFile(batch.id, 0).catch((reason: Error) => setError(reason.message))}
                  >
                    下载
                  </Button>
                  <Button
                    size="small"
                    icon={<PlusOutlined rotate={45} />}
                    onClick={() => {
                      void deleteImageBatch(batch.id)
                        .then(() => setBatches((current) => current.filter((item) => item.id !== batch.id)))
                        .catch((reason: Error) => setError(reason.message));
                    }}
                  >
                    删除
                  </Button>
                </div>
                {batch.files.length > 1 ? (
                  <div className="image-thumbs">
                    {batch.files.slice(1).map((file) => (
                      <button key={file.index} type="button" onClick={() => void downloadImageFile(batch.id, file.index)}>
                        <img src={file.url} alt="" />
                      </button>
                    ))}
                  </div>
                ) : null}
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
