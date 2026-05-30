const state = {
  tasks: [],
  currentTask: null,
  filter: "all",
  search: "",
  showFull: false,
  hideAutoInvoked: localStorage.getItem("studioHideAutoInvoked") === "true",
  language: localStorage.getItem("studioLanguage") || "en",
  seenMessageIds: new Set(),
  hasLoadedCurrentTask: false,
};

const els = {
  taskList: document.querySelector("#taskList"),
  taskCount: document.querySelector("#taskCount"),
  taskGoal: document.querySelector("#taskGoal"),
  taskStatus: document.querySelector("#taskStatus"),
  metrics: document.querySelector("#metrics"),
  conversation: document.querySelector("#conversation"),
  agents: document.querySelector("#agents"),
  workItems: document.querySelector("#workItems"),
  artifacts: document.querySelector("#artifacts"),
  searchInput: document.querySelector("#searchInput"),
  refreshButton: document.querySelector("#refreshButton"),
  fullToggle: document.querySelector("#fullToggle"),
  hideAutoInvokedToggle: document.querySelector("#hideAutoInvokedToggle"),
  composeForm: document.querySelector("#composeForm"),
  composeInput: document.querySelector("#composeInput"),
  recipientSelect: document.querySelector("#recipientSelect"),
  messageTypeSelect: document.querySelector("#messageTypeSelect"),
  composeStatus: document.querySelector("#composeStatus"),
  sendMessageButton: document.querySelector("#sendMessageButton"),
  stopAgentsButton: document.querySelector("#stopAgentsButton"),
  resetAllSessionsButton: document.querySelector("#resetAllSessionsButton"),
  dialog: document.querySelector("#messageDialog"),
  dialogMeta: document.querySelector("#dialogMeta"),
  dialogTitle: document.querySelector("#dialogTitle"),
  dialogContent: document.querySelector("#dialogContent"),
  closeDialog: document.querySelector("#closeDialog"),
};

const translations = {
  en: {
    appTitle: "Conversation Mission Control",
    search: "Search",
    searchPlaceholder: "agent, type, text",
    refresh: "Refresh",
    tasks: "Tasks",
    selectTask: "Select a task",
    recipient: "Recipient",
    messageType: "Type",
    messageToOrchestrator: "Message",
    composePlaceholder: "Send a note, instruction, or question to the orchestrator",
    send: "Send",
    sending: "Sending...",
    sent: "Sent",
    dispatched: "Dispatched",
    agentStarted: "Agent started",
    agentReplied: "Agent replied",
    sendFailed: "Send failed",
    chooseTask: "Choose a task before sending.",
    emptyMessage: "Write a message first.",
    all: "All",
    showFullText: "Show full text",
    hideAutoInvoked: "Hide auto-invoked logs",
    agents: "Agents",
    missionBoard: "Mission Board",
    artifactsReviews: "Artifacts & Reviews",
    fullMessage: "Full message",
    nextMessage: "Next message is being prepared",
    waitingForAgent: "Waiting for agent response",
    close: "Close",
    noTasks: "No task files found.",
    noMessages: "No messages match the current filters.",
    noHeartbeats: "No heartbeats yet.",
    noWork: "No work items.",
    noArtifacts: "No artifacts or reviews yet.",
    messages: "messages",
    work: "work",
    noMessagesYet: "No messages yet",
    to: "to",
    chars: "chars",
    fullText: "Full text",
    current: "Current",
    active: "active",
    stale: "stale",
    dead: "dead",
    unknown: "unknown",
    metricMessages: "Messages",
    metricWork: "Work",
    metricArtifacts: "Artifacts",
    metricReviews: "Reviews",
    reloadMcp: "Reload MCP",
    reloadAll: "Reload all",
    reloadDone: "Session reset",
    reloadFailed: "Session reset failed",
    stopAgents: "Stop agents",
    stoppingAgents: "Stopping...",
    stopDone: "Stop requested",
    stopFailed: "Stop failed",
  },
  ja: {
    appTitle: "会話ミッションコントロール",
    search: "検索",
    searchPlaceholder: "agent、type、本文",
    refresh: "更新",
    tasks: "タスク",
    selectTask: "タスクを選択",
    recipient: "宛先",
    messageType: "種類",
    messageToOrchestrator: "メッセージ",
    composePlaceholder: "orchestratorへの指示・質問・メモを送信",
    send: "送信",
    sending: "送信中...",
    sent: "送信しました",
    dispatched: "起動しました",
    agentStarted: "エージェント起動",
    agentReplied: "エージェント応答",
    sendFailed: "送信失敗",
    chooseTask: "送信前にタスクを選択してください。",
    emptyMessage: "先にメッセージを書いてください。",
    all: "すべて",
    showFullText: "全文表示",
    hideAutoInvoked: "自動起動ログを非表示",
    agents: "エージェント",
    missionBoard: "ミッションボード",
    artifactsReviews: "成果物・レビュー",
    fullMessage: "全文メッセージ",
    nextMessage: "次のメッセージを準備中",
    waitingForAgent: "エージェントの応答待機中",
    close: "閉じる",
    noTasks: "タスクファイルがありません。",
    noMessages: "現在のフィルタに一致するメッセージはありません。",
    noHeartbeats: "heartbeatはまだありません。",
    noWork: "作業項目はありません。",
    noArtifacts: "成果物またはレビューはまだありません。",
    messages: "件の発言",
    work: "件の作業",
    noMessagesYet: "メッセージなし",
    to: "宛先",
    chars: "文字",
    fullText: "全文",
    current: "現在",
    active: "稼働中",
    stale: "停滞",
    dead: "停止",
    unknown: "不明",
    metricMessages: "発言",
    metricWork: "作業",
    metricArtifacts: "成果物",
    metricReviews: "レビュー",
  },
};

Object.assign(translations.ja, {
  stopAgents: "\u5f37\u5236\u505c\u6b62",
  stoppingAgents: "\u505c\u6b62\u4e2d...",
  stopDone: "\u505c\u6b62\u3092\u8981\u6c42\u3057\u307e\u3057\u305f",
  stopFailed: "\u505c\u6b62\u5931\u6557",
});

function t(key) {
  return translations[state.language][key] || translations.en[key] || key;
}

async function loadTasks() {
  const response = await fetch("/api/tasks");
  const data = await response.json();
  state.tasks = data.tasks;
  renderTasks();
  if (!state.currentTask && state.tasks.length) {
    await selectTask(state.tasks[0].id);
  } else if (state.currentTask) {
    await selectTask(state.currentTask.id, { preserveScroll: true });
  }
}

async function selectTask(taskId, options = {}) {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`);
  const nextTask = await response.json();
  trackIncomingMessages(nextTask);
  state.currentTask = nextTask;
  renderTasks();
  renderTask();
  if (!options.preserveScroll) {
    els.conversation.scrollTop = 0;
  }
}

function renderTasks() {
  els.taskCount.textContent = `${state.tasks.length}`;
  if (!state.tasks.length) {
    els.taskList.innerHTML = `<div class="empty">${t("noTasks")}</div>`;
    return;
  }
  els.taskList.innerHTML = state.tasks.map((task) => `
    <button class="task-row ${state.currentTask?.id === task.id ? "active" : ""}" data-task-id="${escapeHtml(task.id)}" type="button">
      <span class="task-title">${escapeHtml(task.goal)}</span>
      <span class="task-meta">
        <span class="pill">${escapeHtml(task.status)}</span>
        <span class="pill">${task.message_count} ${t("messages")}</span>
        <span class="pill">${task.work_count} ${t("work")}</span>
      </span>
      <span class="muted">${escapeHtml(task.latest_message || t("noMessagesYet"))}</span>
    </button>
  `).join("");

  document.querySelectorAll("[data-task-id]").forEach((button) => {
    button.addEventListener("click", () => selectTask(button.dataset.taskId));
  });
}

function renderTask() {
  const task = state.currentTask;
  if (!task) return;
  els.taskGoal.textContent = task.goal;
  els.taskStatus.textContent = `${task.status} / ${task.id}`;
  els.metrics.innerHTML = [
    [t("metricMessages"), task.messages.length],
    [t("metricWork"), task.work_items.length],
    [t("metricArtifacts"), task.artifacts.length],
    [t("metricReviews"), task.reviews.length],
  ].map(([label, value]) => `<div class="metric"><strong>${value}</strong><span>${label}</span></div>`).join("");

  renderConversation(task);
  renderAgents(task);
  renderWork(task);
  renderArtifacts(task);
}

function renderConversation(task) {
  const visibleActivityMessages = task.message_summaries.filter((message) => {
    const matchesFilter = state.filter === "all" || message.sender === state.filter;
    const haystack = `${message.sender} ${message.type} ${message.content}`.toLowerCase();
    const matchesSearch = !state.search || haystack.includes(state.search.toLowerCase());
    return matchesFilter && matchesSearch && isAutoInvocationMessage(message);
  }).sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

  const messages = task.message_summaries.filter((message) => {
    const matchesFilter = state.filter === "all" || message.sender === state.filter;
    const haystack = `${message.sender} ${message.type} ${message.content}`.toLowerCase();
    const matchesSearch = !state.search || haystack.includes(state.search.toLowerCase());
    const isHiddenHeartbeat = message.type === "heartbeat";
    return matchesFilter && matchesSearch && !(state.hideAutoInvoked && isHiddenHeartbeat);
  }).sort((a, b) => new Date(b.created_at) - new Date(a.created_at));

  const pendingAgents = runningAgents(task);
  if (!messages.length && !pendingAgents.length && !visibleActivityMessages.length) {
    els.conversation.innerHTML = `<div class="empty">${t("noMessages")}</div>`;
    return;
  }

  const pendingHtml = pendingAgents.map((agent) => pendingMessageCard(agent)).join("");
  const activityHtml = activityNoticeCards(visibleActivityMessages);
  const messageHtml = messages.map((message) => {
    const text = state.showFull ? message.content : summarizeMessage(message);
    const title = messageTitle(message);
    return `
      <article class="message-row ${escapeHtml(message.type)}">
        <div class="message-meta">
          ${agentPill(message.sender)}
          <span class="pill">${escapeHtml(message.type)}</span>
          <span class="muted">${formatTime(message.created_at)}</span>
          ${message.recipient !== "all" ? `<span class="pill">${t("to")} ${escapeHtml(message.recipient)}</span>` : ""}
        </div>
        <h3 class="message-title">${escapeHtml(title)}</h3>
        <p class="message-body">${escapeHtml(text)}</p>
        <div class="message-actions">
          <span class="muted">${message.char_count} ${t("chars")}</span>
          <button class="small-button" data-message-id="${escapeHtml(message.id)}" type="button">${t("fullText")}</button>
        </div>
      </article>
    `;
  }).join("");
  els.conversation.innerHTML = pendingHtml + activityHtml + messageHtml;

  document.querySelectorAll("[data-message-id]").forEach((button) => {
    button.addEventListener("click", () => openMessage(button.dataset.messageId));
  });
}

function activityNoticeCards(messages) {
  return messages
    .filter((message) => isAutoInvocationMessage(message))
    .slice(0, 3)
    .map((message) => `
      <article class="activity-notice ${escapeHtml(message.sender)}">
        ${agentPill(message.sender)}
        <span>${escapeHtml(t("agentStarted"))}</span>
        <span class="muted">${formatTime(message.created_at)}</span>
      </article>
    `)
    .join("");
}

function isAutoInvocationMessage(message) {
  return message.type === "heartbeat" && String(message.content || "").startsWith("Auto-invoked for");
}

function renderAgents(task) {
  const entries = Object.entries(task.agent_activity || {});
  if (!entries.length) {
    els.agents.innerHTML = `<div class="empty">${t("noHeartbeats")}</div>`;
    return;
  }
  els.agents.innerHTML = entries.map(([agent, activity]) => {
    const liveness = getLiveness(activity.last_seen_at);
    const resetButton = ["claude", "codex"].includes(agent)
      ? `<button class="small-button session-reset-button" data-reset-agent="${escapeHtml(agent)}" type="button">${escapeHtml(t("reloadMcp"))}</button>`
      : "";
    return `
    <div class="agent-row ${liveness}">
      <div class="task-meta">
        <span class="status-dot ${liveness}" title="${escapeHtml(t(liveness))}"></span>
        <span class="agent-name">${escapeHtml(agent)}</span>
        <span class="pill">${escapeHtml(activity.status)}</span>
        <span class="pill liveness ${liveness}">${escapeHtml(t(liveness))}</span>
        ${resetButton}
      </div>
      <div class="muted">${escapeHtml(formatTime(activity.last_seen_at))}</div>
      ${activity.current_work_id ? `<div class="muted">${t("current")}: ${escapeHtml(activity.current_work_id)}</div>` : ""}
      ${activity.note ? `<div>${escapeHtml(activity.note)}</div>` : ""}
    </div>
  `;
  }).join("");

  document.querySelectorAll("[data-reset-agent]").forEach((button) => {
    button.addEventListener("click", () => resetSession(button.dataset.resetAgent));
  });
}

function runningAgents(task) {
  return Object.entries(task.agent_activity || {})
    .filter(([, activity]) => activity.status === "running")
    .map(([agent, activity]) => ({ agent, activity }));
}

function pendingMessageCard({ agent, activity }) {
  return `
    <article class="pending-message-row ${escapeHtml(agent)}">
      <div class="message-meta">
        ${agentPill(agent)}
        <span class="pill">${escapeHtml(t("waitingForAgent"))}</span>
        <span class="muted">${escapeHtml(formatTime(activity.last_seen_at))}</span>
      </div>
      <h3 class="message-title">${escapeHtml(t("nextMessage"))}</h3>
      <div class="pending-body">
        <div class="typing-spinner" aria-hidden="true">
          <span></span><span></span><span></span><span></span><span></span><span></span>
        </div>
        <div>
          <strong>${escapeHtml(agent)}</strong>
          <p>${escapeHtml(activity.note || t("waitingForAgent"))}</p>
        </div>
      </div>
    </article>
  `;
}

function agentPill(agent) {
  return `<span class="pill ${escapeHtml(agent)}">${agentLogo(agent)}<span>${escapeHtml(agent)}</span></span>`;
}

function agentLogo(agent) {
  if (agent === "claude") {
    return '<span class="agent-logo claude-logo" aria-hidden="true">C</span>';
  }
  if (agent === "codex") {
    return '<span class="agent-logo codex-logo" aria-hidden="true">◇</span>';
  }
  if (agent === "orchestrator") {
    return '<span class="agent-logo orchestrator-logo" aria-hidden="true">O</span>';
  }
  if (agent === "human") {
    return '<span class="agent-logo human-logo" aria-hidden="true">H</span>';
  }
  return "";
}

function renderWork(task) {
  if (!task.work_items.length) {
    els.workItems.innerHTML = `<div class="empty">${t("noWork")}</div>`;
    return;
  }
  els.workItems.innerHTML = task.work_items.map((item) => `
    <div class="work-row ${escapeHtml(item.status)}">
      <div class="work-title">${escapeHtml(item.title)}</div>
      <div class="work-meta">
        <span class="pill ${escapeHtml(item.owner)}">${escapeHtml(item.owner)}</span>
        <span class="pill">${escapeHtml(item.status)}</span>
      </div>
      ${item.description ? `<div class="muted">${escapeHtml(item.description)}</div>` : ""}
    </div>
  `).join("");
}

function renderArtifacts(task) {
  const parts = [];
  if (task.artifacts.length) {
    parts.push(...task.artifacts.map((artifact) => `
      <div class="artifact-row">
        <strong>${escapeHtml(artifact.title)}</strong>
        <div class="task-meta">
          <span class="pill">${escapeHtml(artifact.kind)}</span>
          <span class="pill ${escapeHtml(artifact.owner)}">${escapeHtml(artifact.owner || t("unknown"))}</span>
        </div>
        ${artifact.path ? `<div class="muted">${escapeHtml(artifact.path)}</div>` : ""}
      </div>
    `));
  }
  if (task.reviews.length) {
    parts.push(...task.reviews.map((review) => `
      <div class="artifact-row">
        <strong>${escapeHtml(review.verdict)}</strong>
        <div class="muted">${escapeHtml(review.reviewer)} on ${escapeHtml(review.artifact_id)}</div>
        <div>${escapeHtml(review.notes)}</div>
      </div>
    `));
  }
  els.artifacts.innerHTML = parts.join("") || `<div class="empty">${t("noArtifacts")}</div>`;
}

function trackIncomingMessages(task) {
  const ids = new Set(task.messages.map((message) => message.id));
  if (!state.currentTask || state.currentTask.id !== task.id || !state.hasLoadedCurrentTask) {
    state.seenMessageIds = ids;
    state.hasLoadedCurrentTask = true;
    return;
  }

  const incoming = task.messages.filter((message) => !state.seenMessageIds.has(message.id));
  state.seenMessageIds = ids;
  incoming.forEach((message) => {
    if (isAutoInvocationMessage(message)) {
      showToast(`${t("agentStarted")}: ${message.sender}`, "info");
    }
    if (message.type === "report_result" && message.metadata?.source === "auto_agent") {
      showToast(`${t("agentReplied")}: ${message.sender}`, "success");
    }
  });
}

function showToast(message, tone = "info") {
  let container = document.querySelector("#toastStack");
  if (!container) {
    container = document.createElement("div");
    container.id = "toastStack";
    container.className = "toast-stack";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.className = `toast ${tone}`;
  toast.textContent = message;
  container.prepend(toast);
  window.setTimeout(() => {
    toast.classList.add("leaving");
    window.setTimeout(() => toast.remove(), 240);
  }, 4200);
}

function openMessage(messageId) {
  const message = state.currentTask.message_summaries.find((item) => item.id === messageId);
  if (!message) return;
  els.dialogMeta.textContent = `${message.sender} -> ${message.recipient} / ${message.type} / ${formatTime(message.created_at)}`;
  els.dialogTitle.textContent = t("fullMessage");
  els.dialogContent.textContent = message.content;
  els.dialog.showModal();
}

function messageTitle(message) {
  if (message.type === "handoff") {
    return state.language === "ja" ? `${message.recipient}への引き継ぎ` : `Handoff to ${message.recipient}`;
  }
  if (message.type === "heartbeat") {
    return state.language === "ja" ? `${message.sender}の生存通知` : `${message.sender} heartbeat`;
  }
  if (message.type === "assign_task") {
    return firstMeaningfulLine(message.content) || (state.language === "ja" ? "作業割り当て" : "Work assignment");
  }
  if (message.type === "report_result") {
    const firstLine = firstMeaningfulLine(message.content);
    if (firstLine.startsWith("実行しました")) return "実行しました";
    if (firstLine.startsWith("実行します")) return "実行します";
    if (firstLine.startsWith("実行しません")) return "実行しません";
    return firstLine || (state.language === "ja" ? `${message.sender}の結果報告` : `${message.sender} report`);
  }
  if (message.type === "chat" && message.sender === "human") {
    return state.language === "ja" ? "ユーザー指示" : "Human instruction";
  }
  return firstMeaningfulLine(message.content) || messageTypeLabel(message.type);
}

function firstMeaningfulLine(text) {
  const line = String(text || "")
    .split(/\r?\n/)
    .map((item) => item.trim())
    .find(Boolean) || "";
  const normalized = line.replace(/\s+/g, " ");
  const limit = 54;
  return normalized.length > limit ? `${normalized.slice(0, limit).trim()}...` : normalized;
}

function summarizeMessage(message) {
  const normalized = String(message.content || "").replace(/\s+/g, " ").trim();
  const limit = 180;
  const clipped = normalized.length > limit ? `${normalized.slice(0, limit).trim()}...` : normalized;
  if (state.language !== "ja") {
    return message.summary || clipped;
  }
  const translated = translateCommonPhrases(clipped);
  const sender = escapeSummaryLabel(message.sender);
  const recipient = escapeSummaryLabel(message.recipient);
  const type = messageTypeLabel(message.type);
  return `${sender}から${recipient}への${type}: ${translated}`;
}

function translateCommonPhrases(text) {
  return text
    .replaceAll("The human sent this to the orchestrator.", "ユーザーがorchestratorに送信しました。")
    .replaceAll("Continue from the shared task context and answer back to the mission board.", "共有コンテキストから続行し、mission boardへ返答してください。")
    .replaceAll("The orchestrator assigned you this work item.", "orchestratorがこの作業を割り当てました。")
    .replaceAll("Auto-invoked for", "自動起動:")
    .replaceAll("Auto response recorded for", "自動応答を記録:")
    .replaceAll("Work item:", "作業項目:")
    .replaceAll("human message:", "ユーザーメッセージ:");
}

function messageTypeLabel(type) {
  if (state.language !== "ja") return type;
  return {
    proposal: "提案",
    plan_update: "計画更新",
    assign_task: "作業割り当て",
    report_result: "結果報告",
    request_review: "レビュー依頼",
    review: "レビュー",
    objection: "異議",
    decision: "決定",
    handoff: "引き継ぎ",
    heartbeat: "生存通知",
    chat: "会話",
    user_goal: "目標",
  }[type] || type;
}

function escapeSummaryLabel(value) {
  return String(value || t("unknown"));
}

function getLiveness(lastSeenAt) {
  const seen = new Date(lastSeenAt);
  if (Number.isNaN(seen.valueOf())) return "dead";
  const ageMs = Date.now() - seen.getTime();
  if (ageMs < 2 * 60 * 1000) return "active";
  if (ageMs < 15 * 60 * 1000) return "stale";
  return "dead";
}

async function submitMessage(event) {
  event.preventDefault();
  if (!state.currentTask) {
    setComposeStatus(t("chooseTask"), true);
    return;
  }
  const content = els.composeInput.value.trim();
  if (!content) {
    setComposeStatus(t("emptyMessage"), true);
    return;
  }
  setComposeStatus(t("sending"));
  els.sendMessageButton.disabled = true;
  try {
    const response = await fetch(`/api/tasks/${encodeURIComponent(state.currentTask.id)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sender: "human",
        recipient: els.recipientSelect.value,
        message_type: els.messageTypeSelect.value,
        content,
      }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `${response.status}`);
    }
    const result = await response.json();
    els.composeInput.value = "";
    if (result.dispatched_agents?.length) {
      setComposeStatus(`${t("sent")} / ${t("dispatched")}: ${result.dispatched_agents.join(", ")}`);
    } else {
      setComposeStatus(t("sent"));
    }
    await loadTasks();
  } catch (error) {
    setComposeStatus(`${t("sendFailed")}: ${error.message}`, true);
  } finally {
    els.sendMessageButton.disabled = false;
  }
}

async function resetSession(agent = "all") {
  if (!state.currentTask) return;
  const target = agent || "all";
  try {
    const response = await fetch("/api/reset_session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: state.currentTask.id,
        agent: target,
      }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `${response.status}`);
    }
    const result = await response.json();
    showToast(`${t("reloadDone")}: ${target} (${result.removed.length})`, "success");
  } catch (error) {
    showToast(`${t("reloadFailed")}: ${error.message}`, "error");
  }
}

async function stopAgents(agent = "all") {
  if (!state.currentTask) return;
  const target = agent || "all";
  const previousLabel = els.stopAgentsButton.textContent;
  els.stopAgentsButton.disabled = true;
  els.stopAgentsButton.textContent = t("stoppingAgents");
  try {
    const response = await fetch("/api/stop_agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task_id: state.currentTask.id,
        agent: target,
      }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || `${response.status}`);
    }
    const result = await response.json();
    showToast(`${t("stopDone")}: ${target} (${result.stopped.length})`, "success");
    await loadTasks();
  } catch (error) {
    showToast(`${t("stopFailed")}: ${error.message}`, "error");
  } finally {
    els.stopAgentsButton.disabled = false;
    els.stopAgentsButton.textContent = previousLabel || t("stopAgents");
  }
}

function setComposeStatus(message, isError = false) {
  els.composeStatus.textContent = message;
  els.composeStatus.classList.toggle("error-text", isError);
}

function applyLanguage() {
  document.documentElement.lang = state.language;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelectorAll(".language-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.language === state.language);
  });
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return date.toLocaleString();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

document.querySelectorAll(".segment").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".segment").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    renderTask();
  });
});

els.searchInput.addEventListener("input", (event) => {
  state.search = event.target.value;
  renderTask();
});

els.fullToggle.addEventListener("change", (event) => {
  state.showFull = event.target.checked;
  renderTask();
});

els.hideAutoInvokedToggle.checked = state.hideAutoInvoked;
els.hideAutoInvokedToggle.addEventListener("change", (event) => {
  state.hideAutoInvoked = event.target.checked;
  localStorage.setItem("studioHideAutoInvoked", String(state.hideAutoInvoked));
  renderTask();
});

els.refreshButton.addEventListener("click", () => loadTasks());
els.stopAgentsButton.addEventListener("click", () => stopAgents("all"));
els.resetAllSessionsButton.addEventListener("click", () => resetSession("all"));
els.closeDialog.addEventListener("click", () => els.dialog.close());
els.composeForm.addEventListener("submit", submitMessage);
document.querySelectorAll(".language-button").forEach((button) => {
  button.addEventListener("click", () => {
    state.language = button.dataset.language;
    localStorage.setItem("studioLanguage", state.language);
    applyLanguage();
    renderTasks();
    renderTask();
  });
});

applyLanguage();
loadTasks();
setInterval(loadTasks, 5000);
