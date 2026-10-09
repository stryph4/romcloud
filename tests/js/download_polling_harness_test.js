"use strict";

(async () => {
  const polling = window.ROMCloudDownloadPolling;
  const assert = (condition, message) => {
    if (!condition) throw new Error(message);
  };
  const makeClock = () => {
    const scheduled = [];
    return {
      setTimeoutFn(callback, delay) {
        const timer = {callback, delay, cancelled: false};
        scheduled.push(timer);
        return timer;
      },
      clearTimeoutFn(timer) { timer.cancelled = true; },
      current() { return [...scheduled].reverse().find((timer) => !timer.cancelled); },
    };
  };

  const documentState = {hidden: false};
  let view = "library";
  let loads = 0;
  const statuses = [
    {items: [{state: "complete"}]},
    {items: [{state: "queued"}]},
    {items: [{state: "running"}]},
  ];
  const clock = makeClock();
  const poller = new polling.AdaptiveDownloadPoller({
    document: documentState,
    load: async () => statuses[loads++],
    isDownloadsView: () => view === "downloads",
    setTimeoutFn: clock.setTimeoutFn,
    clearTimeoutFn: clock.clearTimeoutFn,
  });

  await poller.viewChanged({immediate: true});
  assert(loads === 0 && clock.current() === undefined, "inactive view polled");

  view = "downloads";
  await poller.viewChanged({immediate: true});
  assert(loads === 1, "opening Downloads did not refresh");
  assert(clock.current().delay === polling.IDLE_DELAY_MS, "terminal history was not idle");

  await poller.refreshNow();
  assert(clock.current().delay === polling.ACTIVE_DELAY_MS, "queued work was not rapid");

  documentState.hidden = true;
  await poller.visibilityChanged();
  assert(clock.current() === undefined, "hidden page retained timer");

  documentState.hidden = false;
  await poller.visibilityChanged();
  assert(loads === 3, "visible page did not refresh immediately");

  view = "library";
  await poller.viewChanged();
  assert(clock.current() === undefined, "leaving Downloads retained timer");

  document.body.dataset.result = "passed";
})().catch((error) => {
  document.body.dataset.result = `failed: ${error.message}`;
});
