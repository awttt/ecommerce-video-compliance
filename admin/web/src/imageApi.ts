import { api } from "./api";
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
