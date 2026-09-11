"use strict";

const assert = require("assert");
const path = require("path");
const controllerPath = path.resolve(process.argv[2]);
const controller = require(controllerPath);
const spatial = require(path.join(path.dirname(controllerPath), "spatial_navigation.js"));

const mapper = new controller.StandardGamepadMapper();
const buttons = Array.from({length: 16}, () => ({pressed: false, value: 0}));
const standardPad = {connected: true, mapping: "standard", buttons, axes: [0, 0]};
buttons[0] = {pressed: true, value: 1};
buttons[4] = {pressed: true, value: 1};
buttons[9] = {pressed: true, value: 1};
buttons[15] = {pressed: true, value: 1};
let logical = mapper.pressedState(standardPad);
assert.strictEqual(logical[controller.LOGICAL_ACTIONS.CONFIRM], true);
assert.strictEqual(logical[controller.LOGICAL_ACTIONS.PREVIOUS_PAGE], true);
assert.strictEqual(logical[controller.LOGICAL_ACTIONS.MENU], true);
assert.strictEqual(logical[controller.LOGICAL_ACTIONS.RIGHT], true);
standardPad.axes[1] = -0.8;
logical = mapper.pressedState(standardPad);
assert.strictEqual(logical[controller.LOGICAL_ACTIONS.UP], true);

const rawPad = {...standardPad, mapping: ""};
assert.strictEqual(mapper.supports(rawPad), true);
assert.strictEqual(mapper.pressedState(rawPad)[controller.LOGICAL_ACTIONS.CONFIRM], true);
const unknownPad = {connected: true, mapping: "", buttons: [], axes: []};
assert.strictEqual(mapper.supports(unknownPad), false);
assert.ok(Object.values(mapper.pressedState(unknownPad)).every((pressed) => pressed === false));

const diagnosticRequests = [];
let diagnosticTimer = null;
const diagnosticWindow = {
  isSecureContext: true,
  navigator: {getGamepads: () => [rawPad]},
  addEventListener: () => {},
  setTimeout: (callback) => { diagnosticTimer = callback; return 1; },
  clearTimeout: () => { diagnosticTimer = null; },
  fetch: (endpoint, options) => { diagnosticRequests.push({endpoint, options}); return Promise.resolve(); },
};
const diagnostics = new controller.BrowserControllerDiagnostics(
  diagnosticWindow, "/api/controller-diagnostics", {interactionMode: "controller"}
);
diagnostics.initialize([rawPad]);
buttons[0] = {pressed: false, value: 0};
diagnostics.observe([rawPad]);
diagnostics.focus({zone: "games", row: 2, col: 1}, {id: "pin", tagName: "BUTTON"});
diagnostics.logical({up: false, confirm: true}, {zone: "games", row: 2, col: 1});
diagnostics.flush();
assert.strictEqual(diagnosticRequests.length, 1);
const diagnosticBody = JSON.parse(diagnosticRequests[0].options.body);
assert.ok(diagnosticBody.events.some((event) => event.event === "controller-initialized"));
assert.ok(diagnosticBody.events.some((event) =>
  event.event === "controller-boundary" && event.detail.state === "nonstandard-gamepad-exposed"
));
assert.ok(diagnosticBody.events.some((event) => event.event === "gamepad-snapshot"));
assert.ok(diagnosticBody.events.some((event) =>
  event.event === "gamepad-snapshot" && event.detail.mapping_supported === false
));
assert.ok(diagnosticBody.events.some((event) => event.event === "gamepad-input-change"));
assert.ok(diagnosticBody.events.some((event) => event.event === "focus-change"));
assert.ok(diagnosticBody.events.some((event) => event.event === "logical-input-change"));
assert.strictEqual(diagnosticTimer, null);

const model = new controller.FocusModel([
  "systems", "primary", "tabs", "controls", "games", "pager", "dialog",
]);
model.setLayout({
  systems: [1, 1, 1],
  primary: [1],
  tabs: [2],
  controls: [3],
  games: [3, 2, 1],
  pager: [3],
});
assert.deepStrictEqual(model.current, {zone: "systems", row: 0, col: 0});
assert.deepStrictEqual(model.moveVertical(1), {zone: "systems", row: 1, col: 0});
assert.deepStrictEqual(model.moveHorizontal(1), {zone: "primary", row: 0, col: 0});
model.set({zone: "systems", row: 1, col: 0});
assert.deepStrictEqual(model.moveVertical(1), {zone: "primary", row: 0, col: 0});
assert.deepStrictEqual(model.moveVertical(1), {zone: "tabs", row: 0, col: 0});
assert.deepStrictEqual(model.moveHorizontal(1), {zone: "tabs", row: 0, col: 1});
assert.deepStrictEqual(model.moveVertical(1), {zone: "controls", row: 0, col: 1});
assert.deepStrictEqual(model.moveVertical(1), {zone: "games", row: 0, col: 1});
assert.deepStrictEqual(model.moveVertical(1), {zone: "games", row: 1, col: 1});
assert.deepStrictEqual(model.moveVertical(1), {zone: "games", row: 2, col: 0});
assert.deepStrictEqual(model.moveVertical(1), {zone: "pager", row: 0, col: 0});

model.set({zone: "games", row: 2, col: 0});
model.setLayout({games: [2]});
assert.deepStrictEqual(model.current, {zone: "games", row: 0, col: 0});
model.zoneOrder = ["dialog"];
model.current = null;
model.setLayout({dialog: [2]});
assert.deepStrictEqual(model.current, {zone: "dialog", row: 0, col: 0});
assert.deepStrictEqual(model.moveHorizontal(1), {zone: "dialog", row: 0, col: 1});
assert.deepStrictEqual(model.moveVertical(1), {zone: "dialog", row: 0, col: 1});

const bumper = new controller.RepeatButton({initialDelay: 420, accelerated: true});
assert.strictEqual(bumper.update(true, 0), 1);
assert.strictEqual(bumper.update(true, 200), 0);
assert.strictEqual(bumper.update(true, 421), 1);
assert.strictEqual(bumper.update(true, 1700), 2);
assert.strictEqual(bumper.update(true, 3600), 5);
assert.strictEqual(bumper.update(false, 3601), 0);
assert.strictEqual(bumper.held, false);
assert.strictEqual(bumper.update(false, 9000), 0);
assert.strictEqual(bumper.update(true, 9001), 1);

const keyboard = new controller.ControllerKeyboardModel("AB");
keyboard.moveCursor(-1);
assert.strictEqual(keyboard.insert("X"), "AXB");
assert.strictEqual(keyboard.backspace(), "AB");
assert.strictEqual(keyboard.deleteForward(), "A");
assert.strictEqual(keyboard.cancel(), "AB");
assert.strictEqual(keyboard.cursor, 2);

model.zoneOrder = ["dialog"];
model.current = null;
model.setLayout({dialog: [3, 2]});
assert.deepStrictEqual(model.current, {zone: "dialog", row: 0, col: 0});
assert.deepStrictEqual(model.moveHorizontal(1), {zone: "dialog", row: 0, col: 1});
assert.deepStrictEqual(model.moveVertical(1), {zone: "dialog", row: 1, col: 1});

function fakeElement(id, left, top, width, height) {
  return {
    id,
    getBoundingClientRect: () => ({
      left, top, width, height,
      right: left + width,
      bottom: top + height,
    }),
  };
}

const search = fakeElement("search", 300, 150, 300, 40);
const system = fakeElement("system", 20, 150, 180, 40);
const header = fakeElement("header", 160, 20, 120, 40);
const tab = fakeElement("tab", 300, 80, 180, 40);
const state = fakeElement("state", 620, 150, 160, 40);
const spatialEntries = new Map([
  ["controls:0:0", search],
  ["systems:1:0", system],
  ["global:0:0", header],
  ["tabs:0:0", tab],
  ["controls:0:1", state],
]);
let spatialTarget = spatial.chooseSpatialTarget(search, spatialEntries, "left");
assert.strictEqual(spatialTarget.element, system, "left should prefer the aligned sidebar control");
assert.deepStrictEqual(spatialTarget.descriptor, {zone: "systems", row: 1, col: 0});
spatialTarget = spatial.chooseSpatialTarget(search, spatialEntries, "up");
assert.strictEqual(spatialTarget.element, tab, "up should prefer the control physically above");
spatialTarget = spatial.chooseSpatialTarget(search, spatialEntries, "right");
assert.strictEqual(spatialTarget.element, state, "right should prefer the adjacent toolbar control");
assert.strictEqual(spatial.chooseSpatialTarget(search, new Map([["controls:0:0", search]]), "left"), null);

console.log("controller state tests passed");
