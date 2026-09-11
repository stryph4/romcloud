(function (root) {
  "use strict";

  const controller = root.ROMCloudController;
  if (!controller || !controller.BrowserGamepadNavigator) {
    if (typeof module !== "undefined" && module.exports) {
      throw new Error("ROMCloudController must be loaded before spatial_navigation.js");
    }
    return;
  }

  const HORIZONTAL = new Set(["left", "right"]);

  function rectFor(element) {
    if (!element || typeof element.getBoundingClientRect !== "function") return null;
    const rect = element.getBoundingClientRect();
    if (!rect) return null;
    const width = Number(rect.width !== undefined ? rect.width : rect.right - rect.left);
    const height = Number(rect.height !== undefined ? rect.height : rect.bottom - rect.top);
    if (!(width > 0) || !(height > 0)) return null;
    return {
      left: Number(rect.left), right: Number(rect.right),
      top: Number(rect.top), bottom: Number(rect.bottom),
      width, height,
      centerX: (Number(rect.left) + Number(rect.right)) / 2,
      centerY: (Number(rect.top) + Number(rect.bottom)) / 2,
    };
  }

  function rangesOverlap(aStart, aEnd, bStart, bEnd) {
    return Math.min(aEnd, bEnd) >= Math.max(aStart, bStart);
  }

  function directionalScore(current, candidate, direction) {
    const horizontal = HORIZONTAL.has(direction);
    const sign = direction === "left" || direction === "up" ? -1 : 1;
    const primaryDelta = sign * (
      horizontal
        ? candidate.centerX - current.centerX
        : candidate.centerY - current.centerY
    );
    if (!(primaryDelta > 0.5)) return null;

    const perpendicularDelta = Math.abs(
      horizontal
        ? candidate.centerY - current.centerY
        : candidate.centerX - current.centerX
    );
    const inBeam = horizontal
      ? rangesOverlap(current.top, current.bottom, candidate.top, candidate.bottom)
      : rangesOverlap(current.left, current.right, candidate.left, candidate.right);

    let primaryGap;
    let perpendicularGap;
    if (direction === "left") {
      primaryGap = Math.max(0, current.left - candidate.right);
      perpendicularGap = Math.max(0, candidate.top - current.bottom, current.top - candidate.bottom);
    } else if (direction === "right") {
      primaryGap = Math.max(0, candidate.left - current.right);
      perpendicularGap = Math.max(0, candidate.top - current.bottom, current.top - candidate.bottom);
    } else if (direction === "up") {
      primaryGap = Math.max(0, current.top - candidate.bottom);
      perpendicularGap = Math.max(0, candidate.left - current.right, current.left - candidate.right);
    } else {
      primaryGap = Math.max(0, candidate.top - current.bottom);
      perpendicularGap = Math.max(0, candidate.left - current.right, current.left - candidate.right);
    }

    return {
      inBeam,
      primaryDelta,
      perpendicularDelta,
      primaryGap,
      perpendicularGap,
      angle: Math.atan2(perpendicularDelta, primaryDelta),
      distance: Math.hypot(primaryDelta, perpendicularDelta),
    };
  }

  function compareScores(a, b) {
    if (a.inBeam !== b.inBeam) return a.inBeam ? -1 : 1;
    if (a.inBeam) {
      return a.primaryGap - b.primaryGap
        || a.primaryDelta - b.primaryDelta
        || a.perpendicularDelta - b.perpendicularDelta
        || a.distance - b.distance;
    }
    return a.angle - b.angle
      || a.perpendicularGap - b.perpendicularGap
      || a.distance - b.distance
      || a.primaryDelta - b.primaryDelta;
  }

  function descriptorFromKey(key) {
    const parts = String(key || "").split(":");
    if (parts.length !== 3) return null;
    const row = Number(parts[1]);
    const col = Number(parts[2]);
    if (!Number.isFinite(row) || !Number.isFinite(col)) return null;
    return {zone: parts[0], row, col};
  }

  function chooseSpatialTarget(currentElement, entries, direction, available = null) {
    const currentRect = rectFor(currentElement);
    if (!currentRect || !entries || !HORIZONTAL.has(direction) && !["up", "down"].includes(direction)) return null;

    let best = null;
    for (const [key, element] of entries) {
      if (!element || element === currentElement) continue;
      if (available && !available(element)) continue;
      const candidateRect = rectFor(element);
      if (!candidateRect) continue;
      const score = directionalScore(currentRect, candidateRect, direction);
      if (!score) continue;
      const descriptor = descriptorFromKey(key);
      if (!descriptor) continue;
      const candidate = {key, element, descriptor, score};
      if (!best || compareScores(score, best.score) < 0) best = candidate;
    }
    return best;
  }

  const Navigator = controller.BrowserGamepadNavigator;
  const logicalMove = Navigator.prototype._move;

  Navigator.prototype._move = function spatialMove(axis, delta) {
    this.reconcile();
    const direction = axis === "horizontal"
      ? (delta < 0 ? "left" : "right")
      : (delta < 0 ? "up" : "down");
    const current = this.elements.get(this._key(this.model.current));
    const target = chooseSpatialTarget(
      current,
      this.elements,
      direction,
      (element) => this._available(element),
    );
    if (!target) return logicalMove.call(this, axis, delta);
    this.model.set(target.descriptor);
    this._focusCurrent();
  };

  const api = {rectFor, directionalScore, compareScores, chooseSpatialTarget, descriptorFromKey};
  root.ROMCloudSpatialNavigation = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
