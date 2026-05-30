const state = {
  taskId: new URLSearchParams(window.location.search).get("task_id") || "",
  latestReport: null,
};

const els = {
  taskSelect: document.getElementById("taskSelect"),
  goal: document.getElementById("goal"),
  overallPercent: document.getElementById("overallPercent"),
  completedItems: document.getElementById("completedItems"),
  currentPhase: document.getElementById("currentPhase"),
  attentionBanner: document.getElementById("attentionBanner"),
  phaseTree: document.getElementById("phaseTree"),
  showReport: document.getElementById("showReport"),
  overlay: document.getElementById("noticeOverlay"),
  closeNotice: document.getElementById("closeNotice"),
  noticeTitle: document.getElementById("noticeTitle"),
  noticeBody: document.getElementById("noticeBody"),
};

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function loadTasks() {
  const payload = await fetchJson("/api/tasks");
  const tasks = payload.tasks || [];
  if (!state.taskId && tasks.length) {
    state.taskId = tasks[tasks.length - 1].id;
  }
  els.taskSelect.innerHTML = tasks
    .map((task) => {
      const selected = task.id === state.taskId ? "selected" : "";
      return `<option value="${escapeHtml(task.id)}" ${selected}>${escapeHtml(task.goal)} (${escapeHtml(task.id)})</option>`;
    })
    .join("");
}

async function loadReport() {
  if (!state.taskId) return;
  const report = await fetchJson(`/api/tasks/${encodeURIComponent(state.taskId)}/progress`);
  state.latestReport = report;
  renderReport(report);
  showNewNotice(report.notifications || []);
}

function renderReport(report) {
  els.goal.textContent = report.goal || "Untitled task";
  els.overallPercent.textContent = `${report.summary.overall_percent}%`;
  els.completedItems.textContent = `${report.summary.completed_items}/${report.summary.total_items}`;
  els.currentPhase.textContent = report.summary.current_phase || "-";

  if (report.summary.needs_attention) {
    const blockerCount = report.blockers.length;
    els.attentionBanner.classList.remove("hidden");
    els.attentionBanner.textContent = blockerCount
      ? `${blockerCount} blocker(s) need human or agent attention.`
      : "Recent orchestrator notice needs attention.";
  } else {
    els.attentionBanner.classList.add("hidden");
    els.attentionBanner.textContent = "";
  }

  els.phaseTree.innerHTML = (report.phases || [])
    .map((phase, index) => renderPhase(phase, index))
    .join("");
}

function renderPhase(phase, index) {
  const side = index % 2 === 0 ? "left" : "right";
  const checklist = (phase.checklist || []).map(renderChecklistItem).join("");
  return `
    <article class="phase-node ${side} ${escapeHtml(phase.status)}">
      <div class="branch-dot" aria-hidden="true"></div>
      <div class="phase-card">
        <div class="phase-top">
          <div>
            <p class="eyebrow">Phase ${escapeHtml(String(phase.index))}</p>
            <h3>${escapeHtml(phase.name)}</h3>
          </div>
          <span class="status-pill ${escapeHtml(phase.status)}">${labelForStatus(phase.status)}</span>
        </div>
        <p class="phase-goal">${escapeHtml(phase.goal || "")}</p>
        <div class="progress-bar" aria-label="Phase progress">
          <span style="width:${Number(phase.progress_percent) || 0}%"></span>
        </div>
        <div class="phase-count">${escapeHtml(String(phase.completed))}/${escapeHtml(String(phase.total))} complete</div>
        <ul class="checklist">${checklist}</ul>
      </div>
    </article>
  `;
}

function renderChecklistItem(item) {
  const evidence = (item.evidence || []).slice(0, 2).join(" - ");
  const files = (item.files_present || []).length && (item.files_expected || []).length
    ? `${item.files_present.length}/${item.files_expected.length} files`
    : "";
  return `
    <li class="check-item ${escapeHtml(item.status)}">
      <span class="check-mark" aria-hidden="true"></span>
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <span>${escapeHtml([labelForStatus(item.status), item.worker, files, evidence].filter(Boolean).join(" - "))}</span>
      </div>
    </li>
  `;
}

function showNewNotice(notifications) {
  const latest = [...notifications].reverse().find((notice) => notice.tone === "warning") || notifications[notifications.length - 1];
  if (!latest) return;
  const key = `progress-notice-seen:${state.taskId}:${latest.id}`;
  if (localStorage.getItem(key)) return;
  localStorage.setItem(key, "1");
  openNotice(latest.title, latest.body);
}

function openNotice(title, body) {
  els.noticeTitle.textContent = title || "Orchestrator notice";
  els.noticeBody.textContent = body || "";
  els.overlay.classList.remove("hidden");
}

function closeNotice() {
  els.overlay.classList.add("hidden");
}

function labelForStatus(status) {
  return {
    done: "Done",
    in_progress: "In progress",
    running: "Running",
    blocked: "Blocked",
    failed: "Failed",
    todo: "Todo",
  }[status] || status || "Todo";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function refresh() {
  try {
    await loadTasks();
    await loadReport();
  } catch (error) {
    els.attentionBanner.classList.remove("hidden");
    els.attentionBanner.textContent = `Progress report failed: ${error.message}`;
  }
}

els.taskSelect.addEventListener("change", () => {
  state.taskId = els.taskSelect.value;
  const url = new URL(window.location.href);
  url.searchParams.set("task_id", state.taskId);
  window.history.replaceState({}, "", url);
  loadReport();
});

els.showReport.addEventListener("click", () => {
  if (!state.latestReport) return;
  const summary = state.latestReport.summary;
  openNotice(
    "Progress report",
    `${state.latestReport.goal}\n\nOverall: ${summary.overall_percent}% (${summary.completed_items}/${summary.total_items})\nCurrent phase: ${summary.current_phase}`,
  );
});

els.closeNotice.addEventListener("click", closeNotice);
els.overlay.addEventListener("click", (event) => {
  if (event.target === els.overlay) closeNotice();
});

refresh();
setInterval(refresh, 10000);
