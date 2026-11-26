import assert from "node:assert/strict";
import test from "node:test";

import { updateNotice, updateVisible, type UpdateStatus } from "./src/updateMenu.ts";

function status(partial: Partial<UpdateStatus>): UpdateStatus {
  return {
    current: "0.1.5",
    latest: "",
    available: false,
    phase: "idle",
    check_state: "idle",
    ...partial,
  };
}

test("a failed check is not reported as up to date", () => {
  const notice = updateNotice(status({ check_state: "failed" }));
  assert.equal(notice?.level, "error");
  assert.match(notice?.text ?? "", /不能确定是不是最新版本/);
  assert.equal((notice?.text ?? "").includes("已是最新"), false);
});

test("a failed recheck keeps the version found earlier", () => {
  const notice = updateNotice(status({ check_state: "failed", available: true, latest: "0.1.6" }));
  assert.equal(notice?.level, "warning");
  assert.match(notice?.text ?? "", /0\.1\.6/);
});

test("the current version is only claimed after a successful check", () => {
  const notice = updateNotice(status({ check_state: "current", latest: "0.1.5" }));
  assert.equal(notice?.level, "success");
  assert.match(notice?.text ?? "", /已是最新版本 0\.1\.5/);
  assert.equal(updateNotice(status({ check_state: "idle" })), null);
});

test("a newer version is announced without installing", () => {
  const notice = updateNotice(status({ check_state: "available", available: true, latest: "0.1.6" }));
  assert.equal(notice?.level, "info");
  assert.match(notice?.text ?? "", /0\.1\.6/);
});

test("the banner only shows when there is something to say", () => {
  assert.equal(updateVisible(null), false);
  assert.equal(updateVisible(status({ check_state: "current" })), false);
  assert.equal(updateVisible(status({ check_state: "available", available: true, latest: "0.1.6" })), true);
  assert.equal(updateVisible(status({ phase: "downloading" })), true);
  assert.equal(updateVisible(status({ phase: "error" })), true);
});

test("a running update is not started again", () => {
  for (const phase of ["downloading", "installing"]) {
    const notice = updateNotice(status({ phase, check_state: "busy", available: true, latest: "0.1.6" }));
    assert.equal(notice?.level, "info");
    assert.match(notice?.text ?? "", /不会再次下载或安装/);
  }
});
