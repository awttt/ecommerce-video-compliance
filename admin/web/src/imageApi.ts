import { ApiError, api, download } from "./api";
import {
  desktopImageSave,
  runImageDownload,
  type ImageDownloadOutcome,
  type ImageGenerateBody,
  type ImageJob,
  type ImageStatus,
} from "./imageWorkbench";

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

export type { ImageDownloadOutcome };

export async function downloadImageFile(url: string, filename: string): Promise<ImageDownloadOutcome> {
  try {
    return await runImageDownload(url, filename, desktopImageSave(window.pywebview), (path, name) => download(path, name));
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError(error instanceof Error && error.message ? error.message : "下载失败", 0);
  }
}
