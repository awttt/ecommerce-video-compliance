/** 生图工作台的纯逻辑。能力、价格和可选状态都来自服务端元数据，这里不自填。 */

export interface ImageModelOption {
  id: string;
  label: string;
  ready: boolean;
  upstream_verified?: boolean;
  selectable?: boolean;
  note?: string;
}

export interface ImagePricing {
  currency: "CNY";
  unit: "张";
  price_per_image: number;
  display: string;
  note: string;
}

export interface ImageSizeOption {
  value: string;
  label: string;
  note?: string;
  verified: boolean;
  gateway_pending?: boolean;
  verified_for?: string[];
  enabled: boolean;
}

export interface ImageQualityOption {
  value: string;
  label: string;
  enabled: boolean;
  note?: string;
}

export interface ImageStatus {
  logged_in: boolean;
  group_ready: boolean;
  group_reason: string;
  group_name: string;
  key_name: string;
  key_ready: boolean;
  mock: boolean;
  live_enabled: boolean;
  models: ImageModelOption[];
  pricing: ImagePricing;
  sizes: ImageSizeOption[];
  qualities: ImageQualityOption[];
  counts: number[];
  capability_note?: string;
  endpoint: { method: string; url: string; editable: boolean };
  references: { enabled: boolean; reason: string };
  async_generation: { enabled: boolean; reason: string };
}

export interface ImageResultImage {
  url: string;
  mock: boolean;
}

export interface ImageJob {
  id: string;
  prompt: string;
  model: string;
  size: string;
  quality: string;
  n: number;
  status: string;
  mock: boolean;
  message: string;
  error: string;
  est_cost_cents: number;
  est_cost: number;
  orientation: string;
  images: ImageResultImage[];
  created_at: string;
}

export interface ImageGenerateBody {
  prompt: string;
  model: string;
  n: number;
  size: string;
  quality: string;
  references?: string[];
  confirm: true;
}

export function sizeOptionLabel(option: ImageSizeOption): string {
  const note = (option.note ?? "").trim();
  if (note) return `${option.label}（${note}）`;
  if (!option.verified) return `${option.label}（未验证）`;
  return option.label;
}

/** 上游已验证尺寸只对 verified_for 里的模型开放。其余只看服务端 enabled。 */
export function sizeEnabledFor(option: ImageSizeOption, model: string): boolean {
  if (!option.enabled) return false;
  const verifiedFor = option.verified_for ?? [];
  if (verifiedFor.length > 0) return verifiedFor.includes(model.trim());
  return true;
}

export function modelOptionLabel(model: ImageModelOption): string {
  if (model.note) return `${model.label}（${model.note}）`;
  if (model.upstream_verified) return `${model.label}（上游已验证 · 本站网关待验）`;
  return model.ready ? model.label : `${model.label}（未就绪）`;
}

/** 能不能选，只看服务端的 selectable，不在前端把未确认模型标成已就绪。 */
export function modelSelectable(model: ImageModelOption): boolean {
  return model.selectable === true;
}

export function estimateCostYuan(pricing: ImagePricing | null | undefined, n: number): string {
  const unit = pricing && Number.isFinite(pricing.price_per_image) ? Math.max(pricing.price_per_image, 0) : 0;
  const count = Number.isFinite(n) ? Math.max(Math.floor(n), 1) : 1;
  return (count * unit).toFixed(2);
}

export function imageOrientation(size: string): "square" | "landscape" | "portrait" | "auto" {
  if (!size || size === "auto" || !size.includes("x")) return "auto";
  const [width, height] = size.split("x").map((part) => Number.parseInt(part, 10));
  if (!Number.isFinite(width) || !Number.isFinite(height)) return "auto";
  if (width === height) return "square";
  return width > height ? "landscape" : "portrait";
}

export function generationBlockReason(input: {
  groupReady: boolean;
  model: string;
  models: ImageModelOption[];
  size: string;
  sizes: ImageSizeOption[];
  quality: string;
  qualities: ImageQualityOption[];
  n: number;
}): string {
  if (!input.groupReady) return "当前账号没有生图专用分组";
  const model = input.models.find((item) => item.id === input.model);
  if (!model || !modelSelectable(model)) return "请选择可用的生图模型";
  const size = input.sizes.find((item) => item.value === input.size);
  if (!size || !sizeEnabledFor(size, input.model)) return "该尺寸对当前模型不可用";
  const quality = input.qualities.find((item) => item.value === input.quality);
  if (!quality || !quality.enabled) return "该质量尚未实测";
  if ((size.verified_for ?? []).includes(input.model) && (input.n !== 1 || input.quality !== "auto")) {
    return "该尺寸仅限 1 张且默认质量";
  }
  if (!Number.isInteger(input.n) || input.n < 1 || input.n > 4) return "张数只支持 1 到 4";
  return "";
}

export function sizeLockedToSingle(option: ImageSizeOption | undefined, model: string): boolean {
  if (!option || !model.trim()) return false;
  return (option.verified_for ?? []).includes(model.trim());
}

export type ImageSaveResult = {
  ok?: boolean;
  cancelled?: boolean;
  message?: string;
  bytes?: number;
  filename?: string;
};

export type ImageSaveCall = (url: string, filename: string) => Promise<ImageSaveResult>;

export type DesktopSaveBridge = ImageSaveCall | "unavailable" | null;

/** 桌面窗口里才有 pywebview。没有保存函数时不能退回浏览器下载，否则会装成已经保存。 */
export function desktopImageSave(
  pywebview: { api?: { save_generated_image?: unknown } } | null | undefined,
): DesktopSaveBridge {
  if (!pywebview) return null;
  const save = pywebview.api?.save_generated_image;
  if (typeof save !== "function") return "unavailable";
  return (url, filename) => (save as ImageSaveCall)(url, filename);
}

export type ImageDownloadOutcome = "saved" | "cancelled" | "started";

/** 桌面端必须走保存桥。取消不算成功；浏览器只允许下载本机生成记录。 */
export async function runImageDownload(
  url: string,
  filename: string,
  bridge: DesktopSaveBridge,
  browserDownload: (path: string, filename: string) => Promise<void>,
): Promise<ImageDownloadOutcome> {
  if (bridge === "unavailable") throw new Error("桌面保存还没准备好，请再试一次");
  if (bridge) {
    let result: ImageSaveResult;
    try {
      result = await bridge(url, filename);
    } catch {
      throw new Error("下载失败");
    }
    const outcome = interpretImageSave(result);
    return outcome === "cancelled" ? "cancelled" : "saved";
  }
  const path = localImagePath(url);
  if (!path) throw new Error("只能保存本机已经生成的图片");
  await browserDownload(`${path}?download=1`, filename);
  return "started";
}

export function interpretImageSave(result: ImageSaveResult | null | undefined): "saved" | "cancelled" {
  if (result?.cancelled) return "cancelled";
  const bytes = result?.bytes;
  if (!result?.ok || typeof bytes !== "number" || !Number.isInteger(bytes) || bytes < 1) {
    throw new Error(safeSaveMessage(result?.message));
  }
  return "saved";
}

function safeSaveMessage(message: string | undefined): string {
  const text = message?.trim() || "";
  if (!text || /sk-|bearer|token|authorization/i.test(text)) return "下载失败";
  return text;
}

const LOCAL_IMAGE = /^\/v1\/image-workbench\/jobs\/[0-9a-f]{32}\/files\/(?:mock|image)-[1-4]\.(?:svg|png|jpe?g|webp)$/;

/** 只接受本机生成记录里的图片地址。远程地址、data 地址和路径跳转都拒绝。 */
export function localImagePath(url: string): string | null {
  if (typeof url !== "string") return null;
  const trimmed = url.trim();
  if (!trimmed || /[\u0000\\]/.test(trimmed)) return null;
  const lowered = trimmed.toLowerCase();
  if (
    lowered.startsWith("http://") ||
    lowered.startsWith("https://") ||
    lowered.startsWith("data:") ||
    lowered.startsWith("file:") ||
    lowered.startsWith("blob:") ||
    lowered.startsWith("//")
  ) {
    return null;
  }
  const path = trimmed.split("#")[0]?.split("?")[0] ?? "";
  if (!path.startsWith("/") || path.split("/").includes("..")) return null;
  return LOCAL_IMAGE.test(path) ? path : null;
}

const IMAGE_EXT = /\.(png|jpe?g|webp|gif|svg)$/i;

/** 下载到本机时用的文件名。去掉路径符号，保留服务端文件的序号和扩展名。 */
export function imageDownloadName(prompt: string, url: string, index = 1): string {
  const leaf = decodeURIComponent(url.split("?")[0]?.split("/").pop() || "");
  const extMatch = IMAGE_EXT.exec(leaf);
  const ext = extMatch ? extMatch[0].toLowerCase().replace(/\.jpeg$/, ".jpg") : ".png";
  const numbered = /-(\d+)\.[^.]+$/.exec(leaf);
  const order = numbered?.[1] || String(Number.isFinite(index) && index > 0 ? Math.floor(index) : 1);
  const base = prompt
    .replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 40)
    .replace(/(?:\.(?:png|jpe?g|webp|gif|svg))+$/i, "")
    .replace(/[. ]+$/g, "");
  return `${base || "生图"}-${order}${ext}`;
}
