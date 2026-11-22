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
  sentence_start?: number | null;
  sentence_end?: number | null;
}

export interface CutSpan {
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
  speech_retry: "" | "transcribe" | "judge";
  mock: boolean;
  output_ready: boolean;
  preview_status: string;
  preview_hint: string;
  cuts: CutSpan[];
  edit_rendered: boolean;
  has_edit_plan: boolean;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  started_at: string | null;
  paused_total_sec?: number;
  hold_sec?: number;
  paused_at?: string | null;
  findings: Finding[];
}

export interface PhraseRule {
  text: string;
  category: string;
  source?: "口播" | "画面";
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
  motion_enabled: boolean;
  timeout_sec: number;
  mock_mode: boolean;
  fault_bad_json: boolean;
  fault_tencent: boolean;
  fault_partial_frames: boolean;
  mcp_enabled: boolean;
  asr_enabled: boolean;
  has_tencent_secret: boolean;
  tencent_secret_id_masked: string;
  tencent_secret_key_masked: string;
  tencent_secret_source: string;
  tencent_region: string;
  asr_engine: string;
  rules: DetectionRules;
  account_logged_in: boolean;
  account_email: string;
  remembered_email: string;
  account_balance: number | null;
  account_frozen_balance: number | null;
  account_today_cost: number | null;
  account_total_cost: number | null;
}

export interface AccountOptions {
  reachable: boolean;
  registration_enabled: boolean;
  email_verify_enabled: boolean;
  captcha: string;
  message: string;
}

export interface AccountAction {
  settings: Settings;
  requires_2fa: boolean;
  email_masked: string;
  message: string;
}

export interface BatchResult {
  jobs: Job[];
  errors: { filename: string; detail: string }[];
}

export const TERMINAL = ["done", "failed", "cancelled"];

/** 时间轴与图例色块：低饱和、同明度，避免三原色抢视觉。改动时同步 styles.css 的 --cat-* token */
export const CATEGORY_COLOR: Record<string, string> = {
  价格: "#c0604a",
  规格: "#b5872f",
  品牌: "#4b7fa6",
  夸大宣传: "#7a4e78",
};
