import assert from "node:assert/strict";
import test from "node:test";

import {
  EXAMPLE_PROMPTS,
  SIZE_OPTIONS,
  buildImagePayload,
  filterBatches,
  isGenerateShortcut,
  type ImageBatch,
} from "./src/imageWorkbench.ts";

const batch = (patch: Partial<ImageBatch>): ImageBatch => ({
  id: "1",
  prompt: "湖面",
  model: "preview-mock",
  model_label: "预览模拟（不是上游模型）",
  size: "1024x1024",
  size_label: "1024×1024",
  orientation: "square",
  count: 1,
  status: "done",
  message: "",
  preview: true,
  upstream_sent: false,
  ...patch,
});

test("example chips and sizes match the verified reference layout", () => {
  assert.equal(EXAMPLE_PROMPTS.length, 4);
  assert.deepEqual(SIZE_OPTIONS.map((item) => item.id), ["auto", "1024x1024", "1536x1024", "1024x1536"]);
  assert.deepEqual(SIZE_OPTIONS.map((item) => item.orientation), ["auto", "square", "landscape", "portrait"]);
});

test("payload stays local and cannot name a group or gateway", () => {
  const payload = buildImagePayload({ prompt: "  湖面  ", model: "preview-mock", size: "1536x1024", count: 9 });
  assert.equal(payload.prompt, "湖面");
  assert.equal(payload.size, "1536x1024");
  assert.equal(payload.count, 4);
  assert.equal(payload.quality, "auto");
  assert.equal(payload.async_generation, false);
  assert.equal("group" in payload, false);
  assert.equal("base_url" in payload, false);
  assert.equal("api_key" in payload, false);
});

test("history filter uses prompt, model and orientation", () => {
  const rows = [
    batch({ id: "a", prompt: "湖面日出", orientation: "square" }),
    batch({ id: "b", prompt: "城市", orientation: "landscape", model: "preview-mock" }),
  ];
  assert.deepEqual(filterBatches(rows, "日出", "all").map((item) => item.id), ["a"]);
  assert.deepEqual(filterBatches(rows, "", "landscape").map((item) => item.id), ["b"]);
  assert.deepEqual(filterBatches(rows, "preview", "square").map((item) => item.id), ["a"]);
});

test("generate shortcut is ctrl or meta enter", () => {
  assert.equal(isGenerateShortcut({ key: "Enter", ctrlKey: true, metaKey: false }), true);
  assert.equal(isGenerateShortcut({ key: "Enter", ctrlKey: false, metaKey: true }), true);
  assert.equal(isGenerateShortcut({ key: "Enter", ctrlKey: false, metaKey: false }), false);
});
