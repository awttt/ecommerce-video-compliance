import assert from "node:assert/strict";
import test from "node:test";

import { desktopImageSave, interpretImageSave, localImagePath, runImageDownload } from "./src/imageWorkbench.ts";

const LOCAL = "/v1/image-workbench/jobs/" + "a".repeat(32) + "/files/image-1.png";

test("a webview without the save bridge is not treated as a browser download", () => {
  assert.equal(desktopImageSave(undefined), null);
  assert.equal(desktopImageSave(null), null);
  assert.equal(desktopImageSave({}), "unavailable");
  assert.equal(desktopImageSave({ api: {} }), "unavailable");
  const bridge = desktopImageSave({
    api: { save_generated_image: async () => ({ ok: true, cancelled: false, bytes: 8, filename: "a.png" }) },
  });
  assert.equal(typeof bridge, "function");
});

test("cancel is not success and a token-like error is not shown", () => {
  assert.equal(interpretImageSave({ ok: false, cancelled: true, bytes: 0 }), "cancelled");
  assert.equal(interpretImageSave({ ok: true, cancelled: true, bytes: 12 }), "cancelled");
  assert.equal(interpretImageSave({ ok: true, cancelled: false, bytes: 12, filename: "礼盒-1.png" }), "saved");
  assert.throws(() => interpretImageSave({ ok: false, message: "图片不存在或已删除" }), /图片不存在或已删除/);
  assert.throws(() => interpretImageSave({ ok: true, bytes: 0 }), /下载失败/);
  assert.throws(() => interpretImageSave({ ok: false, message: "Bearer sk-secret" }), /下载失败/);
});

test("only a local generated image path can be saved", () => {
  assert.equal(localImagePath(LOCAL), LOCAL);
  assert.equal(localImagePath(LOCAL + "?download=1"), LOCAL);
  assert.equal(
    localImagePath("/v1/image-workbench/jobs/" + "a".repeat(32) + "/files/mock-1.svg"),
    "/v1/image-workbench/jobs/" + "a".repeat(32) + "/files/mock-1.svg",
  );
  assert.equal(localImagePath("https://token4u.cc/image.png"), null);
  assert.equal(localImagePath("data:image/png;base64,aaaa"), null);
  assert.equal(localImagePath("/v1/image-workbench/jobs/" + "a".repeat(32) + "/files/../image-1.png"), null);
});

test("download uses the desktop bridge and does not report cancel as success", async () => {
  let seen = "";
  const outcome = await runImageDownload(
    LOCAL,
    "礼盒-1.png",
    async (url, filename) => {
      seen = `${url} ${filename}`;
      return { ok: false, cancelled: true, bytes: 0, filename: "", message: "" };
    },
    async () => {
      throw new Error("browser download");
    },
  );
  assert.equal(outcome, "cancelled");
  assert.equal(seen, `${LOCAL} 礼盒-1.png`);
});

test("a saved file reports the byte count as success", async () => {
  const outcome = await runImageDownload(
    LOCAL,
    "礼盒-1.png",
    async () => ({ ok: true, cancelled: false, bytes: PNG_BYTES, filename: "礼盒-1.png" }),
    async () => {
      throw new Error("browser download");
    },
  );
  assert.equal(outcome, "saved");
});

test("a missing desktop bridge reports that saving is not ready", async () => {
  await assert.rejects(
    () => runImageDownload(LOCAL, "礼盒-1.png", "unavailable", async () => undefined),
    /桌面保存还没准备好/,
  );
});

test("a browser download of a local file is not reported as saved", async () => {
  let seen = "";
  const outcome = await runImageDownload(LOCAL, "礼盒-1.png", null, async (path) => {
    seen = path;
  });
  assert.equal(outcome, "started");
  assert.equal(seen, `${LOCAL}?download=1`);
});

test("the browser path refuses a remote image url", async () => {
  await assert.rejects(
    () => runImageDownload("https://evil.example/a.png", "a.png", null, async () => undefined),
    /只能保存本机已经生成的图片/,
  );
});

const PNG_BYTES = 67;
