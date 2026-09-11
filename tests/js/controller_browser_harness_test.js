(function () {
  "use strict";
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const gamepadEvent = (name) => {
    const event = new Event(name);
    Object.defineProperty(event, "gamepad", {value: window.testPad});
    return event;
  };
  const setRect = (id, left, top, width, height) => {
    const element = document.getElementById(id);
    element.getBoundingClientRect = () => ({
      left, top, width, height,
      right: left + width,
      bottom: top + height,
      x: left,
      y: top,
      toJSON: () => ({}),
    });
  };
  try {
    let activated = 0;
    let pageDelta = 0;
    setRect("system-0", 20, 100, 180, 40);
    setRect("system-1", 20, 150, 180, 40);
    setRect("tab-0", 300, 80, 180, 40);
    setRect("search", 300, 150, 300, 40);
    setRect("game-0", 300, 230, 500, 60);
    setRect("close", 300, 150, 100, 40);
    setRect("confirm", 420, 150, 100, 40);

    document.getElementById("system-1").addEventListener("click", () => { activated += 1; });
    window.addEventListener("romcloud:page-jump", (event) => { pageDelta += event.detail.delta; });
    window.testButton(0, true);
    const navigator = window.ROMCloudController.startBrowserController();
    assert(document.activeElement.id === "system-0", "connect did not establish deterministic focus");
    window.testTick(-20);
    assert(activated === 0, "held launch confirm double-triggered in browser");
    window.testButton(0, false); window.testTick(-10);

    window.testButton(13, true); window.testTick(0);
    assert(document.activeElement.id === "system-1", "D-pad down did not move one system");
    window.testButton(13, false); window.testTick(16);
    window.testTick(300);
    assert(document.activeElement.id === "system-1", "released D-pad continued navigating");

    window.testButton(0, true); window.testTick(320);
    window.testButton(0, false); window.testTick(336);
    assert(activated === 1, "South button did not activate focused control");

    window.testButton(15, true); window.testTick(350);
    window.testButton(15, false); window.testTick(366);
    assert(document.activeElement.id === "search", "D-pad right did not choose the spatially adjacent content control");
    window.testButton(14, true); window.testTick(380);
    window.testButton(14, false); window.testTick(396);
    assert(document.activeElement.id === "system-1", "D-pad left did not return to the spatially adjacent system");

    window.testButton(5, true); window.testTick(420);
    assert(pageDelta === 1, "RB initial page jump failed");
    window.testTick(2120);
    assert(pageDelta === 3, "RB sustained acceleration failed");
    window.testButton(5, false); window.testTick(2130);
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
