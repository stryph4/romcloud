(async function () {
  "use strict";
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const calls = [];
  const operation = {
    operation_id: "op-one", timestamp_utc: "2026-01-01T00:00:00+00:00",
    name: "Auto Quick Sync", subsystem: "savesync", status: "success",
    duration_ms: 12, generation_before: 4, generation_after: 5,
    examined: 1, uploaded: 1, downloaded: 0, conflicts: 0, unchanged: 0, repairs: 0,
  };
  const api = async (url) => {
    calls.push(url);
    const params = new URL(url, location.href).searchParams;
    if (params.get("detail")) return {
      events: [{timestamp_utc: operation.timestamp_utc, level: "INFO", event_code: "reconciliation.decision", message: "upload", metadata: {group_id: "game/save", layout_id: "retroarch", decision: "upload", reason: "local changed"}}],
      has_more: false, facets: {subsystems: ["savesync"], levels: ["INFO"]},
    };
    return {operations: [operation], has_more: true, facets: {subsystems: ["savesync"], levels: ["INFO"]}};
  };
  try {
    let exited = false;
    const browser = new window.ROMCloudDiagnostics.DiagnosticsBrowser({
      api,
      contentUpdated: () => window.dispatchEvent(new CustomEvent("romcloud:content-updated")),
      exitLocal: () => { exited = true; },
    });
    window.addEventListener("romcloud:controller-back", (event) => { event.preventDefault(); browser.back(); });
    window.addEventListener("romcloud:page-jump", (event) => browser.pageJump(event.detail.delta));
    await browser.open();
    const navigator = window.romcloudGamepad = window.ROMCloudController.startBrowserController();
    window.testTick(-10);
    navigator.focusZone("diagnostic-list");
    assert(document.activeElement.textContent.includes("Auto Quick Sync"), "operation row did not receive shared focus");

    window.testButton(0, true); window.testTick(0);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert(!document.getElementById("diagnostics-detail-view").classList.contains("hidden"), "confirm did not open detail");
    assert(document.activeElement.id === "diag-detail-back", "detail focus was not established");
    window.testButton(0, false); window.testTick(16);

    window.testButton(1, true); window.testTick(32);
    assert(!document.getElementById("diagnostics-list-view").classList.contains("hidden"), "back did not return to results");
    assert(document.activeElement.textContent.includes("Auto Quick Sync"), "back did not restore operation focus");
    window.testButton(1, false); window.testTick(48);

    window.testButton(5, true); window.testTick(64);
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert(calls.some((url) => new URL(url, location.href).searchParams.get("page") === "2"), "bumper paging did not use shared action");
    window.testButton(5, false); window.testTick(80);
    assert(!exited, "child back leaked into top-level exit");
    navigator._disconnect(window.testPad);

    // A remote (non-controller) session has no exitLocal: top-level back must
    // return to the Library view instead of requesting an Open Here exit.
    const remote = new window.ROMCloudDiagnostics.DiagnosticsBrowser({
      api,
      contentUpdated: () => window.dispatchEvent(new CustomEvent("romcloud:content-updated")),
      exitLocal: null,
    });
    await remote.open();
    assert(remote.active && !document.getElementById("diagnostics-main").classList.contains("hidden"), "remote diagnostics did not open");
    remote.back();
    assert(!remote.active, "remote back did not leave diagnostics");
    assert(document.getElementById("diagnostics-main").classList.contains("hidden"), "remote back did not hide diagnostics");
    assert(!document.getElementById("library-main").classList.contains("hidden"), "remote back did not restore library");
    assert(document.getElementById("app-section-title").textContent === "Library Manager", "remote back did not restore section title");
    document.body.dataset.result = "passed";
    document.body.textContent = "diagnostics browser controller navigation passed";
  } catch (error) {
    document.body.dataset.result = "failed";
    document.body.textContent = error.stack || error.message;
  }
})();
