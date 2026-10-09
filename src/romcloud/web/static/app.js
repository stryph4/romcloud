const $ = (id) => document.getElementById(id);
const state = {token: "", localSession: false, controllerFirst: false, view: "library", system: "", scope: "full", page: 1, pages: 0, selected: new Set(), status: null, loadSequence: 0};
let contentUpdateScheduled = false;
let oskSession = null;
let diagnosticsBrowser = null;
let downloadsPoller = null;
let downloadsRenderSignature = "";

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
    $("download-selected").disabled = !state.status.can_download;
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

function ensureDiagnosticsBrowser() {
  if (diagnosticsBrowser) return diagnosticsBrowser;
  diagnosticsBrowser = new window.ROMCloudDiagnostics.DiagnosticsBrowser({
    api,
    contentUpdated,
    exitLocal: state.controllerFirst ? requestLocalExit : null,
  });
  return diagnosticsBrowser;
}

function setViewNav(view) {
  state.view = view;
  $("nav-library").classList.toggle("active", view === "library");
  $("nav-downloads").classList.toggle("active", view === "downloads");
  $("nav-diagnostics").classList.toggle("active", view === "diagnostics");
  const url = new URL(location.href);
  if (view === "library") url.searchParams.delete("view");
  else url.searchParams.set("view", view);
  history.replaceState(null, "", url);
  if (downloadsPoller) void downloadsPoller.viewChanged();
}

async function openDiagnostics() {
  if (diagnosticsBrowser && diagnosticsBrowser.active) return;
  $("downloads-main").classList.add("hidden");
  setViewNav("diagnostics");
  await ensureDiagnosticsBrowser().open();
}

function showLibrary() {
  if (diagnosticsBrowser && diagnosticsBrowser.active) diagnosticsBrowser.close();
  $("diagnostics-main").classList.add("hidden");
  $("downloads-main").classList.add("hidden");
  $("library-main").classList.remove("hidden");
  $("app-section-title").textContent = "Library Manager";
  setViewNav("library");
  contentUpdated();
}

async function openDownloads() {
  if (diagnosticsBrowser && diagnosticsBrowser.active) diagnosticsBrowser.close();
  $("library-main").classList.add("hidden");
  $("diagnostics-main").classList.add("hidden");
  $("downloads-main").classList.remove("hidden");
  $("app-section-title").textContent = "Downloads";
  setViewNav("downloads");
  await refreshDownloads();
}

function downloadControl(text, item, action, row, col, danger = false) {
  const button = el("button", danger ? "danger-text" : "", text);
  button.dataset.controllerZone = "downloads";
  button.dataset.controllerRow = String(row);
  button.dataset.controllerCol = String(col);
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await api(`/api/downloads/${item.id}/${action}`, {method: "POST", body: "{}"});
      await refreshDownloads();
    } catch (error) { showDownloadsNotice(error.message, true); }
    finally { button.disabled = false; }
  });
  return button;
}

function showDownloadsNotice(message, error = false) {
  const node = $("downloads-notice");
  node.textContent = message; node.classList.remove("hidden"); node.classList.toggle("error", error);
}

async function loadDownloads() {
  try {
    const data = await api("/api/downloads");
    const signature = JSON.stringify(data);
    if (signature === downloadsRenderSignature) return data;
    downloadsRenderSignature = signature;
    $("partial-usage").textContent = `${formatBytes(data.retained_partial_bytes)} retained`;
    const root = $("downloads-list"); root.replaceChildren();
    const groups = [
      ["Active", ["running", "verifying"]], ["Queued", ["queued"]],
      ["Paused", ["paused"]], ["Interrupted", ["interrupted"]],
      ["Failed", ["failed"]], ["History", ["cancelled", "complete"]],
    ];
    let row = 0;
    groups.forEach(([heading, states]) => {
      const items = data.items.filter((item) => states.includes(item.state));
      if (!items.length) return;
      root.append(el("h2", "download-section-title", heading));
      items.forEach((item) => {
        const card = el("article", "download-item");
        const main = el("div", "download-main");
        const detail = el("div", "game-title");
        detail.append(el("b", "", item.game_title), el("small", "", `${item.system} · ${title(item.state)} · ${formatBytes(item.bytes_present)} of ${formatBytes(item.bytes_total)}`));
        const badge = el("span", `badge ${item.state}`, title(item.state));
        main.append(detail, badge);
        if (["running", "verifying"].includes(item.state)) {
          const progress = document.createElement("progress");
          if (item.bytes_total) { progress.max = item.bytes_total; progress.value = item.bytes_present; }
          const speed = item.speed_bytes_per_second ? `${formatBytes(item.speed_bytes_per_second)}/s` : "Calculating speed";
          const eta = item.eta_seconds != null ? ` · ${formatDuration(item.eta_seconds)} remaining` : "";
          card.append(main, progress, el("small", "download-stats", `${speed}${eta}`));
        } else card.append(main);
        if (item.error_message) card.append(el("div", "error", item.error_message));
        if (item.total_files > 1) card.append(el("small", "download-stats", `${number(item.retained_files)} files retained · ${number(item.interrupted_files)} interrupted file${item.interrupted_files === 1 ? "" : "s"} being verified · ${number(item.remaining_files)} files remaining`));
        const actions = el("div", "row-actions"); let col = 0;
        if (["running", "verifying"].includes(item.state)) actions.append(downloadControl("Pause", item, "pause", row, col++), downloadControl("Cancel", item, "cancel", row, col++, true));
        if (["paused", "interrupted", "cancelled"].includes(item.state) && (item.state !== "cancelled" || item.has_partial)) actions.append(downloadControl("Resume", item, "resume", row, col++));
        if (item.state === "failed") actions.append(downloadControl("Retry", item, "retry", row, col++));
        if (["paused", "interrupted", "failed", "cancelled"].includes(item.state) && item.has_partial) actions.append(downloadControl("Discard Partial", item, "discard", row, col++, true));
        if (item.state === "queued") actions.append(downloadControl("Cancel", item, "cancel", row, col++, true), downloadControl("Remove", item, "remove", row, col++));
        card.append(actions); root.append(card); row++;
      });
    });
    if (!root.children.length) root.append(el("div", "empty", "No downloads yet."));
    const active = data.active;
    $("job").classList.toggle("hidden", !active);
    if (active) {
      $("job-title").textContent = active.state === "verifying" ? `Verifying ${active.game_title}` : `Downloading ${active.game_title}`;
      $("job-detail").textContent = `${formatBytes(active.bytes_present)} / ${formatBytes(active.bytes_total)}`;
      if (active.bytes_total) { $("job-progress").max = active.bytes_total; $("job-progress").value = active.bytes_present; }
      else $("job-progress").removeAttribute("value");
    }
    contentUpdated();
    return data;
  } catch (error) {
    downloadsRenderSignature = "";
    $("downloads-list").replaceChildren(el("div", "empty error", error.message));
    $("job").classList.add("hidden");
    contentUpdated();
    return null;
  }
}

function refreshDownloads() {
  return downloadsPoller ? downloadsPoller.refreshNow() : loadDownloads();
}

async function runAction(action, ids, button = null) {
  if (button) button.disabled = true;
  showNotice(`${title(action)} in progress…`);
  try {
    await api("/api/actions", {method: "POST", body: JSON.stringify({action, game_ids: ids})});
    ids.forEach((id) => state.selected.delete(id)); updateBulk(); showNotice(["cache", "download_selected"].includes(action) ? `Queued ${ids.length} download${ids.length === 1 ? "" : "s"}.` : `${title(action)} completed for ${ids.length} game${ids.length === 1 ? "" : "s"}.`); await loadSystems(); await loadGames(); await refreshDownloads();
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
    [["Pinned games needing data", number(plan.games_needing_data)], ["Future growth needed", formatBytes(plan.additional_bytes)], ["Current physical cache", formatBytes(plan.current_cache_bytes)], ["Retained staging", formatBytes(plan.staging_bytes)], ["Active reserved growth", formatBytes(plan.active_reserved_growth)], ["Projected accounted storage", formatBytes(plan.resulting_cache_bytes)], ["Cache-size limit", formatBytes(plan.max_cache_bytes)], ["Filesystem free", formatBytes(plan.free_bytes)], ["Minimum free reserve", formatBytes(plan.min_free_bytes)]].forEach(([name, value]) => { const metric = el("div", "metric"); metric.append(el("span", "", name), el("b", "", value)); body.append(metric); });
    $("preflight-error").textContent = plan.reasons.join(" ");
    $("start-download").disabled = !plan.allowed || plan.games_needing_data === 0;
    contentUpdated();
  } catch (error) { body.replaceChildren(); $("preflight-error").textContent = error.message; }
}

async function startDownload() {
  $("start-download").disabled = true;
  try { await api("/api/download-pinned", {method: "POST", body: "{}"}); $("preflight").close(); await refreshDownloads(); showNotice("Pinned downloads queued."); }
  catch (error) { $("preflight-error").textContent = error.message; $("start-download").disabled = false; }
}

function updateBulk() { $("bulk").classList.toggle("hidden", !state.selected.size); $("selected-count").textContent = `${state.selected.size} selected`; contentUpdated(); }
function setActiveTab() { document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("active", node.dataset.scope === state.scope)); }
function showNotice(message, error = false) { const node = $("notice"); node.textContent = message; node.classList.remove("hidden"); node.classList.toggle("error", error); }
function el(tag, className = "", textContent = "") { const node = document.createElement(tag); if (className) node.className = className; if (textContent !== "") node.textContent = textContent; return node; }
function title(value) { return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()); }
function labelState(value) { return {remote_only: "Remote Only", cached: "Cached", pinned: "Pinned", incomplete: "Incomplete", transferring: "Transferring"}[value] || title(value); }
function number(value) { return new Intl.NumberFormat().format(value || 0); }
function formatBytes(value) { if (value == null) return "Size unknown"; if (value === 0) return "0 B"; const units = ["B", "KB", "MB", "GB", "TB"]; const power = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1); return `${(value / 1024 ** power).toFixed(power ? 1 : 0)} ${units[power]}`; }
function formatDuration(seconds) { const value = Math.max(0, Math.round(seconds)); if (value < 60) return `${value}s`; if (value < 3600) return `${Math.ceil(value / 60)}m`; return `${(value / 3600).toFixed(1)}h`; }

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
  const isSearch = source.type === "search";
  $("osk-title").textContent = isSearch ? (source.id === "search" ? "Search library" : "Search diagnostics") : "Enter text";
  $("osk-submit").textContent = isSearch ? "Search" : "Submit";
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
$("job").addEventListener("click", openDownloads);
$("downloads-cancel-all").addEventListener("click", () => { $("cancel-all-dialog").showModal(); contentUpdated(); });
$("cancel-all-back").addEventListener("click", () => $("cancel-all-dialog").close("cancel"));
$("cancel-all-confirm").addEventListener("click", async () => {
  $("cancel-all-confirm").disabled = true;
  try {
    const result = await api("/api/downloads/cancel-all", {method: "POST", body: "{}"});
    $("cancel-all-dialog").close("confirmed");
    showDownloadsNotice(`Cancelled ${result.cancelled} download${result.cancelled === 1 ? "" : "s"}.`);
    await refreshDownloads();
  } catch (error) { showDownloadsNotice(error.message, true); }
  finally { $("cancel-all-confirm").disabled = false; }
});
$("downloads-retry-all").addEventListener("click", async () => { await api("/api/downloads/retry-all-failed", {method: "POST", body: "{}"}); await refreshDownloads(); });
$("downloads-cleanup").addEventListener("click", async () => { const result = await api("/api/downloads/cleanup", {method: "POST", body: "{}"}); showDownloadsNotice(`Cleaned ${result.cleaned} stale download${result.cleaned === 1 ? "" : "s"}.`); await refreshDownloads(); });

document.addEventListener("visibilitychange", () => {
  if (downloadsPoller) void downloadsPoller.visibilityChanged();
});

window.addEventListener("romcloud:page-jump", (event) => {
  if (diagnosticsBrowser && diagnosticsBrowser.active) {
    diagnosticsBrowser.pageJump(Number(event.detail.delta || 0));
    return;
  }
  if (!state.pages || $("preflight").open) return;
  const nextPage = Math.max(1, Math.min(state.pages, state.page + Number(event.detail.delta || 0)));
  if (nextPage === state.page) return;
  state.page = nextPage;
  $("page-label").textContent = `Page ${state.page} of ${state.pages}`;
  loadGames();
});

window.addEventListener("romcloud:controller-back", (event) => {
  if (diagnosticsBrowser && diagnosticsBrowser.active) {
    event.preventDefault(); diagnosticsBrowser.back();
    if (!diagnosticsBrowser.active) {
      setViewNav("library");
      if (window.romcloudGamepad) window.romcloudGamepad.focusZone("global");
    }
    return;
  }
  if (state.view === "downloads") {
    event.preventDefault(); showLibrary();
    if (window.romcloudGamepad) window.romcloudGamepad.focusZone("global");
    return;
  }
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
  $("nav-library").addEventListener("click", showLibrary);
  $("nav-downloads").addEventListener("click", openDownloads);
  $("nav-diagnostics").addEventListener("click", openDiagnostics);
  await connect(state.token || "");
  downloadsPoller = new window.ROMCloudDownloadPolling.AdaptiveDownloadPoller({
    document,
    load: loadDownloads,
    isDownloadsView: () => state.view === "downloads",
  });
  const requestedView = new URLSearchParams(location.search).get("view");
  if (new URLSearchParams(location.search).get("view") === "diagnostics") {
    await openDiagnostics();
  } else if (requestedView === "downloads") {
    await openDownloads();
  } else {
    setViewNav("library");
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
