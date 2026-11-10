export interface Finding {
  时间点: string;
  类别: string;
  来源: string;
  原文: string;
  位置: string;
  置信度: string;
  判定理由: string;
  备注: string;
  start_sec: number;
  end_sec: number;
}

export interface Job {
  id: string;
  filename: string;
  status: string;
  stage: string;
  message: string;
  progress: number;
  fps: number;
  concurrency: number;
  duration_sec: number | null;
  frames_estimated: number;
  frames_extracted: number;
  frames_recognized: number;
  failed_frames: number;
  hit_count: number;
  error: string | null;
  speech_stage: string;
  speech_message: string;
  asr_error: string | null;
  mock: boolean;
  output_ready: boolean;
  preview_hint: string;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  findings: Finding[];
}

export interface PhraseRule {
  text: string;
  category: string;
}

export interface DetectionRules {
  categories: string[];
  extra_phrases: PhraseRule[];
  ignore_phrases: string[];
  note: string;
}

export interface Settings {
  has_api_key: boolean;
  api_key_masked: string;
  api_key_source: string;
  base_url: string;
  model: string;
  fps: number;
  concurrency: number;
  max_width: number;
  jpeg_q: number;
  detail: string;
  thinking: boolean;
  timeout_sec: number;
  mock_mode: boolean;
  asr_enabled: boolean;
  has_tencent_secret: boolean;
  tencent_secret_id_masked: string;
  tencent_secret_key_masked: string;
  tencent_secret_source: string;
  tencent_region: string;
  asr_engine: string;
  rules: DetectionRules;
}

export interface BatchResult {
  jobs: Job[];
  errors: { filename: string; detail: string }[];
}

export const TERMINAL = ["done", "failed", "cancelled"];

export const CATEGORY_COLOR: Record<string, string> = {
  价格: "#cf1322",
  规格: "#d46b08",
  品牌: "#1677ff",
};
