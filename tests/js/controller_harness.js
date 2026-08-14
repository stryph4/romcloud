(function () {
  "use strict";
  try {
    const {FocusModel, RepeatButton} = window.ROMCloudController;
    const model = new FocusModel(["systems", "tabs", "games", "dialog"]);
    model.setLayout({systems: [1, 1], tabs: [2], games: [3, 2]});
    if (model.moveVertical(1).row !== 1) throw new Error("system step failed");
    if (model.moveVertical(1).zone !== "tabs") throw new Error("zone step failed");
    if (model.moveHorizontal(1).col !== 1) throw new Error("horizontal step failed");
    if (model.moveVertical(1).zone !== "games") throw new Error("game entry failed");
    if (model.moveVertical(1).row !== 1) throw new Error("game row step failed");
    if (model.current.col !== 1) throw new Error("game column retention failed");

    model.zoneOrder = ["dialog"];
    model.current = null;
    model.setLayout({dialog: [2]});
    if (model.current.zone !== "dialog" || model.moveHorizontal(1).col !== 1) {
      throw new Error("dialog focus trap failed");
    }

    const bumper = new RepeatButton({initialDelay: 420, accelerated: true});
    if (bumper.update(true, 0) !== 1 || bumper.update(true, 200) !== 0) throw new Error("initial repeat failed");
    if (bumper.update(true, 1700) !== 2 || bumper.update(true, 3600) !== 5) throw new Error("acceleration failed");
    if (bumper.update(false, 3601) !== 0 || bumper.held) throw new Error("release did not stop repeat");

    document.body.dataset.result = "passed";
    document.body.textContent = "controller state tests passed";
  } catch (error) {
    document.body.dataset.result = "failed";
    document.body.textContent = error.stack || error.message;
  }
})();
