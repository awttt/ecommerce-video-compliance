import assert from "node:assert/strict";
import test from "node:test";

import { batchElapsed, jobElapsed } from "./src/elapsed.ts";
import type { Job } from "./src/types.ts";

function job(partial: Partial<Job>): Job {
  return {
    id: "a",
    filename: "a.mp4",
    status: "running",
    stage: "recognizing",
    message: "",
    progress: 10,
    fps: 1,
    concurrency: 1,
    duration_sec: 10,
    frames_estimated: 1,
    frames_extracted: 1,
    frames_recognized: 0,
    failed_frames: 0,
    hit_count: 0,
    error: null,
    speech_stage: "",
    speech_message: "",
    asr_error: null,
    mock: false,
    output_ready: false,
    preview_hint: "",
    cuts: [],
    edit_rendered: false,
    has_edit_plan: false,
    created_at: "2026-11-13T00:00:00.000Z",
    updated_at: "2026-11-13T00:00:30.000Z",
    finished_at: null,
    started_at: "2026-11-13T00:00:00.000Z",
    findings: [],
    ...partial,
  };
}

const at = (seconds: number) => Date.parse("2026-11-13T00:00:00.000Z") + seconds * 1000;

test("running elapsed follows the clock", () => {
  assert.equal(jobElapsed(job({}), at(12)), "00:12");
});

test("paused elapsed stays at the pause mark and skips earlier pauses", () => {
  const paused = job({
    status: "paused",
    paused_at: "2026-11-13T00:00:08.000Z",
    paused_total_sec: 3,
    updated_at: "2026-11-13T00:01:00.000Z",
  });
  assert.equal(jobElapsed(paused, at(8)), "00:05");
  assert.equal(jobElapsed(paused, at(40)), "00:05");
});

test("resumed elapsed leaves the pause gap out", () => {
  const resumed = job({ paused_total_sec: 10, paused_at: null });
  assert.equal(jobElapsed(resumed, at(25)), "00:15");
});

test("batch clock freezes while every unfinished video is paused", () => {
  const first = job({
    id: "1",
    status: "paused",
    paused_at: "2026-11-13T00:00:20.000Z",
    hold_sec: 4,
  });
  const second = job({
    id: "2",
    status: "queued",
    started_at: null,
    created_at: "2026-11-13T00:00:05.000Z",
    paused_at: "2026-11-13T00:00:20.000Z",
    hold_sec: 4,
  });
  assert.equal(batchElapsed([first, second], at(20)), "00:16");
  assert.equal(batchElapsed([first, second], at(50)), "00:16");
});

test("batch clock continues after resume without the paused stretch", () => {
  const first = job({ hold_sec: 10 });
  const second = job({
    id: "2",
    status: "queued",
    started_at: null,
    created_at: "2026-11-13T00:00:05.000Z",
    hold_sec: 10,
    paused_at: null,
  });
  assert.equal(batchElapsed([first, second], at(30)), "00:20");
});

test("elapsed freezes at the last update when the server has no pause stamp", () => {
  const id = `client-job-${Math.random()}`;
  const paused = job({
    id,
    status: "paused",
    paused_at: null,
    paused_total_sec: 0,
    updated_at: "2026-11-13T00:00:08.000Z",
  });
  assert.equal(jobElapsed(paused, at(8)), "00:08");
  assert.equal(jobElapsed(paused, at(40)), "00:08");
  const resumed = job({
    id,
    status: "running",
    paused_at: null,
    paused_total_sec: 0,
    updated_at: "2026-11-13T00:00:40.000Z",
  });
  assert.equal(jobElapsed(resumed, at(40)), "00:08");
  assert.equal(jobElapsed(resumed, at(47)), "00:15");
});

test("batch clock freezes from the paused video update when pause stamps are absent", () => {
  const suffix = Math.random().toString(16).slice(2);
  const first = job({
    id: `client-a-${suffix}`,
    status: "paused",
    paused_at: null,
    hold_sec: 0,
    updated_at: "2026-11-13T00:00:20.000Z",
  });
  const second = job({
    id: `client-b-${suffix}`,
    status: "queued",
    started_at: null,
    created_at: "2026-11-13T00:00:05.000Z",
    updated_at: "2026-11-13T00:00:05.000Z",
    paused_at: null,
    hold_sec: 0,
  });
  assert.equal(batchElapsed([first, second], at(20)), "00:20");
  assert.equal(batchElapsed([first, second], at(50)), "00:20");
  const resumed = job({ ...first, status: "running", updated_at: "2026-11-13T00:00:50.000Z" });
  assert.equal(batchElapsed([resumed, second], at(50)), "00:20");
  assert.equal(batchElapsed([resumed, second], at(56)), "00:26");
});
