/** 生图工作台的本地参数。这些选项来自已看到的参考页，不代表上游契约。 */

export const EXAMPLE_PROMPTS = [
  "日出时分的平静湖面，薄雾，摄影写实",
  "图书馆里看书的温馨机器人，暖光，插画",
  "极简山景海报，粗体形状，扁平设计",
  "夜晚的未来城市街道，霓虹倒影",
];

export const SIZE_OPTIONS = [
  { id: "auto", label: "自动", orientation: "auto" },
  { id: "1024x1024", label: "1024×1024", orientation: "square" },
  { id: "1536x1024", label: "1536×1024", orientation: "landscape" },
  { id: "1024x1536", label: "1024×1536", orientation: "portrait" },
] as const;

export type ImageSizeId = (typeof SIZE_OPTIONS)[number]["id"];
export type ImageOrientation = "all" | "auto" | "square" | "landscape" | "portrait";

export interface ImageBatch {
  id: string;
  prompt: string;
  model: string;
  model_label: string;
  size: string;
  size_label: string;
  orientation: string;
  count: number;
  status: string;
  message: string;
  preview: boolean;
  upstream_sent: boolean;
}

const SIZE_IDS = new Set(SIZE_OPTIONS.map((item) => item.id));

export function orientationOf(size: string): string {
  return SIZE_OPTIONS.find((item) => item.id === size)?.orientation ?? "auto";
}

export function filterBatches<T extends ImageBatch>(batches: T[], query: string, orientation: ImageOrientation): T[] {
  const needle = query.trim().toLowerCase();
  return batches.filter((batch) => {
    if (orientation !== "all" && batch.orientation !== orientation) return false;
    if (!needle) return true;
    return batch.prompt.toLowerCase().includes(needle) || batch.model.toLowerCase().includes(needle) || batch.model_label.toLowerCase().includes(needle);
  });
}

export function isGenerateShortcut(event: { key: string; ctrlKey: boolean; metaKey: boolean }): boolean {
  return (event.ctrlKey || event.metaKey) && event.key === "Enter";
}

/** 发给本机接口的字段。不包含分组、网关或密钥。 */
export function buildImagePayload(input: {
  prompt: string;
  model: string;
  size: string;
  count: number;
}): { prompt: string; model: string; size: string; count: number; quality: "auto"; async_generation: false } {
  const size = SIZE_IDS.has(input.size as ImageSizeId) ? input.size : "auto";
  const count = Math.min(4, Math.max(1, Math.round(input.count)));
  return {
    prompt: input.prompt.trim(),
    model: input.model,
    size,
    count,
    quality: "auto",
    async_generation: false,
  };
}
