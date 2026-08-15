(function (root) {
  "use strict";

  const DEFAULT_ZONES = [
    "auth", "systems", "primary", "tabs", "controls", "bulk",
    "select", "games", "pager", "global", "dialog",
  ];

  // This vocabulary mirrors ports_gfx.actions.Action. It is the stable
  // ROMCloud contract above both input backends; raw SDL and Gamepad API
  // indices must be translated here/below, never consumed by navigation.
  const LOGICAL_ACTIONS = Object.freeze({
    UP: "up", DOWN: "down", LEFT: "left", RIGHT: "right",
    CONFIRM: "confirm", BACK: "back", PREVIOUS_PAGE: "previous_page",
    NEXT_PAGE: "next_page", MENU: "menu",
  });

  // W3C standard Gamepad layout slots. These are deliberately not SDL raw
  // joystick indices. A pad with mapping === "" has implementation-specific
  // ordering and is not guessed at; the native persisted raw mapping cannot
  // safely translate it (and may describe a different, host-side controller).
  const STANDARD_GAMEPAD_BINDINGS = Object.freeze({
    [LOGICAL_ACTIONS.CONFIRM]: Object.freeze({button: 0}),
    [LOGICAL_ACTIONS.BACK]: Object.freeze({button: 1}),
    [LOGICAL_ACTIONS.PREVIOUS_PAGE]: Object.freeze({button: 4}),
    [LOGICAL_ACTIONS.NEXT_PAGE]: Object.freeze({button: 5}),
    [LOGICAL_ACTIONS.MENU]: Object.freeze({button: 9}),
    [LOGICAL_ACTIONS.UP]: Object.freeze({button: 12, axis: 1, direction: -1}),
    [LOGICAL_ACTIONS.DOWN]: Object.freeze({button: 13, axis: 1, direction: 1}),
    [LOGICAL_ACTIONS.LEFT]: Object.freeze({button: 14, axis: 0, direction: -1}),
    [LOGICAL_ACTIONS.RIGHT]: Object.freeze({button: 15, axis: 0, direction: 1}),
  });

  class StandardGamepadMapper {
    constructor(bindings = STANDARD_GAMEPAD_BINDINGS, deadzone = 0.58) {
      this.bindings = bindings;
      this.deadzone = deadzone;
    }

    supports(pad) {
      return Boolean(pad && pad.connected && pad.mapping === "standard");
    }

    pressedState(pad) {
      const result = {};
      Object.values(LOGICAL_ACTIONS).forEach((action) => { result[action] = false; });
      if (!this.supports(pad)) return result;
      const buttonPressed = (index) => Boolean(
        pad.buttons[index] && (pad.buttons[index].pressed || pad.buttons[index].value > 0.5)
      );
      Object.entries(this.bindings).forEach(([action, binding]) => {
        let pressed = buttonPressed(binding.button);
        if (binding.axis !== undefined) {
          const value = Number(pad.axes[binding.axis] || 0);
          pressed = pressed || value * binding.direction > this.deadzone;
        }
        result[action] = pressed;
      });
      return result;
    }
  }

  class FocusModel {
    constructor(zoneOrder = DEFAULT_ZONES) {
      this.zoneOrder = [...zoneOrder];
      this.layout = {};
      this.current = null;
    }

    setLayout(layout) {
      this.layout = layout || {};
      if (this.current) this.current = this._clamp(this.current);
      if (!this.current) this.current = this.first();
      return this.current;
    }

    first() {
      const zone = this.zoneOrder.find((name) => this._rows(name).length);
      return zone ? {zone, row: 0, col: 0} : null;
    }

    set(descriptor) {
      const next = this._clamp(descriptor);
      if (next) this.current = next;
      return this.current;
    }

    moveHorizontal(delta) {
      if (!this.current || !delta) return this.current;
      const {zone, row, col} = this.current;
      const rows = this._rows(zone);
      if (zone === "systems") {
        return this.set({zone, row: row + Math.sign(delta), col: 0});
      }
      return this.set({zone, row, col: col + Math.sign(delta)});
    }

    moveVertical(delta) {
      if (!this.current || !delta) return this.current;
      const sign = Math.sign(delta);
      const {zone, row, col} = this.current;
      const rows = this._rows(zone);
      if (rows[row + sign]) {
        return this.set({zone, row: row + sign, col});
      }
      const zoneIndex = this.zoneOrder.indexOf(zone);
      for (let index = zoneIndex + sign; index >= 0 && index < this.zoneOrder.length; index += sign) {
        const candidateZone = this.zoneOrder[index];
        const candidateRows = this._rows(candidateZone);
        if (!candidateRows.length) continue;
        const candidateRow = sign > 0 ? 0 : candidateRows.length - 1;
        return this.set({zone: candidateZone, row: candidateRow, col});
      }
      return this.current;
    }

    _rows(zone) {
      return Array.isArray(this.layout[zone]) ? this.layout[zone] : [];
    }

    _clamp(descriptor) {
      if (!descriptor || !this._rows(descriptor.zone).length) return null;
      const rows = this._rows(descriptor.zone);
      const row = Math.max(0, Math.min(rows.length - 1, Number(descriptor.row) || 0));
      const columns = Math.max(1, Number(rows[row]) || 1);
      const col = Math.max(0, Math.min(columns - 1, Number(descriptor.col) || 0));
      return {zone: descriptor.zone, row, col};
    }
  }

  class RepeatButton {
    constructor({initialDelay = 380, interval = 115, accelerated = false} = {}) {
      this.initialDelay = initialDelay;
      this.interval = interval;
      this.accelerated = accelerated;
      this.reset();
    }

    update(pressed, now) {
      if (!pressed) { this.reset(); return 0; }
      if (!this.held) {
        this.held = true;
        this.startedAt = now;
        this.nextAt = now + this.initialDelay;
        return 1;
      }
      if (now < this.nextAt) return 0;
      const heldFor = now - this.startedAt;
      const multiplier = this.accelerated ? (heldFor >= 3500 ? 5 : heldFor >= 1600 ? 2 : 1) : 1;
      const cadence = this.accelerated ? (heldFor >= 3500 ? 80 : heldFor >= 1600 ? 130 : 190) : this.interval;
      this.nextAt = now + cadence;
      return multiplier;
    }

    reset() {
      this.held = false;
      this.startedAt = 0;
      this.nextAt = 0;
    }
  }

  class ControllerKeyboardModel {
    constructor(value = "") {
      this.original = String(value);
      this.value = this.original;
      this.cursor = this.value.length;
    }

    insert(text) {
      const addition = String(text);
      this.value = this.value.slice(0, this.cursor) + addition + this.value.slice(this.cursor);
      this.cursor += addition.length;
      return this.value;
    }

    backspace() {
      if (this.cursor > 0) {
        this.value = this.value.slice(0, this.cursor - 1) + this.value.slice(this.cursor);
        this.cursor -= 1;
      }
      return this.value;
    }

    deleteForward() {
      if (this.cursor < this.value.length) {
        this.value = this.value.slice(0, this.cursor) + this.value.slice(this.cursor + 1);
      }
      return this.value;
    }

    moveCursor(delta) {
      this.cursor = Math.max(0, Math.min(this.value.length, this.cursor + Math.sign(delta)));
      return this.cursor;
    }

    cancel() {
      this.value = this.original;
      this.cursor = this.original.length;
      return this.value;
    }
  }

  class BrowserControllerDiagnostics {
    constructor(win, endpoint, {interactionMode = "unknown", maxEvents = 500} = {}) {
      this.window = win;
      this.endpoint = endpoint;
      this.interactionMode = interactionMode;
      this.maxEvents = maxEvents;
      this.eventCount = 0;
      this.queue = [];
      this.flushTimer = null;
      this.padSignatures = new Map();
      this.padLayouts = new Map();
      this.lastLogical = "";
      this.lastFocus = "";
      this.enabled = Boolean(endpoint && win && win.fetch);
      if (this.enabled) {
        this.window.addEventListener("pagehide", () => this.flush(true));
      }
    }

    initialize(pads) {
      const present = pads.filter(Boolean);
      this.record("controller-initialized", {
        interaction_mode: this.interactionMode,
        secure_context: Boolean(this.window.isSecureContext),
        gamepad_api: Boolean(
          this.window.navigator && typeof this.window.navigator.getGamepads === "function"
        ),
        initial_gamepad_count: present.length,
      });
      this.record("controller-boundary", {
        state: !this.window.navigator || typeof this.window.navigator.getGamepads !== "function"
          ? "gamepad-api-unavailable"
          : !present.length
            ? "no-gamepad-exposed"
            : present.some((pad) => pad.connected && pad.mapping === "standard")
              ? "standard-gamepad-exposed"
              : "nonstandard-gamepad-exposed",
      });
      if (!present.length) this.record("gamepad-snapshot", {gamepads: []});
      this.observe(pads);
    }

    connected(pad) {
      this.record("gamepad-connected", this.describe(pad));
      this.record("controller-boundary", {
        state: pad && pad.connected && pad.mapping === "standard"
          ? "standard-gamepad-exposed"
          : "nonstandard-gamepad-exposed",
      });
      this.padSignatures.delete(pad.index);
      this.padLayouts.delete(pad.index);
      this.observe([pad]);
    }

    disconnected(pad) {
      this.record("gamepad-disconnected", this.describe(pad));
      this.padSignatures.delete(pad.index);
      this.padLayouts.delete(pad.index);
    }

    observe(pads) {
      if (!this.enabled) return;
      pads.forEach((pad) => {
        if (!pad) return;
        const description = this.describe(pad);
        const layout = JSON.stringify({
          id: description.id,
          mapping: description.mapping,
          connected: description.connected,
          buttons: description.buttons,
          axes: description.axes,
        });
        if (this.padLayouts.get(pad.index) !== layout) {
          this.padLayouts.set(pad.index, layout);
          this.record("gamepad-snapshot", description);
        }
        const signature = this.inputSignature(pad);
        const previous = this.padSignatures.get(pad.index);
        this.padSignatures.set(pad.index, signature);
        if (previous !== undefined && previous !== signature) {
          this.record("gamepad-input-change", {
            ...this.describe(pad),
            pressed_buttons: pad.buttons
              .map((button, index) => button && (button.pressed || button.value > 0.5) ? index : null)
              .filter((index) => index !== null),
            button_values: pad.buttons.map((button) => Number((button && button.value || 0).toFixed(3))),
            axis_values: pad.axes.map((value) => Number(Number(value || 0).toFixed(3))),
          });
        }
      });
    }

    logical(state, focus) {
      const active = Object.entries(state).filter(([, value]) => value).map(([key]) => key);
      const signature = JSON.stringify(active);
      if (signature === this.lastLogical) return;
      this.lastLogical = signature;
      this.record("logical-input-change", {active, focus: focus || null});
    }

    focus(descriptor, element) {
      const detail = {
        descriptor: descriptor || null,
        element_id: element && element.id || "",
        tag: element && element.tagName || "",
      };
      const signature = JSON.stringify(detail);
      if (signature === this.lastFocus) return;
      this.lastFocus = signature;
      this.record("focus-change", detail);
    }

    failure(error) {
      this.record("controller-initialization-error", {
        name: error && error.name || "Error",
        message: String(error && error.message || error || "unknown error").slice(0, 500),
      });
      this.flush(true);
    }

    describe(pad) {
      return {
        index: Number(pad && pad.index),
        id: String(pad && pad.id || "").slice(0, 300),
        mapping: String(pad && pad.mapping || ""),
        mapping_supported: Boolean(pad && pad.connected && pad.mapping === "standard"),
        connected: Boolean(pad && pad.connected),
        buttons: pad && pad.buttons ? pad.buttons.length : 0,
        axes: pad && pad.axes ? pad.axes.length : 0,
        timestamp: Number(pad && pad.timestamp || 0),
      };
    }

    inputSignature(pad) {
      const buttons = pad.buttons.map((button) => Boolean(
        button && (button.pressed || button.value > 0.5)
      ));
      const axes = pad.axes.map((value) => value < -0.35 ? -1 : value > 0.35 ? 1 : 0);
      return JSON.stringify({buttons, axes});
    }

    record(event, detail = {}) {
      if (!this.enabled || this.eventCount >= this.maxEvents) return;
      this.eventCount += 1;
      this.queue.push({event, detail});
      if (this.queue.length >= 8) this.flush();
      else if (this.flushTimer === null) {
        this.flushTimer = this.window.setTimeout(() => this.flush(), 250);
      }
    }

    flush(keepalive = false) {
      if (!this.enabled || !this.queue.length) return;
      if (this.flushTimer !== null) this.window.clearTimeout(this.flushTimer);
      this.flushTimer = null;
      const events = this.queue.splice(0, 64);
      this.window.fetch(this.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({events}),
        keepalive,
      }).catch(() => {});
    }
  }

  class BrowserGamepadNavigator {
    constructor(win, doc, mapper = new StandardGamepadMapper(), diagnostics = null) {
      this.window = win;
      this.document = doc;
      this.model = new FocusModel();
      this.elements = new Map();
      this.connected = new Set();
      this.mapper = mapper;
      this.diagnostics = diagnostics;
      this.frame = null;
      this.scanTimer = null;
      this.usingController = false;
      this.editing = null;
      this.editingOriginal = null;
      this.lastNonModal = null;
      this.repeaters = {
        up: new RepeatButton(), down: new RepeatButton(),
        left: new RepeatButton(), right: new RepeatButton(),
        lb: new RepeatButton({initialDelay: 420, accelerated: true}),
        rb: new RepeatButton({initialDelay: 420, accelerated: true}),
      };
      this.edges = {confirm: false, back: false, menu: false};
      this._boundPoll = (now) => {
        try {
          this._poll(now);
        } catch (error) {
          this.frame = null;
          if (this.diagnostics) {
            this.diagnostics.record("controller-runtime-error", {
              name: error && error.name || "Error",
              message: String(error && error.message || error || "unknown error").slice(0, 500),
            });
            this.diagnostics.flush(true);
          }
        }
      };
    }

    start() {
      this.window.addEventListener("gamepadconnected", (event) => this._connect(event.gamepad));
      this.window.addEventListener("gamepaddisconnected", (event) => this._disconnect(event.gamepad));
      this.document.addEventListener("focusin", (event) => this._rememberElement(event.target));
      this.document.addEventListener("pointerdown", () => this._setControllerMode(false), true);
      this.document.addEventListener("keydown", () => this._setControllerMode(false), true);
      this.window.addEventListener("romcloud:content-updated", () => this.reconcile());
      this.document.addEventListener("close", (event) => {
        if (event.target && event.target.tagName === "DIALOG") this._restoreAfterDialog();
      }, true);
      const pads = this._gamepads();
      if (this.diagnostics) this.diagnostics.initialize(pads);
      pads.forEach((pad) => { if (pad) this.connected.add(pad.index); });
      if (this._activePad()) {
        this._setControllerMode(true);
        this.reconcile(true);
        this._schedule();
      }
      this._announce();
      if (this.diagnostics && !this.connected.size) this._schedule();
      return this;
    }

    reconcile(forceFocus = false) {
      const dialog = this.document.querySelector("dialog[open]");
      const scope = dialog || this.document;
      const zones = dialog ? ["dialog"] : DEFAULT_ZONES.filter((zone) => zone !== "dialog");
      const layout = {};
      const groups = {};
      this.elements.clear();
      scope.querySelectorAll("[data-controller-zone]").forEach((element) => {
        if (!this._available(element)) return;
        const zone = element.dataset.controllerZone;
        if (!zones.includes(zone)) return;
        const row = Number(element.dataset.controllerRow || 0);
        if (!groups[zone]) groups[zone] = [];
        if (!groups[zone][row]) groups[zone][row] = [];
        groups[zone][row].push(element);
      });
      Object.entries(groups).forEach(([zone, rows]) => {
        layout[zone] = [];
        rows.forEach((rowElements, row) => {
          if (!rowElements) return;
          rowElements.sort((a, b) => Number(a.dataset.controllerCol || 0) - Number(b.dataset.controllerCol || 0));
          layout[zone][row] = rowElements.length;
          rowElements.forEach((element, col) => {
            element.dataset.controllerRuntimeCol = String(col);
            this.elements.set(this._key({zone, row, col}), element);
          });
        });
      });
      this.model.zoneOrder = zones;
      if (dialog && this.model.current && this.model.current.zone !== "dialog") {
        this.lastNonModal = this.model.current;
        this.model.current = null;
      }
      this.model.setLayout(layout);
      if ((forceFocus || this.usingController) && this.model.current) this._focusCurrent();
    }

    focusZone(zone) {
      this.reconcile();
      if (this.model.set({zone, row: 0, col: 0})) this._focusCurrent();
    }

    _connect(gamepad) {
      this.connected.add(gamepad.index);
      if (this.diagnostics) this.diagnostics.connected(gamepad);
      this._resetInputs();
      if (this.mapper.supports(gamepad)) {
        this._setControllerMode(true);
        this.reconcile(true);
      }
      this._announce();
      this._schedule();
    }

    _disconnect(gamepad) {
      this.connected.delete(gamepad.index);
      if (this.diagnostics) this.diagnostics.disconnected(gamepad);
      this._resetInputs();
      if (!this._activePad(gamepad.index)) {
        if (this.frame !== null) this.window.cancelAnimationFrame(this.frame);
        this.frame = null;
        this._setControllerMode(false);
      }
      this._announce();
      if (this.diagnostics) this._schedule();
    }

    _poll(now) {
      this.frame = null;
      const observed = this._gamepads();
      if (this.diagnostics) this.diagnostics.observe(observed);
      const present = new Set(observed.filter(Boolean).map((pad) => pad.index));
      [...this.connected].forEach((index) => {
        if (!present.has(index)) this.connected.delete(index);
      });
      observed.forEach((pad) => { if (pad) this.connected.add(pad.index); });
      const pad = this._activePad();
      if (!pad) { this._resetInputs(); this._schedule(); return; }
      const pressed = this._pressedState(pad);
      if (this.diagnostics) this.diagnostics.logical(pressed, this.model.current);
      const up = this.repeaters.up.update(pressed.up, now);
      const down = this.repeaters.down.update(pressed.down, now);
      const left = this.repeaters.left.update(pressed.left, now);
      const right = this.repeaters.right.update(pressed.right, now);
      const vertical = up ? -1 : (down ? 1 : 0);
      const horizontal = left ? -1 : (right ? 1 : 0);
      const modal = this._dialogOpen();
      const lb = this.repeaters.lb.update(modal ? false : pressed.lb, now);
      const rb = this.repeaters.rb.update(modal ? false : pressed.rb, now);
      if (vertical || horizontal || lb || rb || pressed.confirm || pressed.back || pressed.menu) this._setControllerMode(true);
      if (this.editing) {
        if (vertical) this._changeSelect(vertical);
      } else if (!modal) {
        if (lb) this._pageJump(-lb);
        else if (rb) this._pageJump(rb);
        else if (vertical) this._move("vertical", vertical);
        else if (horizontal) this._move("horizontal", horizontal);
      } else if (vertical) {
        this._move("vertical", vertical);
      } else if (horizontal) {
        this._move("horizontal", horizontal);
      }
      if (pressed.confirm && !this.edges.confirm) this._activate();
      if (pressed.back && !this.edges.back) this._back();
      if (pressed.menu && !this.edges.menu) this._menu();
      this.edges.confirm = pressed.confirm;
      this.edges.back = pressed.back;
      this.edges.menu = pressed.menu;
      this._schedule();
    }

    _move(axis, delta) {
      this.reconcile();
      if (axis === "vertical") this.model.moveVertical(delta);
      else this.model.moveHorizontal(delta);
      this._focusCurrent();
    }

    _activate() {
      const element = this.elements.get(this._key(this.model.current));
      if (!element) return;
      if (element.tagName === "SELECT") {
        if (this.editing === element) this._finishSelect(true);
        else {
          this.editing = element;
          this.editingOriginal = element.value;
          element.classList.add("controller-editing");
        }
        return;
      }
      if (element.tagName === "INPUT" && ["search", "text", "password"].includes(element.type)) {
        element.dispatchEvent(new this.window.CustomEvent("romcloud:controller-text", {
          bubbles: true,
          cancelable: true,
          detail: {element},
        }));
        return;
      }
      if (element.dataset.controllerActivate === "toggle-row") {
        const checkbox = element.querySelector('input[type="checkbox"]');
        if (checkbox) checkbox.click();
        element.setAttribute("aria-checked", checkbox && checkbox.checked ? "true" : "false");
        return;
      }
      element.click();
    }

    _back() {
      if (this.editing) { this._finishSelect(false); return; }
      const dialog = this.document.querySelector("dialog[open]");
      if (dialog) { dialog.close("cancel"); return; }
      const event = new this.window.CustomEvent("romcloud:controller-back", {cancelable: true});
      if (!this.window.dispatchEvent(event)) return;
      this.focusZone("systems");
    }

    _menu() {
      const event = new this.window.CustomEvent("romcloud:controller-menu", {cancelable: true});
      if (!this.window.dispatchEvent(event)) return;
      this.focusZone("systems");
    }

    _changeSelect(delta) {
      const select = this.editing;
      if (!select) return;
      let next = select.selectedIndex + Math.sign(delta);
      while (next >= 0 && next < select.options.length && select.options[next].disabled) next += Math.sign(delta);
      if (next >= 0 && next < select.options.length) select.selectedIndex = next;
    }

    _finishSelect(commit) {
      const select = this.editing;
      if (!select) return;
      if (!commit) select.value = this.editingOriginal;
      select.classList.remove("controller-editing");
      this.editing = null;
      this.editingOriginal = null;
      if (commit) select.dispatchEvent(new this.window.Event("change", {bubbles: true}));
    }

    _pageJump(delta) {
      this.window.dispatchEvent(new this.window.CustomEvent("romcloud:page-jump", {detail: {delta}}));
    }

    _focusCurrent() {
      this.document.querySelectorAll(".controller-focus").forEach((element) => element.classList.remove("controller-focus"));
      const element = this.elements.get(this._key(this.model.current));
      if (!element) return;
      element.classList.add("controller-focus");
      element.focus({preventScroll: true});
      element.scrollIntoView({block: "nearest", inline: "nearest", behavior: "instant"});
      if (this.diagnostics) this.diagnostics.focus(this.model.current, element);
    }

    _rememberElement(element) {
      const target = element && element.closest ? element.closest("[data-controller-zone]") : element;
      if (!target || !target.dataset || !target.dataset.controllerZone) return;
      this.model.set({
        zone: target.dataset.controllerZone,
        row: Number(target.dataset.controllerRow || 0),
        col: Number(target.dataset.controllerRuntimeCol || target.dataset.controllerCol || 0),
      });
    }

    _restoreAfterDialog() {
      this.model.zoneOrder = DEFAULT_ZONES;
      this.model.current = this.lastNonModal || {zone: "primary", row: 0, col: 0};
      this.lastNonModal = null;
      this.reconcile(this.usingController);
    }

    _pressedState(pad) {
      const logical = this.mapper.pressedState(pad);
      return {
        up: logical[LOGICAL_ACTIONS.UP], down: logical[LOGICAL_ACTIONS.DOWN],
        left: logical[LOGICAL_ACTIONS.LEFT], right: logical[LOGICAL_ACTIONS.RIGHT],
        confirm: logical[LOGICAL_ACTIONS.CONFIRM], back: logical[LOGICAL_ACTIONS.BACK],
        lb: logical[LOGICAL_ACTIONS.PREVIOUS_PAGE], rb: logical[LOGICAL_ACTIONS.NEXT_PAGE],
        menu: logical[LOGICAL_ACTIONS.MENU],
      };
    }

    _activePad(excludedIndex = null) {
      const pads = this._gamepads();
      for (const index of this.connected) {
        if (index !== excludedIndex && this.mapper.supports(pads[index])) return pads[index];
      }
      const fallback = pads.find((pad) => pad && pad.index !== excludedIndex && this.mapper.supports(pad));
      if (fallback) this.connected.add(fallback.index);
      return fallback || null;
    }

    _gamepads() {
      return this.window.navigator.getGamepads ? Array.from(this.window.navigator.getGamepads() || []) : [];
    }

    _available(element) {
      return !element.disabled && !element.closest(".hidden") && element.getAttribute("aria-hidden") !== "true";
    }

    _dialogOpen() {
      return Boolean(this.document.querySelector("dialog[open]"));
    }

    _setControllerMode(enabled) {
      this.usingController = enabled;
      this.document.body.classList.toggle("controller-active", enabled);
      if (!enabled) this.document.querySelectorAll(".controller-focus").forEach((element) => element.classList.remove("controller-focus"));
    }

    _announce() {
      const pads = this._gamepads().filter((pad) => pad && pad.connected);
      const usable = pads.filter((pad) => this.mapper.supports(pad)).length;
      this.window.dispatchEvent(new this.window.CustomEvent("romcloud:controller-status", {
        detail: {connected: pads.length > 0, usable: usable > 0, unsupported: Math.max(0, pads.length - usable)},
      }));
    }

    _resetInputs() {
      Object.values(this.repeaters).forEach((repeater) => repeater.reset());
      this.edges = {confirm: false, back: false, menu: false};
    }

    _schedule() {
      if (this.frame === null && this.connected.size) {
        this.frame = this.window.requestAnimationFrame(this._boundPoll);
      } else if (this.diagnostics && this.frame === null && this.scanTimer === null) {
        this.scanTimer = this.window.setTimeout(() => {
          this.scanTimer = null;
          this.frame = this.window.requestAnimationFrame(this._boundPoll);
        }, 1000);
      }
    }

    _key(descriptor) {
      return descriptor ? `${descriptor.zone}:${descriptor.row}:${descriptor.col}` : "";
    }
  }

  function startBrowserController(win = window, doc = document, options = {}) {
    const diagnostics = options.diagnostics || (
      options.diagnosticsEndpoint
        ? new BrowserControllerDiagnostics(win, options.diagnosticsEndpoint, options)
        : null
    );
    try {
      return new BrowserGamepadNavigator(
        win, doc, options.mapper || new StandardGamepadMapper(), diagnostics
      ).start();
    } catch (error) {
      if (diagnostics) diagnostics.failure(error);
      throw error;
    }
  }

  const api = {
    FocusModel, RepeatButton, ControllerKeyboardModel, BrowserControllerDiagnostics,
    StandardGamepadMapper, BrowserGamepadNavigator,
    startBrowserController, DEFAULT_ZONES, LOGICAL_ACTIONS, STANDARD_GAMEPAD_BINDINGS,
  };
  root.ROMCloudController = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
