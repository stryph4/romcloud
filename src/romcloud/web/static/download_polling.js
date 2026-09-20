(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ROMCloudDownloadPolling = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const ACTIVE_DELAY_MS = 1000;
  const IDLE_DELAY_MS = 15000;
  const LIVE_STATES = new Set(["queued", "running", "verifying"]);

  function hasLiveWork(status) {
    return Boolean(
      status && Array.isArray(status.items)
      && status.items.some((item) => LIVE_STATES.has(item.state))
    );
  }

  class AdaptiveDownloadPoller {
    constructor({
      document,
      load,
      isDownloadsView,
      setTimeoutFn = setTimeout,
      clearTimeoutFn = clearTimeout,
      activeDelayMs = ACTIVE_DELAY_MS,
      idleDelayMs = IDLE_DELAY_MS,
    }) {
      this.document = document;
      this.load = load;
      this.isDownloadsView = isDownloadsView;
      this.setTimeoutFn = setTimeoutFn;
      this.clearTimeoutFn = clearTimeoutFn;
      this.activeDelayMs = activeDelayMs;
      this.idleDelayMs = idleDelayMs;
      this.liveWork = false;
      this.timer = null;
      this.inFlight = null;
      this.refreshRequested = false;
    }

    eligible() {
      return !this.document.hidden && this.isDownloadsView();
    }

    stop() {
      if (this.timer !== null) this.clearTimeoutFn(this.timer);
      this.timer = null;
    }

    schedule() {
      this.stop();
      if (!this.eligible()) return;
      const delay = this.liveWork ? this.activeDelayMs : this.idleDelayMs;
      this.timer = this.setTimeoutFn(() => {
        this.timer = null;
        void this.refreshNow();
      }, delay);
    }

    refreshNow() {
      this.stop();
      if (!this.eligible()) return Promise.resolve(null);
      if (this.inFlight !== null) {
        this.refreshRequested = true;
        return this.inFlight;
      }
      this.inFlight = Promise.resolve()
        .then(() => this.load())
        .then((status) => {
          if (status !== null && status !== undefined) {
            this.liveWork = hasLiveWork(status);
          }
          return status;
        })
        .finally(() => {
          this.inFlight = null;
          if (!this.eligible()) {
            this.refreshRequested = false;
            this.stop();
          } else if (this.refreshRequested) {
            this.refreshRequested = false;
            void this.refreshNow();
          } else {
            this.schedule();
          }
        });
      return this.inFlight;
    }

    viewChanged({immediate = false} = {}) {
      if (!this.eligible()) {
        this.refreshRequested = false;
        this.stop();
        return Promise.resolve(null);
      }
      if (immediate) return this.refreshNow();
      this.schedule();
      return Promise.resolve(null);
    }

    visibilityChanged() {
      return this.viewChanged({immediate: !this.document.hidden});
    }
  }

  return {ACTIVE_DELAY_MS, IDLE_DELAY_MS, hasLiveWork, AdaptiveDownloadPoller};
});
