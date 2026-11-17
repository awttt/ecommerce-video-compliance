import assert from "node:assert/strict";
import test from "node:test";

import {
  applyPageChange,
  FILTER_ALL,
  filterFindings,
  findingsEmptyText,
  lastMatchingRow,
  locateFinding,
  pageCount,
  pageSlice,
  selectCategory,
} from "./src/findingsView.ts";

interface Row {
  类别: string;
  原文: string;
}

function row(类别: string, 原文: string): Row {
  return { 类别, 原文 };
}

test("page size choice changes the slice and the page count", () => {
  const items = Array.from({ length: 30 }, (_, index) => index);
  assert.equal(pageSlice(items, 1, 8).length, 8);
  assert.equal(pageCount(items.length, 8), 4);

  const chosen = applyPageChange(1, 20, items.length);
  assert.equal(chosen.pageSize, 20);
  assert.equal(chosen.pages, 2);
  assert.deepEqual(pageSlice(items, chosen.page, chosen.pageSize), items.slice(0, 20));

  const turned = applyPageChange(2, chosen.pageSize, items.length);
  assert.equal(turned.page, 2);
  assert.deepEqual(pageSlice(items, turned.page, turned.pageSize), items.slice(20));
});

test("shrinking the page size keeps the current page when it still exists", () => {
  const items = Array.from({ length: 25 }, (_, index) => index);
  assert.deepEqual(pageSlice(items, 3, 8), items.slice(16, 24));
  const next = applyPageChange(3, 10, items.length);
  assert.equal(next.pages, 3);
  assert.equal(next.page, 3);
  assert.deepEqual(pageSlice(items, next.page, next.pageSize), items.slice(20));
});

test("a page past the end is pulled back after the page size grows", () => {
  const items = Array.from({ length: 25 }, (_, index) => index);
  const next = applyPageChange(4, 50, items.length);
  assert.equal(next.pages, 1);
  assert.equal(next.page, 1);
  assert.equal(pageSlice(items, next.page, next.pageSize).length, 25);
});

test("invalid page size falls back to 8", () => {
  const items = [1, 2, 3];
  const next = applyPageChange(1, Number.NaN, items.length);
  assert.equal(next.pageSize, 8);
  assert.deepEqual(pageSlice(items, next.page, next.pageSize), items);
});

test("category filter keeps only that category and starts at page 1", () => {
  const items = [
    row("价格", "¥9.9"),
    row("规格", "500ml"),
    row("品牌", "耐克"),
    row("价格", "十九元"),
    row("夸大宣传", "全网第一"),
    row("解析失败", ""),
    row("price", "¥1"),
    ...Array.from({ length: 8 }, (_, index) => row("价格", `价${index}`)),
  ];
  const selected = selectCategory("价格");
  assert.deepEqual(selected, { filter: "价格", page: 1 });
  const prices = filterFindings(items, selected.filter);
  assert.equal(prices.length, 11);
  assert.ok(prices.every((item) => item.类别 === "价格" || item.类别 === "price"));
  assert.equal(pageCount(prices.length, 8), 2);
  assert.equal(pageSlice(prices, selected.page, 8).length, 8);
  assert.deepEqual(
    pageSlice(prices, 2, 8).map((item) => item.原文),
    ["价5", "价6", "价7"],
  );

  assert.deepEqual(
    filterFindings(items, "规格").map((item) => item.原文),
    ["500ml"],
  );
  assert.deepEqual(
    filterFindings(items, "品牌").map((item) => item.原文),
    ["耐克"],
  );
  assert.equal(filterFindings(items, FILTER_ALL).length, items.length);
  assert.equal(findingsEmptyText(false, "规格"), "没有「规格」类问题");
  assert.equal(selectCategory("不存在").filter, FILTER_ALL);
});

test("marker click opens the page that contains the row", () => {
  const specs = Array.from({ length: 10 }, (_, index) => row("规格", `规${index}`));
  const prices = Array.from({ length: 9 }, (_, index) => row("价格", `价${index}`));
  const items = [...specs, ...prices];
  const target = prices[8];
  assert.equal(items.indexOf(target), 18);

  const kept = locateFinding(items, target, "价格", 8);
  assert.equal(kept.clearedFilter, false);
  assert.equal(kept.filter, "价格");
  assert.equal(kept.index, 8);
  assert.equal(kept.page, 2);

  const cleared = locateFinding(items, target, "规格", 8);
  assert.equal(cleared.clearedFilter, true);
  assert.equal(cleared.filter, FILTER_ALL);
  assert.equal(cleared.index, 18);
  assert.equal(cleared.page, 3);
});

test("marker click leaves a matching filter in place", () => {
  const items = [row("品牌", "甲"), row("价格", "¥1"), row("品牌", "乙")];
  const located = locateFinding(items, items[2], "品牌", 8);
  assert.equal(located.clearedFilter, false);
  assert.equal(located.filter, "品牌");
  assert.equal(located.page, 1);
  assert.equal(located.index, 1);
});

test("missing row does not clear the filter", () => {
  const items = [row("价格", "¥1")];
  const located = locateFinding(items, row("规格", "不在列表里"), "价格", 8);
  assert.equal(located.index, -1);
  assert.equal(located.clearedFilter, false);
  assert.equal(located.filter, "价格");
});

test("scroll target prefers the later row when the key is repeated", () => {
  const rows = [
    { getAttribute: () => "a" },
    { getAttribute: () => "b" },
    { getAttribute: () => "a" },
  ];
  assert.equal(lastMatchingRow(rows, "a"), rows[2]);
  assert.equal(lastMatchingRow(rows, "missing"), null);
});
