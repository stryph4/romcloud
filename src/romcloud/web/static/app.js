const $ = (id) => document.getElementById(id);
const state = {token: "", localSession: false, controllerFirst: false, system: "", scope: "full", page: 1, pages: 0, selected: new Set(), status: null, loadSequence: 0};
let contentUpdateScheduled = false;
let oskSession = null;

function contentUpdated() {
  if (contentUpdateScheduled) return;
  contentUpdateScheduled = true;
  requestAnimationFrame(() => {
    contentUpdateScheduled = false;
    window.dispatchEvent(new CustomEvent("romcloud:content-updated"));
  });
}

function tokenFromStorage() {
  return sessionStorage.getItem("romcloud-token") || "";
}

async function api(path, options = {}) {
  const auth = state.token ? {"Authorization": `Bearer ${state.token}`} : {};
  const response = await fetch(path, {credentials: "same-origin", headers: {...auth, ...(options.body ? {"Content-Type": "application/json"} : {})}, ...options});
  let body = {};
  try { body = await response.json(); } catch (_) { /* handled below */ }
  if (!response.ok) { const error = new Error(body.error || `Request failed (${response.status})`); error.status = response.status; throw error; }
  return body;
}

async function pair(code, trust) {
  try {
    const response = await fetch("/auth/pair", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({code, trust}),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Pairing failed.");
    await connect("");
    return true;
  } catch (error) {
    $("auth-error").textContent = error.message;
    $("auth").classList.remove("hidden");
    return false;
  }
}

async function connect(token) {
  state.token = token;
  try {
    state.status = await api("/api/status");
    sessionStorage.setItem("romcloud-token", token);
    $("auth").classList.add("hidden"); $("shell").classList.remove("hidden");
    $("logout").classList.toggle("hidden", state.localSession);
    const modeName = state.status.mode === "cache" ? "Cached Storage" : title(state.status.mode);
    $("mode").textContent = modeName;
    $("offline-banner").classList.toggle("hidden", !state.status.offline);
    $("tab-full").disabled = !state.status.full_library_available;
    $("download-pinned").disabled = !state.status.can_download;
    if (!state.status.full_library_available) { state.scope = "device"; setActiveTab(); }
    if (!state.status.source_reachable && !state.status.offline) showNotice("ROM source unavailable — browsing local content only.", true);
    if (!window.isSecureContext) showNotice("Controller input requires HTTPS when this manager is opened from another device. Restart without --http.", true);
    await loadSystems();
    contentUpdated();
  } catch (error) {
    $("auth-error").textContent = error.status === 401 ? "That access token was not accepted." : error.message;
    $("auth").classList.remove("hidden"); $("shell").classList.add("hidden");
  }
}

async function loadTrustedDevices() {
  const data = await api("/api/trusted-devices");
  const root = $("device-list"); root.replaceChildren();
  if (!data.devices.length) root.append(el("div", "empty", "No remembered remote devices."));
  data.devices.forEach((device, index) => {
    const row = el("div", "device-row");
    const detail = el("div");
    detail.append(el("b", "", device.label), el("small", "", `${device.trust}${device.current ? " · This device" : ""}`));
    const revoke = el("button", "danger-text", "Revoke");
    revoke.dataset.controllerZone = "dialog"; revoke.dataset.controllerRow = String(index + 1); revoke.dataset.controllerCol = "0";
    revoke.addEventListener("click", async () => { await api("/api/trusted-devices/revoke", {method: "POST", body: JSON.stringify({id: device.id})}); await loadTrustedDevices(); });
    row.append(detail, revoke); root.append(row);
  });
}

async function loadSystems() {
  const data = await api("/api/systems");
  const root = $("systems"); root.replaceChildren();
  data.systems.forEach((item, index) => {
    const button = el("button", "system"); button.dataset.system = item.system;
    button.dataset.controllerZone = "systems"; button.dataset.controllerRow = String(index); button.dataset.controllerCol = "0";
    button.append(el("b", "", item.system), el("span", "", `${item.local}/${item.total}`));
    button.addEventListener("click", () => chooseSystem(item.system)); root.append(button);
  });
  if (!state.system && data.systems.length) chooseSystem(data.systems[0].system);
  contentUpdated();
}

function chooseSystem(system) {
  state.system = system; state.page = 1; state.selected.clear();
  $("system-title").textContent = system;
  document.querySelectorAll(".system").forEach((node) => node.classList.toggle("active", node.dataset.system === system));
  updateBulk(); loadGames();
}

async function loadGames() {
  if (!state.system) return;
  const sequence = ++state.loadSequence;
  const params = new URLSearchParams({system: state.system, scope: state.scope, search: $("search").value, state: $("state-filter").value, sort: $("sort").value, page: state.page, page_size: 50});
  $("games").replaceChildren(el("div", "empty", "Loading library…"));
  try {
    const data = await api(`/api/games?${params}`);
    if (sequence !== state.loadSequence) return;
    state.scope = data.scope; state.page = data.page; state.pages = data.pages; setActiveTab();
    renderGames(data.games);
    $("result-count").textContent = `${number(data.total)} games`;
    $("page-label").textContent = `Page ${data.pages ? data.page : 0} of ${data.pages}`;
    $("jump").value = data.pages ? data.page : 1; $("jump").max = Math.max(1, data.pages);
    $("previous").disabled = data.page <= 1; $("next").disabled = data.page >= data.pages;
    contentUpdated();
  } catch (error) {
    if (sequence !== state.loadSequence) return;
    $("games").replaceChildren(el("div", "empty error", error.message));
    contentUpdated();
  }
}

function renderGames(games) {
  const root = $("games"); root.replaceChildren();
  if (!games.length) { root.append(el("div", "empty", "No games match this view.")); return; }
  games.forEach((game, rowIndex) => {
    const row = el("article", "game");
    row.tabIndex = -1; row.setAttribute("role", "checkbox"); row.setAttribute("aria-label", `Select ${game.title}`);
    row.setAttribute("aria-checked", state.selected.has(game.id) ? "true" : "false");
    row.dataset.controllerZone = "games"; row.dataset.controllerRow = String(rowIndex); row.dataset.controllerCol = "0"; row.dataset.controllerActivate = "toggle-row";
    const check = el("input"); check.type = "checkbox"; check.checked = state.selected.has(game.id);
    check.addEventListener("change", () => { check.checked ? state.selected.add(game.id) : state.selected.delete(game.id); row.setAttribute("aria-checked", check.checked ? "true" : "false"); updateBulk(); });
    row.addEventListener("click", (event) => {
      if (event.target.closest("button,input,select,a")) return;
      row.focus(); check.click();
    });
    const details = el("div", "game-title"); details.append(el("b", "", game.title), el("small", "", formatBytes(game.local_size_bytes || game.source_size_bytes)));
    const filename = el("div", "file", game.filename);
    const right = el("div"); const badges = el("div", "badges"); badges.append(el("span", `badge ${game.state}`, labelState(game.state)));
    if (game.pinned && game.state !== "pinned") badges.append(el("span", "badge pinned", "Pinned"));
    if (game.offline_ready) badges.append(el("span", "badge cached", "Offline ready"));
    const actions = el("div", "row-actions");
    if (!game.offline_ready && state.status.can_download) actions.append(actionButton("Download", "cache", [game.id]));
    actions.append(actionButton(game.pinned ? "Unpin" : "Pin", game.pinned ? "unpin" : "pin", [game.id]));
    if (game.has_local_copy) actions.append(actionButton("Remove", "remove", [game.id]));
    const select = el("button", "controller-row-select", state.selected.has(game.id) ? "Unselect" : "Select");
    select.addEventListener("click", () => check.click()); actions.append(select);
    [...actions.children].forEach((button, actionIndex) => { button.dataset.controllerZone = "games"; button.dataset.controllerRow = String(rowIndex); button.dataset.controllerCol = String(actionIndex + 1); });
    right.append(badges, actions); row.append(check, details, filename, right); root.append(row);
  });
}

function actionButton(text, action, ids) { const button = el("button", "", text); button.addEventListener("click", () => runAction(action, ids, button)); return button; }

async function runAction(action, ids, button = null) {
  if (button) button.disabled = true;
  showNotice(`${title(action)} in progress…`);
  try {
    await api("/api/actions", {method: "POST", body: JSON.stringify({action, game_ids: ids})});
    ids.forEach((id) => state.selected.delete(id)); updateBulk(); showNotice(`${title(action)} completed for ${ids.length} game${ids.length === 1 ? "" : "s"}.`); await loadSystems(); await loadGames();
  } catch (error) { showNotice(error.message, true); }
  finally { if (button) button.disabled = false; }
}

async function showPreflight() {
  const dialog = $("preflight"), body = $("preflight-body"); body.replaceChildren(el("div", "metric", "Calculating dependency closure…"));
  $("preflight-error").textContent = ""; $("start-download").disabled = true; dialog.showModal();
  contentUpdated();
  try {
    const plan = await api("/api/download-pinned/preflight", {method: "POST", body: "{}"});
    body.replaceChildren();
    [["Pinned games needing data", number(plan.games_needing_data)], ["Additional download", formatBytes(plan.additional_bytes)], ["Current physical cache", formatBytes(plan.current_cache_bytes)], ["Cache-size limit", formatBytes(plan.max_cache_bytes)], ["Filesystem free", formatBytes(plan.free_bytes)], ["Minimum free reserve", formatBytes(plan.min_free_bytes)]].forEach(([name, value]) => { const metric = el("div", "metric"); metric.append(el("span", "", name), el("b", "", value)); body.append(metric); });
    $("preflight-error").textContent = plan.reasons.join(" ");
    $("start-download").disabled = !plan.allowed || plan.games_needing_data === 0;
    contentUpdated();
  } catch (error) { body.replaceChildren(); $("preflight-error").textContent = error.message; }
}

async function startDownload() {
  $("start-download").disabled = true;
  try { const job = await api("/api/download-pinned", {method: "POST", body: "{}"}); $("preflight").close(); $("job").classList.remove("hidden"); pollJob(job.id); }
  catch (error) { $("preflight-error").textContent = error.message; $("start-download").disabled = false; }
}

async function pollJob(id) {
  try {
    const job = await api(`/api/jobs/${id}`); $("job-detail").textContent = `${job.current_game}/${job.total_games}`;
    const progress = $("job-progress"); if (job.bytes_total) { progress.max = job.bytes_total; progress.value = job.bytes_done; } else { progress.removeAttribute("value"); }
    if (["queued", "running"].includes(job.state)) return setTimeout(() => pollJob(id), 750);
    $("job").classList.add("hidden"); showNotice(job.state === "complete" ? `Downloaded ${job.completed_game_ids.length} pinned games.` : job.error, job.state !== "complete"); await loadSystems(); await loadGames();
  } catch (error) { $("job").classList.add("hidden"); showNotice(error.message, true); }
}

function updateBulk() { $("bulk").classList.toggle("hidden", !state.selected.size); $("selected-count").textContent = `${state.selected.size} selected`; contentUpdated(); }
function setActiveTab() { document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("active", node.dataset.scope === state.scope)); }
function showNotice(message, error = false) { const node = $("notice"); node.textContent = message; node.classList.remove("hidden"); node.classList.toggle("error", error); }
function el(tag, className = "", textContent = "") { const node = document.createElement(tag); if (className) node.className = className; if (textContent !== "") node.textContent = textContent; return node; }
function title(value) { return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()); }
function labelState(value) { return {remote_only: "Remote Only", cached: "Cached", pinned: "Pinned", incomplete: "Incomplete", transferring: "Transferring"}[value] || title(value); }
function number(value) { return new Intl.NumberFormat().format(value || 0); }
function formatBytes(value) { if (value == null) return "Size unknown"; if (value === 0) return "0 B"; const units = ["B", "KB", "MB", "GB", "TB"]; const power = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1); return `${(value / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`; }

function renderOskPreview() {
  const preview = $("osk-preview"); preview.replaceChildren();
  if (!oskSession) return;
  preview.append(document.createTextNode(oskSession.model.value.slice(0, oskSession.model.cursor)));
  preview.append(el("span", "osk-caret", "│"));
  preview.append(document.createTextNode(oskSession.model.value.slice(oskSession.model.cursor)));
}

function buildOsk() {
  const keys = "1234567890QWERTYUIOPASDFGHJKLZXCVBNM-_.";
  const root = $("osk-keys"); root.replaceChildren();
  [...keys].forEach((character, index) => {
    const button = el("button", "osk-key", character);
    button.type = "button"; button.dataset.oskValue = character;
    button.dataset.controllerZone = "dialog";
    button.dataset.controllerRow = String(Math.floor(index / 10));
    button.dataset.controllerCol = String(index % 10);
    root.append(button);
  });
  document.querySelectorAll("#controller-osk [data-osk-action], #controller-osk .osk-edit-actions [data-osk-value]").forEach((button, index) => {
    button.dataset.controllerZone = "dialog"; button.dataset.controllerRow = "4"; button.dataset.controllerCol = String(index);
  });
  $("osk-cancel").dataset.controllerZone = "dialog"; $("osk-cancel").dataset.controllerRow = "5"; $("osk-cancel").dataset.controllerCol = "0";
  $("osk-submit").dataset.controllerZone = "dialog"; $("osk-submit").dataset.controllerRow = "5"; $("osk-submit").dataset.controllerCol = "1";
}

function openControllerKeyboard(source) {
  if (!state.controllerFirst) { source.focus(); return; }
  oskSession = {source, model: new window.ROMCloudController.ControllerKeyboardModel(source.value)};
  $("osk-title").textContent = source.id === "search" ? "Search library" : "Enter text";
  $("osk-submit").textContent = source.id === "search" ? "Search" : "Submit";
  renderOskPreview(); $("controller-osk").showModal(); contentUpdated();
}

function cancelControllerKeyboard() {
  if (!oskSession) return;
  oskSession.source.value = oskSession.model.cancel();
  oskSession = null; $("controller-osk").close("cancel");
}

async function submitControllerKeyboard() {
  if (!oskSession) return;
  const {source, model} = oskSession;
  source.value = model.value; oskSession = null; $("controller-osk").close("submit");
  source.dispatchEvent(new Event("input", {bubbles: true}));
  if (source.id === "search") {
    clearTimeout(searchTimer); state.page = 1; await loadGames();
    window.romcloudGamepad.focusZone("games");
  } else source.dispatchEvent(new Event("change", {bubbles: true}));
}

function requestLocalExit() {
  api("/api/local-exit", {method: "POST", body: "{}"}).catch((error) => showNotice(error.message, true));
}

$("pair-form").addEventListener("submit", (event) => { event.preventDefault(); pair($("pair-code").value, $("pair-trust").value); });
$("token-connect").addEventListener("click", () => connect($("token").value));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") window.dispatchEvent(new CustomEvent("romcloud:controller-back", {cancelable: true}));
});
$("logout").addEventListener("click", async () => { sessionStorage.removeItem("romcloud-token"); try { await api("/api/auth/logout", {method: "POST", body: "{}"}); } finally { location.reload(); } });
$("trusted-devices").addEventListener("click", async () => { await loadTrustedDevices(); $("devices-dialog").showModal(); });
$("close-devices").addEventListener("click", () => $("devices-dialog").close());
$("revoke-all").addEventListener("click", async () => { await api("/api/trusted-devices/revoke-all", {method: "POST", body: "{}"}); location.reload(); });
$("controller-menu-button").addEventListener("click", () => { $("controller-menu-dialog").showModal(); contentUpdated(); });
$("close-controller-menu").addEventListener("click", () => $("controller-menu-dialog").close());
$("exit-open-here").addEventListener("click", requestLocalExit);
$("osk-keys").addEventListener("click", (event) => { if (oskSession && event.target.dataset.oskValue) { oskSession.model.insert(event.target.dataset.oskValue); renderOskPreview(); } });
$("controller-osk").addEventListener("click", (event) => {
  if (!oskSession) return;
  const action = event.target.dataset.oskAction;
  if (event.target.closest("#osk-keys")) return;
  if (event.target.dataset.oskValue) oskSession.model.insert(event.target.dataset.oskValue);
  else if (action === "left") oskSession.model.moveCursor(-1);
  else if (action === "right") oskSession.model.moveCursor(1);
  else if (action === "backspace") oskSession.model.backspace();
  else if (action === "delete") oskSession.model.deleteForward();
  renderOskPreview();
});
$("osk-cancel").addEventListener("click", cancelControllerKeyboard);
$("osk-submit").addEventListener("click", submitControllerKeyboard);
$("controller-osk").addEventListener("close", () => {
  if (!oskSession) return;
  oskSession.source.value = oskSession.model.cancel(); oskSession = null;
});
document.addEventListener("romcloud:controller-text", (event) => openControllerKeyboard(event.detail.element));
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => { state.scope = tab.dataset.scope; state.page = 1; setActiveTab(); loadGames(); }));
let searchTimer; $("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.page = 1; loadGames(); }, 250); });
[$("state-filter"), $("sort")].forEach((node) => node.addEventListener("change", () => { state.page = 1; loadGames(); }));
$("previous").addEventListener("click", () => { if (state.page > 1) { state.page--; loadGames(); } });
$("next").addEventListener("click", () => { if (state.page < state.pages) { state.page++; loadGames(); } });
$("jump").addEventListener("change", () => { state.page = Math.max(1, Math.min(state.pages, Number($("jump").value) || 1)); loadGames(); });
$("select-page").addEventListener("change", (event) => { document.querySelectorAll(".game input[type=checkbox]").forEach((box) => { box.checked = event.target.checked; box.dispatchEvent(new Event("change")); }); });
$("clear-selection").addEventListener("click", () => { state.selected.clear(); updateBulk(); loadGames(); });
$("bulk").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => runAction(button.dataset.action, [...state.selected], button)));
$("download-pinned").addEventListener("click", showPreflight); $("start-download").addEventListener("click", startDownload);

window.addEventListener("romcloud:page-jump", (event) => {
  if (!state.pages || $("preflight").open) return;
  const nextPage = Math.max(1, Math.min(state.pages, state.page + Number(event.detail.delta || 0)));
  if (nextPage === state.page) return;
  state.page = nextPage;
  $("page-label").textContent = `Page ${state.page} of ${state.pages}`;
  loadGames();
});

window.addEventListener("romcloud:controller-back", (event) => {
  if (state.selected.size) {
    state.selected.clear(); updateBulk(); loadGames(); event.preventDefault(); return;
  }
  if ($("search").value) {
    $("search").value = ""; state.page = 1; loadGames(); event.preventDefault();
    return;
  }
  if (state.controllerFirst) {
    event.preventDefault(); requestLocalExit();
  }
});

window.addEventListener("romcloud:controller-menu", (event) => {
  if (!state.controllerFirst) return;
  event.preventDefault(); $("controller-menu-dialog").showModal(); contentUpdated();
});

window.addEventListener("romcloud:controller-status", (event) => {
  const connected = Boolean(event.detail.connected);
  const usable = Boolean(event.detail.usable);
  $("controller-status").textContent = usable
    ? "Controller connected"
    : "Controller connected — browser standard mapping unavailable";
  $("controller-status").classList.toggle("hidden", !connected);
  $("controller-help").classList.toggle("hidden", !usable);
  contentUpdated();
});

(async () => {
  state.localSession = ["127.0.0.1", "localhost", "::1"].includes(location.hostname);
  state.controllerFirst = state.localSession && new URLSearchParams(location.search).get("interaction") === "controller";
  document.body.classList.toggle("controller-first", state.controllerFirst);
  $("controller-menu-button").classList.toggle("hidden", !state.controllerFirst);
  buildOsk();
  state.token = tokenFromStorage();
  if (state.token) connect(state.token); else {
    await connect("");
  }
})();
window.romcloudGamepad = window.ROMCloudController.startBrowserController(
  window,
  document,
  {
    diagnosticsEndpoint: state.controllerFirst ? "/api/controller-diagnostics" : "",
    interactionMode: state.controllerFirst ? "controller" : "remote",
  },
);
