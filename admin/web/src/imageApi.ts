import { ApiError, api, download } from "./api";
import type { ImageGenerateBody, ImageJob, ImageStatus } from "./imageWorkbench";

export function getImageStatus(): Promise<ImageStatus> {
  return api<ImageStatus>("/v1/image-workbench/status");
}

export function generateImage(body: ImageGenerateBody): Promise<ImageJob> {
  return api<ImageJob>("/v1/image-workbench/generate", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function listImageJobs(query: string, orientation: string): Promise<ImageJob[]> {
  const params = new URLSearchParams();
  if (query.trim()) params.set("query", query.trim());
  if (orientation) params.set("orientation", orientation);
  const suffix = params.toString();
  return api<ImageJob[]>(`/v1/image-workbench/jobs${suffix ? `?${suffix}` : ""}`);
}

export function deleteImageJob(id: string): Promise<{ ok: boolean }> {
  return api<{ ok: boolean }>(`/v1/image-workbench/jobs/${id}`, { method: "DELETE" });
}

async function saveBlob(blob: Blob, filename: string) {
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}

export async function downloadImageFile(url: string, filename: string): Promise<void> {
  if (url.startsWith("data:")) {
    const response = await fetch(url);
    if (!response.ok) throw new ApiError("下载失败", response.status);
    await saveBlob(await response.blob(), filename);
    return;
  }
  if (/^https?:\/\//i.test(url)) {
    const response = await fetch(url);
    if (!response.ok) throw new ApiError("下载失败", response.status);
    await saveBlob(await response.blob(), filename);
    return;
  }
  const path = url.includes("?") ? `${url}&download=1` : `${url}?download=1`;
  return download(path, filename);
}
