(function () {
  "use strict";
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const gamepadEvent = (name) => {
    const event = new Event(name);
    Object.defineProperty(event, "gamepad", {value: window.testPad});
    return event;
  };
  try {
    let activated = 0;
    let pageDelta = 0;
    document.getElementById("system-1").addEventListener("click", () => { activated += 1; });
    window.addEventListener("romcloud:page-jump", (event) => { pageDelta += event.detail.delta; });
    const navigator = window.ROMCloudController.startBrowserController();
    assert(document.activeElement.id === "system-0", "connect did not establish deterministic focus");

    window.testButton(13, true); window.testTick(0);
    assert(document.activeElement.id === "system-1", "D-pad down did not move one system");
    window.testButton(13, false); window.testTick(16);
    window.testTick(300);
    assert(document.activeElement.id === "system-1", "released D-pad continued navigating");

    window.testButton(0, true); window.testTick(320);
    window.testButton(0, false); window.testTick(336);
    assert(activated === 1, "South button did not activate focused control");

    window.testButton(5, true); window.testTick(400);
    assert(pageDelta === 1, "RB initial page jump failed");
    window.testTick(2100);
    assert(pageDelta === 3, "RB sustained acceleration failed");
    window.testButton(5, false); window.testTick(2110);
    window.testTick(5000);
    assert(pageDelta === 3, "RB navigation continued after release");

    const dialog = document.getElementById("dialog");
    dialog.showModal();
    window.dispatchEvent(new CustomEvent("romcloud:content-updated"));
    assert(document.activeElement.id === "close", "dialog did not trap controller focus");
    window.testButton(15, true); window.testTick(5020);
    window.testButton(15, false); window.testTick(5030);
    assert(document.activeElement.id === "confirm", "dialog focus escaped or failed to move");
    window.testButton(1, true); window.testTick(5040);
    window.testButton(1, false); window.testTick(5050);
    assert(!dialog.open, "East button did not close dialog");
    assert(document.activeElement.id === "system-1", "dialog close did not restore prior focus");

    window.dispatchEvent(gamepadEvent("gamepaddisconnected"));
    assert(!document.body.classList.contains("controller-active"), "disconnect left controller mode active");
    window.dispatchEvent(gamepadEvent("gamepadconnected"));
    assert(Boolean(document.activeElement.dataset.controllerZone), `reconnect stranded focus on ${document.activeElement.id || document.activeElement.tagName}`);
    assert(document.activeElement.classList.contains("controller-focus"), "reconnect did not visibly restore focus");
    assert(document.body.classList.contains("controller-active"), "reconnect did not restore controller mode");
    navigator._disconnect(window.testPad);

    document.body.dataset.result = "passed";
    document.body.textContent = "browser controller navigation passed";
  } catch (error) {
    document.body.dataset.result = "failed";
    document.body.textContent = error.stack || error.message;
  }
})();
