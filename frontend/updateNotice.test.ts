import assert from "node:assert/strict";
import test from "node:test";

import { manualUpdateNotice, type UpdateView } from "./src/updateNotice.ts";

function info(partial: Partial<UpdateView>): UpdateView {
  return {
    current: "0.1.2",
    latest: "",
    available: false,
    phase: "idle",
    can_apply: true,
    message: "",
    check_state: "idle",
    check_message: "",
    ...partial,
  };
}

test("a failed check is not reported as up to date", () => {
  const notice = manualUpdateNotice(info({ check_state: "failed" }));
  assert.equal(notice.level, "error");
  assert.equal(notice.confirmInstall, false);
  assert.match(notice.text, /不能确定是不是最新版本/);
  assert.equal(notice.text.includes("已是最新"), false);
});

test("a failed recheck keeps the earlier update behind confirmation", () => {
  const notice = manualUpdateNotice(info({ check_state: "failed", available: true, latest: "0.2.0" }));
  assert.equal(notice.level, "warning");
  assert.equal(notice.confirmInstall, true);
  assert.match(notice.text, /0\.2\.0/);
  assert.match(notice.text, /确认后才会下载安装/);
});

test("finding a new version does not install by itself", () => {
  const notice = manualUpdateNotice(info({ check_state: "available", available: true, latest: "0.2.0" }));
  assert.equal(notice.confirmInstall, true);
  assert.match(notice.text, /确认后才会下载安装/);
  const dev = manualUpdateNotice(info({ check_state: "available", available: true, latest: "0.2.0", can_apply: false }));
  assert.equal(dev.confirmInstall, false);
});

test("the current version is only claimed after a successful check", () => {
  const ready = manualUpdateNotice(info({ check_state: "current", latest: "0.1.2" }));
  assert.equal(ready.level, "success");
  assert.match(ready.text, /已是最新版本 0\.1\.2/);
  assert.equal(ready.confirmInstall, false);
  const untouched = manualUpdateNotice(info({ check_state: "idle" }));
  assert.equal(untouched.text, "");
  assert.equal(untouched.confirmInstall, false);
});

test("an update already in progress is not started again", () => {
  for (const phase of ["downloading", "installing"]) {
    const notice = manualUpdateNotice(info({ phase, check_state: "busy", available: true, latest: "0.2.0" }));
    assert.equal(notice.confirmInstall, false);
    assert.match(notice.text, /不会再次下载或安装/);
  }
});
