"use strict";

const assert = require("assert");
const path = require("path");
const polling = require(path.resolve(process.argv[2]));

function timers() {
  const scheduled = [];
  return {
    scheduled,
    setTimeoutFn(callback, delay) {
      const timer = {callback, delay, cancelled: false};
      scheduled.push(timer);
      return timer;
    },
    clearTimeoutFn(timer) {
      timer.cancelled = true;
    },
    current() {
      return [...scheduled].reverse().find((timer) => !timer.cancelled);
    },
  };
}

async function run() {
  assert.strictEqual(polling.hasLiveWork({items: [{state: "queued"}]}), true);
  assert.strictEqual(polling.hasLiveWork({items: [{state: "running"}]}), true);
  assert.strictEqual(polling.hasLiveWork({items: [{state: "verifying"}]}), true);
  assert.strictEqual(polling.hasLiveWork({items: [{state: "complete"}]}), false);

  const document = {hidden: false};
  let view = "library";
  const statuses = [
    {items: [{state: "complete"}]},
    {items: [{state: "queued"}]},
    {items: [{state: "running"}]},
  ];
  let loads = 0;
  const clock = timers();
  const poller = new polling.AdaptiveDownloadPoller({
    document,
    load: async () => statuses[loads++],
    isDownloadsView: () => view === "downloads",
    setTimeoutFn: clock.setTimeoutFn,
    clearTimeoutFn: clock.clearTimeoutFn,
  });

  // Inactive view: no fetch and no timer.
  await poller.viewChanged({immediate: true});
  assert.strictEqual(loads, 0);
  assert.strictEqual(clock.current(), undefined);

  // Opening Downloads refreshes immediately; terminal-only history idles.
  view = "downloads";
  await poller.viewChanged({immediate: true});
  assert.strictEqual(loads, 1);
  assert.strictEqual(clock.current().delay, polling.IDLE_DELAY_MS);

  // A queued item selects the rapid cadence.
  await poller.refreshNow();
  assert.strictEqual(loads, 2);
  assert.strictEqual(clock.current().delay, polling.ACTIVE_DELAY_MS);

  // Hidden pages stop completely; becoming visible refreshes immediately.
  document.hidden = true;
  await poller.visibilityChanged();
  assert.strictEqual(clock.current(), undefined);
  document.hidden = false;
  await poller.visibilityChanged();
  assert.strictEqual(loads, 3);
  assert.strictEqual(clock.current().delay, polling.ACTIVE_DELAY_MS);

  // Leaving Downloads cancels this tab's timer.
  view = "diagnostics";
  await poller.viewChanged();
  assert.strictEqual(clock.current(), undefined);

  // Each tab owns independent document visibility and timer state.
  const firstClock = timers();
  const secondClock = timers();
  const firstDocument = {hidden: false};
  const secondDocument = {hidden: false};
  const makeTab = (tabDocument, tabClock) => new polling.AdaptiveDownloadPoller({
    document: tabDocument,
    load: async () => ({items: [{state: "queued"}]}),
    isDownloadsView: () => true,
    setTimeoutFn: tabClock.setTimeoutFn,
    clearTimeoutFn: tabClock.clearTimeoutFn,
  });
  const first = makeTab(firstDocument, firstClock);
  const second = makeTab(secondDocument, secondClock);
  await first.refreshNow();
  await second.refreshNow();
  firstDocument.hidden = true;
  await first.visibilityChanged();
  assert.strictEqual(firstClock.current(), undefined);
  assert.strictEqual(secondClock.current().delay, polling.ACTIVE_DELAY_MS);

  console.log("download polling tests passed");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
