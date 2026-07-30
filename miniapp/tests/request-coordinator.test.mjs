import assert from "node:assert/strict";
import test from "node:test";

import { LatestRequestCoordinator } from "../lib/request-coordinator.js";

test("a stale request cannot finalize the current operation", () => {
  const coordinator = new LatestRequestCoordinator();
  const first = coordinator.begin({ searchId: "FirstSearchToken1" });
  const second = coordinator.begin({ searchId: "SecondSearchToken" });
  let loading = true;

  assert.equal(coordinator.isCurrent(first), false);
  assert.equal(
    coordinator.complete(first, () => {
      loading = false;
    }),
    false,
  );
  assert.equal(loading, true);

  assert.equal(coordinator.isCurrent(second), true);
  assert.equal(
    coordinator.complete(second, () => {
      loading = false;
    }),
    true,
  );
  assert.equal(loading, false);
});

test("invalidation prevents an outstanding request from committing", () => {
  const coordinator = new LatestRequestCoordinator();
  const request = coordinator.begin({ session: 1 });
  let committed = false;

  coordinator.invalidate();

  assert.equal(coordinator.isCurrent(request), false);
  assert.equal(
    coordinator.complete(request, () => {
      committed = true;
    }),
    false,
  );
  assert.equal(committed, false);
});
