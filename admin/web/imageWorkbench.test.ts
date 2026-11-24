import assert from "node:assert/strict";
import test from "node:test";

import {
  estimateCostYuan,
  generationBlockReason,
  imageOrientation,
  modelOptionLabel,
  modelSelectable,
  sizeEnabledFor,
  sizeOptionLabel,
  type ImageModelOption,
  type ImageQualityOption,
  type ImageSizeOption,
} from "./src/imageWorkbench.ts";

const FLARE = "gpt-image-2.5-flare";

const serverSizes: ImageSizeOption[] = [
  {
    value: "auto",
    label: "Auto",
    note: "本站网关待验 · 未经上游实测",
    verified: false,
    gateway_pending: true,
    verified_for: [],
    enabled: true,
  },
  {
    value: "1024x1024",
    label: "1024x1024",
    note: "本站网关待验 · 未经上游实测",
    verified: false,
    gateway_pending: true,
    verified_for: [],
    enabled: true,
  },
  {
    value: "2048x1152",
    label: "2048x1152",
    note: "2K 横版 · 上游已验证尺寸（仅 gpt-image-2.5-flare，n=1、省略 quality） · 本站网关待验",
    verified: true,
    gateway_pending: true,
    verified_for: [FLARE],
    enabled: true,
  },
  {
    value: "3840x2160",
    label: "3840x2160",
    note: "4K 横版 · 上游已验证尺寸（仅 gpt-image-2.5-flare，n=1、省略 quality） · 本站网关待验",
    verified: true,
    gateway_pending: true,
    verified_for: [FLARE],
    enabled: true,
  },
  { value: "2048x2048", label: "2048x2048", note: "2K 方版 · 未验证", verified: false, verified_for: [], enabled: false },
  { value: "2560x1440", label: "2560x1440", note: "未验证", verified: false, verified_for: [], enabled: false },
  { value: "2160x3840", label: "2160x3840", note: "4K 竖版 · 未验证", verified: false, verified_for: [], enabled: false },
];

const qualities: ImageQualityOption[] = [
  { value: "auto", label: "Auto", enabled: true },
  { value: "high", label: "High", enabled: false, note: "未实测 · 禁用" },
];

const flare: ImageModelOption = {
  id: FLARE,
  label: FLARE,
  ready: false,
  upstream_verified: true,
  selectable: true,
  note: "上游已验证 · 本站网关待验",
};
const sunburst: ImageModelOption = {
  id: "gpt-image-2.5-sunburst",
  label: "gpt-image-2.5-sunburst",
  ready: false,
  upstream_verified: false,
  selectable: false,
  note: "未按专组能力确认 · 未就绪",
};

test("orientation follows the requested pixels", () => {
  assert.equal(imageOrientation("auto"), "auto");
  assert.equal(imageOrientation("1024x1024"), "square");
  assert.equal(imageOrientation("2048x1152"), "landscape");
  assert.equal(imageOrientation("3840x2160"), "landscape");
  assert.equal(imageOrientation("1024x1536"), "portrait");
  assert.equal(imageOrientation("garbage"), "auto");
});

test("estimate is n times the server price and is not an upstream cost", () => {
  const pricing = {
    currency: "CNY" as const,
    unit: "张",
    price_per_image: 0.1,
    display: "¥0.10 / 张",
    note: "预估，实际以网关按 ImageCount 实账为准，不是上游成本",
  };
  assert.equal(estimateCostYuan(pricing, 1), "0.10");
  assert.equal(estimateCostYuan(pricing, 4), "0.40");
  assert.equal(estimateCostYuan(null, 4), "0.00");
  assert.equal(pricing.note.includes("上游成本"), true);
});

test("only the two measured sizes are upstream-verified and scoped to flare", () => {
  const scoped = serverSizes.filter((item) => (item.verified_for ?? []).length > 0);
  assert.deepEqual(
    scoped.map((item) => item.value).sort(),
    ["2048x1152", "3840x2160"],
  );
  for (const item of scoped) {
    assert.equal(item.verified, true);
    assert.equal(sizeEnabledFor(item, FLARE), true);
    assert.equal(sizeEnabledFor(item, "gpt-image-2.5-sunburst"), false);
    assert.equal(sizeEnabledFor(item, ""), false);
    const label = sizeOptionLabel(item);
    assert.equal(label.includes("上游已验证"), true);
    assert.equal(label.includes("本站网关待验"), true);
    assert.equal(label.includes("已支持"), false);
  }
  for (const value of ["2048x2048", "2560x1440", "2160x3840"]) {
    const item = serverSizes.find((entry) => entry.value === value)!;
    assert.equal(item.verified, false);
    assert.equal(item.enabled, false);
    assert.equal(sizeEnabledFor(item, FLARE), false);
    assert.equal(sizeOptionLabel(item).includes("未验证"), true);
    assert.equal(sizeOptionLabel(item).includes("已支持"), false);
  }
  const oneK = serverSizes.find((item) => item.value === "1024x1024")!;
  assert.equal(oneK.verified, false);
  assert.equal(oneK.enabled, true);
  assert.equal(sizeOptionLabel(oneK).includes("本站网关待验"), true);
  assert.equal(sizeOptionLabel(oneK).includes("上游已验证"), false);
});

test("client blocks unverified combinations before submit", () => {
  const base = {
    groupReady: true,
    model: FLARE,
    models: [flare, sunburst],
    size: "2048x1152",
    sizes: serverSizes,
    quality: "auto",
    qualities,
    n: 1,
  };
  assert.equal(generationBlockReason(base), "");
  assert.equal(generationBlockReason({ ...base, groupReady: false }), "当前账号没有生图专用分组");
  assert.equal(generationBlockReason({ ...base, model: sunburst.id }), "请选择可用的生图模型");
  assert.equal(generationBlockReason({ ...base, size: "2560x1440" }), "该尺寸对当前模型不可用");
  assert.equal(generationBlockReason({ ...base, n: 4 }), "该尺寸仅限 1 张且默认质量");
  assert.equal(generationBlockReason({ ...base, quality: "high" }), "该质量尚未实测");
  assert.equal(generationBlockReason({ ...base, size: "1024x1024", n: 4 }), "");
});

test("model labels do not call an unverified model ready", () => {
  const flareLabel = modelOptionLabel(flare);
  assert.equal(flareLabel.includes("上游已验证"), true);
  assert.equal(flareLabel.includes("本站网关待验"), true);
  assert.equal(flareLabel.includes("已支持"), false);
  assert.equal(modelOptionLabel(sunburst).includes("未就绪"), true);
  assert.equal(modelSelectable(flare), true);
  assert.equal(modelSelectable(sunburst), false);
  assert.equal(modelSelectable({ ...sunburst, ready: true, selectable: false }), false);
});
