import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import * as XLSX from "xlsx";
import { importTemplateBytes, issuesFor, inviteUrl, rowsFromGrid, rowsFromWorkbook, type ImportDraft } from "./src/importMembers.ts";

function workbook(grid: unknown[][]) {
  const book = XLSX.utils.book_new();
  const sheet = XLSX.utils.aoa_to_sheet(grid);
  XLSX.utils.book_append_sheet(book, sheet, "Sheet1");
  return XLSX.write(book, { type: "buffer", bookType: "xlsx" }) as Uint8Array;
}

test("reads name, email, and title, and trims the email", async () => {
  const rows = await rowsFromWorkbook(
    workbook([
      ["备注"],
      ["职位", "姓名", "邮箱号"],
      ["运营", " 甲 ", " A@QQ.com "],
      ["", "", ""],
      ["剪辑", "乙", "b@qq.com"],
    ]),
  );
  assert.deepEqual(
    rows.map((row) => [row.title, row.name, row.email]),
    [
      ["运营", "甲", "a@qq.com"],
      ["剪辑", "乙", "b@qq.com"],
    ],
  );
});

test("the import template is an empty sheet with the expected headers", async () => {
  const bytes = await importTemplateBytes();
  await assert.rejects(() => rowsFromWorkbook(bytes), /没有可邀请的人/);
});

test("rejects a sheet without the required columns", () => {
  assert.throws(() => rowsFromGrid([["职位", "备注"], ["运营", "无"]]), /姓名/);
});

test("flags a broken email, a duplicate, and someone already invited", () => {
  const rows = rowsFromGrid([
    ["职位", "姓名", "邮箱号"],
    ["运营", "甲", "a@qq.com"],
    ["剪辑", "乙", "a@qq.com"],
    ["运营", "丙", "3226578304qq.com"],
    ["运营", "丁", "d@qq.com"],
  ]);
  const issues = issuesFor(rows, { members: ["d@qq.com"], invites: [] });
  assert.equal(issues[rows[1].key], "这个邮箱在表里重复了");
  assert.equal(issues[rows[2].key], "邮箱不正确");
  assert.equal(issues[rows[3].key], "这个邮箱已经开通");
  assert.equal(issues[rows[0].key], "这个邮箱在表里重复了");
});

test("a finished row blocks the same email from being sent again", () => {
  const rows: ImportDraft[] = rowsFromGrid([
    ["姓名", "邮箱"],
    ["甲", "a@qq.com"],
    ["乙", "a@qq.com"],
  ]);
  rows[0] = { ...rows[0], status: "ok", token: "tok" };
  const issues = issuesFor(rows);
  assert.equal(issues[rows[0].key], undefined);
  assert.equal(issues[rows[1].key], "这个邮箱刚刚已经导入过");
});

test("local console links point at the cloud invite page", () => {
  assert.equal(
    inviteUrl("abc", { hostname: "127.0.0.1", origin: "http://127.0.0.1:8780" }),
    "https://admin.token4u.cc/invite/abc",
  );
  assert.equal(
    inviteUrl("abc", { hostname: "admin.token4u.cc", origin: "https://admin.token4u.cc" }),
    "https://admin.token4u.cc/invite/abc",
  );
});

test("reads the downloaded member workbook", async () => {
  const path = "/Users/awttt/Downloads/邮箱号(1).xlsx";
  if (!existsSync(path)) return;
  const rows = await rowsFromWorkbook(readFileSync(path));
  assert.equal(rows.length, 22);
  assert.equal(rows[0].name, "陈德辉");
  assert.equal(rows[0].email, "2468875601@qq.com");
  assert.equal(rows.find((row) => row.name === "罗郅丽")?.email, "2772701912@qq.com");
  assert.equal(rows.find((row) => row.name === "廖俊滔")?.email, "2519215646@qq.com");
  const issues = issuesFor(rows);
  assert.equal(issues[rows.find((row) => row.name === "杨湛桃")!.key], "邮箱不正确");
  const shared = rows.filter((row) => row.email === "948840255@qq.com");
  assert.deepEqual(
    shared.map((row) => row.name),
    ["钟嘉慧", "刘娇"],
  );
  assert.equal(issues[shared[0].key], "这个邮箱在表里重复了");
  assert.equal(Object.keys(issues).length, 3);
});
