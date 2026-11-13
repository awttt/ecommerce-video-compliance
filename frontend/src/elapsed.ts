import { TERMINAL, type Job } from "./types.ts";

const STORAGE_KEY = "evc-elapsed-hold";

type Span = { open: number | null; heldMs: number };
type Ledger = { jobs: Record<string, Span>; batches: Record<string, Span> };

let ledger: Ledger | null = null;

function emptyLedger(): Ledger {
  return { jobs: {}, batches: {} };
}

function readLedger(): Ledger {
  if (ledger) return ledger;
  ledger = emptyLedger();
  try {
    if (typeof sessionStorage === "undefined") return ledger;
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return ledger;
    const parsed = JSON.parse(raw) as Partial<Ledger>;
    ledger = {
      jobs: parsed.jobs && typeof parsed.jobs === "object" ? parsed.jobs : {},
      batches: parsed.batches && typeof parsed.batches === "object" ? parsed.batches : {},
    };
  } catch {
    ledger = emptyLedger();
  }
  return ledger;
}

function writeLedger(): void {
  try {
    if (typeof sessionStorage === "undefined" || !ledger) return;
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(ledger));
  } catch {
    // 浏览器禁止写入时，这一页里的记账仍然有效。
  }
}

function remember(map: Record<string, Span>, key: string, open: number | null, heldMs: number): Span {
  const span: Span = { open, heldMs };
  map[key] = span;
  writeLedger();
  return span;
}

function serverOwnsJob(job: Job): boolean {
  return Boolean(job.paused_at) || (job.paused_total_sec || 0) > 0;
}

function serverOwnsBatch(jobs: Job[]): boolean {
  return jobs.some((item) => Boolean(item.paused_at) || (item.hold_sec || 0) > 0);
}

/**
 * 当前进程还没把暂停点写进任务时，用页面里记住的时刻。
 * 暂停期间结束时间停在刚暂停的那一刻，恢复后把这段从耗时里扣掉。
 */
function clientHold(
  map: Record<string, Span>,
  key: string,
  paused: boolean,
  mark: number | null,
  now: number,
  closeAt: number,
): { end: number | null; heldMs: number } {
  const current = map[key];
  if (paused) {
    if (current?.open == null) {
      const span = remember(map, key, mark ?? now, current?.heldMs ?? 0);
      return { end: span.open, heldMs: span.heldMs };
    }
    return { end: current.open, heldMs: current.heldMs };
  }
  if (!current) return { end: null, heldMs: 0 };
  if (current.open != null) {
    const span = remember(map, key, null, current.heldMs + Math.max(0, closeAt - current.open));
    return { end: null, heldMs: span.heldMs };
  }
  return { end: null, heldMs: current.heldMs };
}

export function parseStamp(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function formatElapsed(ms: number, done: boolean): string {
  const total = Math.max(0, Math.round(ms / 1000));
  if (total === 0) return done ? "不足 1 秒" : "00:00";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const clock = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return hours > 0 ? `${hours}:${clock}` : clock;
}

function pausedMs(job: Job, end: number, terminal: boolean): number {
  let paused = Math.max(0, (job.paused_total_sec || 0) * 1000);
  const opened = parseStamp(job.paused_at);
  if (terminal && opened != null) paused += Math.max(0, end - opened);
  return paused;
}

/** 已开始的视频按实际处理时间计，暂停的那段不计入。旧记录没有开始时间时，用创建到结束的间隔。 */
export function jobElapsed(job: Job, now: number): string | null {
  const started = parseStamp(job.started_at);
  const created = parseStamp(job.created_at);
  const terminal = TERMINAL.includes(job.status);
  const start = started ?? (terminal ? created : null);
  if (start == null) return null;
  const opened = parseStamp(job.paused_at);
  let end = terminal ? parseStamp(job.finished_at) ?? parseStamp(job.updated_at) ?? now : now;
  if (!terminal && job.status === "paused" && opened != null) end = opened;
  let paused = pausedMs(job, end, terminal);
  if (!serverOwnsJob(job)) {
    const client = clientHold(
      readLedger().jobs,
      job.id,
      !terminal && job.status === "paused",
      parseStamp(job.updated_at),
      now,
      end,
    );
    if (client.end != null) end = client.end;
    paused += client.heldMs;
  }
  return formatElapsed(Math.max(0, end - start - paused), terminal);
}

/** 一批视频的墙钟时间。队列暂停时停住，已经暂停过的时间不再计入。 */
export function batchElapsed(jobs: Job[], now: number): string | null {
  if (jobs.length < 2) return null;
  const starts = jobs.map((item) => parseStamp(item.created_at)).filter((value): value is number => value != null);
  if (starts.length === 0) return null;
  const done = jobs.every((item) => TERMINAL.includes(item.status));
  let end = now;
  if (done) {
    const ends = jobs
      .map((item) => parseStamp(item.finished_at) ?? parseStamp(item.updated_at))
      .filter((value): value is number => value != null);
    if (ends.length > 0) end = Math.max(...ends);
  } else {
    const unfinished = jobs.filter((item) => !TERMINAL.includes(item.status));
    const opened = unfinished
      .map((item) => parseStamp(item.paused_at))
      .filter((value): value is number => value != null);
    const held = unfinished.length > 0 && unfinished.every((item) => item.paused_at);
    if (held && opened.length > 0) end = Math.min(...opened);
  }
  const closed = Math.max(0, ...jobs.map((item) => item.hold_sec || 0));
  let heldMs = closed * 1000;
  if (done) {
    const opened = jobs
      .map((item) => parseStamp(item.paused_at))
      .filter((value): value is number => value != null);
    if (opened.length > 0) heldMs += Math.max(0, end - Math.min(...opened));
  }
  if (!serverOwnsBatch(jobs)) {
    const unfinished = jobs.filter((item) => !TERMINAL.includes(item.status));
    const pausing = unfinished.filter((item) => item.status === "paused");
    const held = !done && pausing.length > 0 && unfinished.every((item) => item.status === "paused" || item.status === "queued");
    const marks = pausing.map((item) => parseStamp(item.updated_at)).filter((value): value is number => value != null);
    const key = jobs.map((item) => item.id).sort().join(",");
    const client = clientHold(readLedger().batches, key, held, marks.length > 0 ? Math.min(...marks) : null, now, end);
    if (client.end != null) end = client.end;
    heldMs += client.heldMs;
  }
  return formatElapsed(Math.max(0, end - Math.min(...starts) - heldMs), done);
}
