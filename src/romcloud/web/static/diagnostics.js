(function (root) {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const text = (value, fallback = "—") => value === null || value === undefined || value === "" ? fallback : String(value);
  const stamp = (value) => text(value).replace("T", " ").replace(/(\.\d+)?\+00:00$/, " UTC");
  const title = (value) => text(value, "unknown").replaceAll("_", " ").replaceAll(".", " / ").replace(/\b\w/g, (letter) => letter.toUpperCase());

  class DiagnosticsBrowser {
    constructor({api, contentUpdated, exitLocal}) {
      this.api = api;
      this.contentUpdated = contentUpdated;
      this.exitLocal = exitLocal;
      this.active = false;
      this.kind = "operations";
      this.page = 1;
      this.hasMore = false;
      this.operation = null;
      this.detailPage = 1;
      this.detailHasMore = false;
      this.detailEvents = [];
      this.detailMode = "groups";
      this._bound = false;
    }

    bind() {
      if (this._bound) return;
      this._bound = true;
      $("diag-operations-tab").addEventListener("click", () => this.setKind("operations"));
      $("diag-events-tab").addEventListener("click", () => this.setKind("events"));
      $("diag-apply").addEventListener("click", () => { this.page = 1; this.loadList(); });
      $("diag-clear").addEventListener("click", () => this.clearFilters());
      $("diag-previous").addEventListener("click", () => this.changePage(-1));
      $("diag-next").addEventListener("click", () => this.changePage(1));
      $("diag-detail-back").addEventListener("click", () => this.closeOperation());
      $("diag-group-back").addEventListener("click", () => this.closeGroup());
      $("diag-groups-tab").addEventListener("click", () => this.setDetailMode("groups"));
      $("diag-raw-tab").addEventListener("click", () => this.setDetailMode("raw"));
      $("diag-detail-previous").addEventListener("click", () => this.changeDetailPage(-1));
      $("diag-detail-next").addEventListener("click", () => this.changeDetailPage(1));
      ["diag-subsystem", "diag-level", "diag-time"].forEach((id) => {
        $(id).addEventListener("change", () => { this.page = 1; this.loadList(); });
      });
      ["diag-search", "diag-operation"].forEach((id) => {
        $(id).addEventListener("change", () => { this.page = 1; this.loadList(); });
        $(id).addEventListener("keydown", (event) => {
          if (event.key === "Enter") { event.preventDefault(); this.page = 1; this.loadList(); }
        });
      });
    }

    open() {
      this.bind();
      this.active = true;
      $("library-main").classList.add("hidden");
      $("diagnostics-main").classList.remove("hidden");
      $("app-section-title").textContent = "Diagnostics";
      this.showList();
      return this.loadList();
    }

    showList() {
      $("diagnostics-list-view").classList.remove("hidden");
      $("diagnostics-detail-view").classList.add("hidden");
      $("diagnostics-group-view").classList.add("hidden");
    }

    setKind(kind) {
      if (kind === this.kind) return;
      this.kind = kind;
      this.page = 1;
      $("diag-operations-tab").classList.toggle("active", kind === "operations");
      $("diag-events-tab").classList.toggle("active", kind === "events");
      this.loadList();
    }

    filters() {
      const range = $("diag-time").value;
      let start = "";
      if (range) {
        const hours = {"24h": 24, "7d": 168, "30d": 720}[range];
        start = new Date(Date.now() - hours * 3600000).toISOString();
      }
      return {
        subsystem: $("diag-subsystem").value,
        level: $("diag-level").value,
        operation_id: $("diag-operation").value.trim(),
        search: $("diag-search").value.trim(),
        start_utc: start,
      };
    }

    query(extra = {}) {
      const params = new URLSearchParams({
        kind: this.kind,
        page: String(this.page),
        page_size: "30",
        ...this.filters(),
        ...extra,
      });
      [...params.entries()].forEach(([key, value]) => { if (!value) params.delete(key); });
      return params;
    }

    async loadList() {
      const list = $("diagnostic-list");
      list.replaceChildren(this.empty("Loading diagnostics…"));
      this.notice("");
      try {
        const data = await this.api(`/api/diagnostics?${this.query()}`);
        this.hasMore = Boolean(data.has_more);
        this.populateFacets(data.facets || {});
        this.renderList(this.kind === "operations" ? data.operations || [] : data.events || []);
        $("diag-previous").disabled = this.page <= 1;
        $("diag-next").disabled = !this.hasMore;
        $("diag-page-label").textContent = `Page ${this.page}${this.hasMore ? " · more available" : ""}`;
      } catch (error) {
        list.replaceChildren(this.empty(error.message, true));
      }
      this.contentUpdated();
    }

    renderList(items) {
      const list = $("diagnostic-list");
      list.replaceChildren();
      if (!items.length) { list.append(this.empty("No matching retained diagnostics.")); return; }
      items.forEach((item, index) => {
        const row = document.createElement("button");
        row.className = "diagnostic-row";
        row.dataset.controllerZone = "diagnostic-list";
        row.dataset.controllerRow = String(index);
        row.dataset.controllerCol = "0";
        if (this.kind === "operations") {
          const identity = document.createElement("div");
          identity.append(this.strong(item.name), this.small(stamp(item.timestamp_utc)));
          const subsystem = document.createElement("div"); subsystem.className = "diagnostic-subsystem";
          subsystem.append(this.strong(item.subsystem), this.small(text(item.operation_id).slice(0, 16)));
          const message = document.createElement("div"); message.className = "diagnostic-message";
          message.textContent = this.operationSummary(item);
          const displayStatus = ["failed", "running"].includes(item.status) ? item.status : "success";
          row.append(identity, subsystem, message, this.badge(displayStatus));
          row.addEventListener("click", () => this.openOperation(item));
        } else {
          const identity = document.createElement("div");
          identity.append(this.strong(stamp(item.timestamp_utc)), this.small(item.event_code));
          const subsystem = document.createElement("div"); subsystem.className = "diagnostic-subsystem";
          subsystem.append(this.strong(item.subsystem), this.small(text(item.operation_id).slice(0, 16)));
          const message = document.createElement("div"); message.className = "diagnostic-message"; message.textContent = item.message;
          row.append(identity, subsystem, message, this.badge(item.level));
        }
        list.append(row);
      });
    }

    operationSummary(item) {
      const parts = [];
      if (item.duration_ms !== null && item.duration_ms !== undefined) parts.push(`${item.duration_ms} ms`);
      if (item.effective_mode) parts.push(`mode ${item.effective_mode}`);
      if (item.provider_id || item.provider_type) parts.push(`provider ${item.provider_id || item.provider_type}`);
      if (item.generation_before !== null && item.generation_before !== undefined || item.generation_after !== null && item.generation_after !== undefined) {
        parts.push(`journal ${text(item.generation_before)} → ${text(item.generation_after)}`);
      }
      if (item.subsystem === "savesync") {
        parts.push(`examined ${item.examined || 0}`, `↑${item.uploaded || 0}`, `↓${item.downloaded || 0}`, `conflicts ${item.conflicts || 0}`, `unchanged ${item.unchanged || 0}`, `repairs ${item.repairs || 0}`);
      } else parts.push(`${item.event_count || 0} events`);
      return parts.join(" · ");
    }

    async openOperation(operation) {
      this.operation = operation;
      this.detailPage = 1;
      this.detailMode = "groups";
      $("diagnostics-list-view").classList.add("hidden");
      $("diagnostics-detail-view").classList.remove("hidden");
      $("diag-detail-title").textContent = operation.name || operation.operation_id;
      this.renderSummary(operation);
      if (root.romcloudGamepad) root.romcloudGamepad.pushContext({zone: "diagnostic-actions", row: 0, col: 0});
      await this.loadOperation();
    }

    async loadOperation() {
      const content = $("diag-detail-content");
      content.replaceChildren(this.empty("Loading operation events…"));
      try {
        const params = this.query({
          kind: "events", detail: "1", operation_id: this.operation.operation_id,
          page: String(this.detailPage), page_size: "100",
          subsystem: "", level: "", search: "", start_utc: "",
        });
        const data = await this.api(`/api/diagnostics?${params}`);
        this.detailEvents = data.events || [];
        this.detailHasMore = Boolean(data.has_more);
        $("diag-detail-previous").disabled = this.detailPage <= 1;
        $("diag-detail-next").disabled = !this.detailHasMore;
        $("diag-detail-page-label").textContent = `Event page ${this.detailPage}${this.detailHasMore ? " · more available" : ""}`;
        this.renderOperationContent();
      } catch (error) {
        content.replaceChildren(this.empty(error.message, true));
      }
      this.contentUpdated();
    }

    renderSummary(item) {
      const fields = [
        ["Time", stamp(item.timestamp_utc)], ["Duration", item.duration_ms == null ? "—" : `${item.duration_ms} ms`],
        ["Status", title(["failed", "running"].includes(item.status) ? item.status : "success")],
        ["Result", title(item.status)], ["Mode", text(item.effective_mode)],
        ["Provider", text(item.provider_id || item.provider_type)],
        ["Journal generation", `${text(item.generation_before)} → ${text(item.generation_after)}`],
        ["Examined", item.examined || 0], ["Uploaded", item.uploaded || 0],
        ["Downloaded", item.downloaded || 0], ["Conflicts", item.conflicts || 0],
        ["Unchanged", item.unchanged || 0], ["Repairs", item.repairs || 0],
      ];
      const rootNode = $("diag-summary"); rootNode.replaceChildren();
      fields.forEach(([name, value]) => {
        const metric = document.createElement("div"); metric.className = "metric";
        metric.append(this.small(name), this.strong(value)); rootNode.append(metric);
      });
    }

    setDetailMode(mode) {
      this.detailMode = mode;
      $("diag-groups-tab").classList.toggle("active", mode === "groups");
      $("diag-raw-tab").classList.toggle("active", mode === "raw");
      this.renderOperationContent();
      this.contentUpdated();
    }

    renderOperationContent() {
      const content = $("diag-detail-content"); content.replaceChildren();
      if (this.detailMode === "raw") {
        this.detailEvents.forEach((event, index) => content.append(this.rawEvent(event, index)));
        if (!this.detailEvents.length) content.append(this.empty("No events on this page."));
        return;
      }
      const groups = this.groupsFromEvents(this.detailEvents);
      if (!groups.length) {
        content.append(this.empty("This operation has no structured changed-save/group events on this page. Use Advanced / Raw Events for the complete timeline."));
        return;
      }
      groups.forEach((group, index) => {
        const row = document.createElement("button"); row.className = "diagnostic-row";
        row.dataset.controllerZone = "diagnostic-list"; row.dataset.controllerRow = String(index); row.dataset.controllerCol = "0";
        const identity = document.createElement("div"); identity.append(this.strong(group.groupId), this.small(group.layout));
        const classification = document.createElement("div"); classification.className = "diagnostic-subsystem";
        classification.append(this.strong(group.classification), this.small(group.reason));
        const decision = document.createElement("div"); decision.className = "diagnostic-message";
        decision.textContent = `${title(group.decision)} · ${group.mutations.length} physical mutation event(s)`;
        row.append(identity, classification, decision, this.badge(group.transactionStatus));
        row.addEventListener("click", () => this.openGroup(group)); content.append(row);
      });
    }

    groupsFromEvents(events) {
      const groups = new Map();
      let transactionStatus = "unknown";
      const operationOutcomes = [];
      events.forEach((event) => {
        const metadata = event.metadata || {};
        if (event.event_code.startsWith("transaction.")) {
          transactionStatus = metadata.status || event.event_code.split(".").slice(1).join(" ");
        }
        if (["journal.committed", "cursor.advanced"].includes(event.event_code)) operationOutcomes.push(event);
        const groupId = metadata.group_id;
        if (!groupId) return;
        if (!groups.has(groupId)) groups.set(groupId, {groupId, layout: "—", classification: "—", decision: "—", reason: "—", transactionStatus: "unknown", events: [], mutations: []});
        const group = groups.get(groupId); group.events.push(event);
        if (metadata.layout_id) group.layout = metadata.layout_id;
        if (metadata.classification) group.classification = metadata.classification;
        if (metadata.decision) group.decision = metadata.decision;
        if (metadata.reason) group.reason = metadata.reason;
        if (event.event_code.startsWith("physical_mutation.")) group.mutations.push(event);
        if (event.event_code.startsWith("transaction.")) group.transactionStatus = event.event_code.split(".").slice(1).join(" ");
      });
      groups.forEach((group) => {
        if (group.transactionStatus === "unknown") group.transactionStatus = transactionStatus;
        group.events.push(...operationOutcomes);
      });
      return [...groups.values()].filter((group) => (
        group.classification === "changed" ||
        (group.decision !== "—" && group.decision !== "unchanged") ||
        group.mutations.length > 0
      ));
    }

    openGroup(group) {
      $("diagnostics-detail-view").classList.add("hidden");
      $("diagnostics-group-view").classList.remove("hidden");
      $("diag-group-title").textContent = group.groupId;
      this.renderGroup(group);
      if (root.romcloudGamepad) root.romcloudGamepad.pushContext({zone: "diagnostic-actions", row: 0, col: 0});
      this.contentUpdated();
    }

    renderGroup(group) {
      const rootNode = $("diag-group-content"); rootNode.replaceChildren();
      const latest = (code) => [...group.events].reverse().find((event) => event.event_code === code);
      const classification = latest("group.classified") || {metadata: {}};
      const decision = latest("reconciliation.decision") || {metadata: {}};
      const localHash = classification.metadata.local_hash || decision.metadata.local_hash;
      const remoteHash = classification.metadata.remote_hash || decision.metadata.remote_hash;
      const baselineHash = classification.metadata.baseline_hash || decision.metadata.baseline_hash;
      const outcomeEvents = group.events.filter((event) => ["baseline.advanced", "journal.committed", "cursor.advanced", "dirty_marker.cleared"].includes(event.event_code));
      rootNode.append(this.fieldSection("Identity and classification", {
        "Logical group": group.groupId, "Layout": group.layout,
        "Classification": group.classification,
        "Local classification": !localHash || !baselineHash ? "unavailable" : localHash === baselineHash ? "matches baseline" : "differs from baseline",
        "Remote classification": !remoteHash || !baselineHash ? "unavailable" : remoteHash === baselineHash ? "matches baseline" : "differs from baseline",
        "Baseline classification": baselineHash ? "recorded" : "absent",
        "Local hash": localHash, "Remote hash": remoteHash, "Baseline hash": baselineHash,
      }));
      rootNode.append(this.fieldSection("Decision and transaction", {
        "Decision": group.decision, "Reason": group.reason,
        "Transaction status": group.transactionStatus,
        "Transaction ID": decision.metadata.transaction_id || classification.metadata.transaction_id,
      }));
      const mutations = this.section("Physical mutations");
      if (!group.mutations.length) mutations.append(this.small("No physical mutations were required."));
      group.mutations.forEach((event) => mutations.append(this.rawEvent(event)));
      rootNode.append(mutations);
      const outcomes = this.section("Journal / baseline / cursor outcome");
      if (!outcomeEvents.length) outcomes.append(this.small("No group-scoped outcome event was recorded on this page."));
      outcomeEvents.forEach((event) => outcomes.append(this.rawEvent(event)));
      rootNode.append(outcomes);
    }

    rawEvent(event, index = null) {
      const row = document.createElement("article"); row.className = "diagnostic-section"; row.tabIndex = -1;
      if (index !== null) { row.dataset.controllerZone = "diagnostic-list"; row.dataset.controllerRow = String(index); row.dataset.controllerCol = "0"; }
      const heading = document.createElement("h2"); heading.textContent = `${stamp(event.timestamp_utc)} · ${event.level} · ${event.event_code}`;
      const message = document.createElement("div"); message.textContent = event.message;
      const metadata = document.createElement("pre"); metadata.className = "raw-metadata"; metadata.textContent = JSON.stringify(event.metadata || {}, null, 2);
      row.append(heading, message, metadata); return row;
    }

    closeGroup() {
      $("diagnostics-group-view").classList.add("hidden");
      $("diagnostics-detail-view").classList.remove("hidden");
      if (root.romcloudGamepad) root.romcloudGamepad.popContext();
      this.contentUpdated();
    }

    closeOperation() {
      this.operation = null;
      this.showList();
      if (root.romcloudGamepad) root.romcloudGamepad.popContext();
      this.contentUpdated();
    }

    back() {
      if (!$("diagnostics-group-view").classList.contains("hidden")) { this.closeGroup(); return true; }
      if (!$("diagnostics-detail-view").classList.contains("hidden")) { this.closeOperation(); return true; }
      this.exitLocal();
      return true;
    }

    pageJump(delta) {
      if (!$("diagnostics-detail-view").classList.contains("hidden")) return this.changeDetailPage(delta);
      if (!$("diagnostics-list-view").classList.contains("hidden")) return this.changePage(delta);
    }

    changePage(delta) {
      const next = this.page + Math.sign(delta);
      if (next < 1 || next > this.page && !this.hasMore) return;
      this.page = next; this.loadList();
    }

    changeDetailPage(delta) {
      const next = this.detailPage + Math.sign(delta);
      if (next < 1 || next > this.detailPage && !this.detailHasMore) return;
      this.detailPage = next; this.loadOperation();
    }

    clearFilters() {
      ["diag-search", "diag-subsystem", "diag-level", "diag-operation", "diag-time"].forEach((id) => { $(id).value = ""; });
      this.page = 1; this.loadList();
    }

    populateFacets(facets) {
      this.populateSelect("diag-subsystem", facets.subsystems || [], "All subsystems");
      this.populateSelect("diag-level", facets.levels || [], "All levels");
    }

    populateSelect(id, values, allLabel) {
      const select = $(id), current = select.value;
      select.replaceChildren();
      const all = document.createElement("option"); all.value = ""; all.textContent = allLabel; select.append(all);
      values.forEach((value) => { const option = document.createElement("option"); option.value = value; option.textContent = value; select.append(option); });
      if ([...select.options].some((option) => option.value === current)) select.value = current;
    }

    notice(message) {
      $("diag-notice").textContent = message;
      $("diag-notice").classList.toggle("hidden", !message);
    }

    badge(value) { const node = document.createElement("span"); node.className = `diagnostic-badge ${String(value || "").toLowerCase()}`; node.textContent = title(value); return node; }
    strong(value) { const node = document.createElement("b"); node.textContent = text(value); return node; }
    small(value) { const node = document.createElement("small"); node.textContent = text(value); return node; }
    empty(message, error = false) { const node = document.createElement("div"); node.className = `empty${error ? " error" : ""}`; node.textContent = message; return node; }
    section(name) { const node = document.createElement("section"); node.className = "diagnostic-section"; const heading = document.createElement("h2"); heading.textContent = name; node.append(heading); return node; }
    fieldSection(name, fields) { const node = this.section(name), list = document.createElement("dl"); list.className = "diagnostic-fields"; Object.entries(fields).forEach(([key, value]) => { const term = document.createElement("dt"); term.textContent = key; const detail = document.createElement("dd"); detail.textContent = text(value); list.append(term, detail); }); node.append(list); return node; }
  }

  root.ROMCloudDiagnostics = {DiagnosticsBrowser};
})(window);
