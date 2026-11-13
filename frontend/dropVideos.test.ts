import assert from "node:assert/strict";
import test from "node:test";

import {
  folderImportMessage,
  mergePicked,
  videosFromEntries,
  type DropEntry,
  type PickedVideo,
} from "./src/dropVideos.ts";

function videoFile(name: string): File {
  return new File(["x"], name, { type: "video/mp4", lastModified: 10 });
}

function fileEntry(path: string, name = path.split("/").pop() || path): DropEntry {
  return {
    isFile: true,
    isDirectory: false,
    name,
    fullPath: `/${path}`,
    file: (success) => success(videoFile(name)),
  };
}

function dirEntry(name: string, children: DropEntry[], options?: { batches?: boolean; fullPath?: string }): DropEntry {
  const batches = options?.batches ? [children.slice(0, 1), children.slice(1)] : [children];
  return {
    isFile: false,
    isDirectory: true,
    name,
    fullPath: options?.fullPath ?? `/${name}`,
    createReader: () => ({
      readEntries(success) {
        success(batches.shift() ?? []);
      },
    }),
  };
}

function picked(label: string): PickedVideo {
  return { file: videoFile(label.split("/").pop() || label), label };
}

test("nested folders keep videos and skip other files", async () => {
  const tree = dirEntry("素材", [
    fileEntry("素材/封面.png", "封面.png"),
    fileEntry("素材/主视频.MP4", "主视频.MP4"),
    dirEntry("正片", [
      fileEntry("素材/正片/第二条.mov", "第二条.mov"),
      dirEntry("再深一层", [fileEntry("素材/正片/再深一层/第三条.mkv", "第三条.mkv")]),
    ]),
    dirEntry("__MACOSX", [fileEntry("素材/__MACOSX/垃圾.mp4", "垃圾.mp4")]),
    fileEntry("素材/.隐藏.mp4", ".隐藏.mp4"),
    fileEntry("素材/说明.txt", "说明.txt"),
  ]);

  const videos = await videosFromEntries([tree]);
  const labels = videos.map((item) => item.label);
  assert.deepEqual(
    labels,
    [...labels].sort((left, right) => left.localeCompare(right, "zh-CN", { numeric: true })),
  );
  assert.deepEqual(new Set(labels), new Set([
    "素材/主视频.MP4",
    "素材/正片/第二条.mov",
    "素材/正片/再深一层/第三条.mkv",
  ]));
});

test("readEntries keeps going until a batch is empty", async () => {
  const tree = dirEntry(
    "一批",
    [fileEntry("一批/a.webm", "a.webm"), fileEntry("一批/b.flv", "b.flv")],
    { batches: true },
  );
  const videos = await videosFromEntries([tree]);
  assert.deepEqual(
    videos.map((item) => item.label),
    ["一批/a.webm", "一批/b.flv"],
  );
});

test("merge stops at 20 and ignores videos already listed", () => {
  const current = Array.from({ length: 18 }, (_, index) => picked(`已有/${index}.mp4`));
  const incoming = [picked("已有/0.mp4"), ...Array.from({ length: 5 }, (_, index) => picked(`新/${index}.ts`))];
  const merged = mergePicked(current, incoming);
  assert.equal(merged.added, 2);
  assert.equal(merged.duplicate, 1);
  assert.equal(merged.overflow, 3);
  assert.equal(merged.next.length, 20);
});

test("folder import message explains how many were added", () => {
  assert.equal(folderImportMessage(0, { added: 0, duplicate: 0, overflow: 0 }).text, "文件夹里没有支持的视频");
  assert.equal(folderImportMessage(2, { added: 2, duplicate: 0, overflow: 0 }).text, "已加入 2 个视频");
  assert.match(folderImportMessage(30, { added: 20, duplicate: 0, overflow: 10 }).text, /还有 10 个没有加入/);
  assert.equal(folderImportMessage(2, { added: 0, duplicate: 2, overflow: 0 }).type, "info");
});
