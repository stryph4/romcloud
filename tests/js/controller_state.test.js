"use strict";

const assert = require("assert");
const path = require("path");
const controller = require(path.resolve(process.argv[2]));

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
assert.strictEqual(mapper.supports(rawPad), false);
assert.ok(Object.values(mapper.pressedState(rawPad)).every((pressed) => pressed === false));

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
assert.deepStrictEqual(model.moveHorizontal(1), {zone: "systems", row: 2, col: 0});
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

console.log("controller state tests passed");
