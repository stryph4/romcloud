(function () {
  "use strict";
  const buttons = Array.from({length: 16}, () => ({pressed: false, value: 0}));
  window.testPad = {index: 0, connected: true, mapping: "standard", axes: [0, 0], buttons};
  Object.defineProperty(navigator, "getGamepads", {value: () => [window.testPad], configurable: true});
  let scheduled = null;
  let frameId = 0;
  window.requestAnimationFrame = (callback) => { scheduled = callback; frameId += 1; return frameId; };
  window.cancelAnimationFrame = () => { scheduled = null; };
  window.testTick = (now) => { const callback = scheduled; scheduled = null; if (!callback) throw new Error("no animation frame scheduled"); callback(now); };
  window.testButton = (index, pressed) => { buttons[index].pressed = pressed; buttons[index].value = pressed ? 1 : 0; };
})();
