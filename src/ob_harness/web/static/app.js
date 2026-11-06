"use strict";

const t = (key, params) => window.I18n.t(key, params);
const $ = (id) => document.getElementById(id);
const els = {
  messages: $("messages"),
  welcome: $("welcome"),
  composer: $("composer"),
  input: $("input"),
  send: $("send"),
  connection: $("connection"),
  statusLine: $("status-line"),
  planner: $("planner"),
  memory: $("use-memory"),
  sidebar: $("sidebar"),
  backdrop: $("backdrop"),
  menu: $("menu-button"),
};

const state = { info: null, runId: null, busy: false, conversationId: null, revision: 0, reviewId: null, pending: null, status: null, connection: null };

function element(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = text;
  return value;
}

function shortVersion(value) {
  const text = String(value || "");
  return text.length > 10 ? `${text.slice(0, 8)}…` : text || "-";
}

function setConnection(ok, key) {
  state.connection = { ok, key };
  els.connection.textContent = t(key);
  els.connection.className = `badge ${ok ? "badge-ready" : "badge-error"}`;
}

function setStatus(text) {
  state.status = null;
  els.statusLine.textContent = text;
}

function setStatusKey(key, params) {
  state.status = { key, params };
  els.statusLine.textContent = t(key, params);
}

function scrollToBottom() {
  els.messages.scrollTop = els.messages.scrollHeight;
}

function autoGrow() {
  els.input.style.height = "auto";
  els.input.style.height = `${Math.min(els.input.scrollHeight, 180)}px`;
}

function setBusy(busy) {
  state.busy = busy;
  els.send.disabled = busy;
  const label = els.send.querySelector("[data-i18n]");
  const text = t(busy ? "send.busy" : "send");
  if (label) label.textContent = text;
  else els.send.textContent = text;
  els.send.setAttribute("aria-label", text);
  els.messages.setAttribute("aria-busy", String(busy));
}

function showTyping(bubble) {
  const typing = element("div", "typing");
  typing.append(element("span"), element("span"), element("span"));
  bubble.replaceChildren(typing);
}

function addUserMessage(text) {
  els.welcome.hidden = true;
  const row = element("div", "message user");
  row.append(element("div", "avatar", t("common.you")), element("div", "bubble", text));
  els.messages.append(row);
  scrollToBottom();
}

function addAssistantMessage() {
  const row = element("div", "message assistant");
  const bubble = element("div", "bubble");
  row.append(element("div", "avatar", "OH"), bubble);
  els.messages.append(row);
  scrollToBottom();
  return bubble;
}

function notice(text, kind) {
  return element("div", `notice ${kind || ""}`.trim(), text);
}

function detailsFor(value) {
  const details = element("details");
  details.append(element("summary", "", t("common.viewRaw")));
  const pre = element("pre", "", JSON.stringify(value, null, 2));
  details.append(pre);
  return details;
}

function statusPill(text, muted) {
  return element("span", `status-pill ${muted ? "muted" : ""}`.trim(), text);
}

function displayName(value, names) {
  return names.get(value) || String(value || "");
}

function renderClarification(bubble, result, data) {
  bubble.replaceChildren();
  const head = element("div", "answer-head");
  head.append(element("strong", "", t("clarification.heading")), statusPill(t("clarification.waiting"), true));
  bubble.append(head, element("p", "answer-text", result.question || t("clarification.defaultQuestion")));
  bubble.append(
    notice(t("clarification.note"), ""),
    detailsFor(data)
  );
}

function renderOntologyAnswer(bubble, result, data) {
  bubble.replaceChildren();
  const concepts = Array.isArray(result.concepts) ? result.concepts : [];
  const relations = Array.isArray(result.relations) ? result.relations : [];
  const found = result.status === "found" && concepts.length > 0;

  const head = element("div", "answer-head");
  head.append(
    element("strong", "", t("answer.ontologyResult")),
    statusPill(t(found ? "status.found" : "status.insufficient"), !found)
  );
  bubble.append(head);

  const summary = element("p", `answer-text ${found ? "" : "empty"}`.trim());
  if (found) {
    summary.textContent = t("answer.foundSummary", {
      concepts: concepts.length,
      relations: relations.length ? t("answer.relationsSuffix", { count: relations.length }) : "",
    });
  } else {
    summary.textContent = t("answer.notFound");
  }
  bubble.append(summary);

  if (concepts.length) {
    const list = element("div", "concept-list");
    for (const concept of concepts) {
      const card = element("div", "concept");
      const top = element("div", "concept-top");
      top.append(
        element("span", "concept-name", String(concept.name || concept.id || t("answer.unnamed"))),
        element("span", "type-pill", String(concept.type || "concept"))
      );
      card.append(top);
      if (concept.definition) {
        card.append(element("div", "concept-definition", String(concept.definition)));
      }
      if (Array.isArray(concept.aliases) && concept.aliases.length) {
        card.append(element("div", "aliases", t("answer.aliases", { names: concept.aliases.join("、") })));
      }
      list.append(card);
    }
    bubble.append(list);
  }

  if (relations.length) {
    const names = new Map(concepts.map((item) => [item.id, item.name || item.id]));
    const list = element("div", "relation-list");
    for (const relation of relations) {
      const row = element("div", "relation");
      row.append(
        element("span", "mono", displayName(relation.source, names)),
        element("span", "arrow", "→"),
        element("b", "", String(relation.type || "relation")),
        element("span", "arrow", "→"),
        element("span", "mono", displayName(relation.target, names))
      );
      list.append(row);
    }
    bubble.append(list);
  }

  const footer = element("div", "answer-footer");
  footer.textContent = [
    t("answer.ontologyFooter", { id: result.ontology_id || state.info?.ontology_id || "-" }),
    t("answer.versionFooter", { version: shortVersion(result.version) }),
    t("answer.stepsFooter", { count: Number(data.steps || 0) }),
  ].join(" · ");
  bubble.append(footer, detailsFor(data));
}

function renderError(bubble, message, data) {
  bubble.__obError = { message, data };
  bubble.replaceChildren();
  const head = element("div", "answer-head");
  head.append(element("strong", "", t("error.heading")), statusPill(t("status.pillError"), true));
  bubble.append(head, notice(message, "error"));
  if (data) bubble.append(detailsFor(data));
}

function renderResponse(bubble, data) {
  bubble.__obResponse = data;
  bubble.__obRecovery = false;
  const result = data.result || {};
  if (data.conversation_id) {
    bubble.replaceChildren();
    bubble.append(element("strong", "", data.message || result.question || t("response.draftResult")));
    if (data.status === "failed") {
      const error = result.error || {};
      bubble.append(notice(error.message || t("response.failed"), "error"));
    }
    if (result.kind === "ontology_answer") {
      renderOntologyAnswer(bubble, result, data);
      bubble.prepend(notice(t("response.currentDraft", { revision: result.draft_revision }), ""));
    }
    const diff = result.review?.diff || (result.kind === "draft_diff" ? result : null);
    if (result.kind === "schema_proposal") {
      for (const [label, text] of [[t("schema.agree"), t("schema.agreeMessage")], [t("schema.disagree"), t("schema.disagreeMessage")]]) {
        const button = element("button", "review-confirm", label);
        button.type = "button";
        button.addEventListener("click", () => submitMessage(text, result.proposal.id));
        bubble.append(button);
      }
    }
    if (diff) {
      const list = element("div", "review-changes");
      for (const change of diff.schema_changes || []) {
        const card = element("div", "concept");
        card.append(element("strong", "", t("schema.extension")));
        card.append(element("p", "", change.reason));
        card.append(element("pre", "", JSON.stringify(change.additions, null, 2)));
        list.append(card);
      }
      const verbs = { add: t("change.add"), update: t("change.update"), delete: t("change.delete") };
      for (const change of diff.changes || []) {
        const record = change.after || change.before || {};
        const card = element("div", "concept");
        card.append(element("strong", "", `${verbs[change.op.split("_")[0]] || change.op}：${record.name || record.id}`));
        const before = change.before || {}, after = change.after || {};
        for (const key of ["name", "definition", "aliases", "type", "source", "target", "constraints", "application_conditions"]) {
          if (JSON.stringify(before[key]) !== JSON.stringify(after[key])) {
            card.append(element("p", "", `${key}：${JSON.stringify(before[key] ?? t("change.none"))} → ${JSON.stringify(after[key] ?? t("change.deleted"))}`));
          }
        }
        const refs = change.after?.field_evidence
          ? Object.values(change.after.field_evidence).flat() : change.instruction_refs || [];
        const unique = new Map(refs.map((ref) => [JSON.stringify(ref), ref]));
        for (const ref of unique.values()) card.append(element("blockquote", "", t("change.userSource", { quote: ref.quote })));
        list.append(card);
      }
      bubble.append(list, notice(t("validation.message", {
        result: t(diff.validation?.passed ? "validation.passed" : "validation.failed"),
      }), ""));
    }
    if (result.review) {
      const packet = result.review;
      bubble.append(notice(t("review.pending", { revision: packet.draft_revision, expires: packet.expires_at }), ""));
      const confirm = element("button", "review-confirm", t("review.confirm"));
      confirm.type = "button";
      confirm.addEventListener("click", () => submitMessage("确认保存", packet.id));
      bubble.append(confirm);
    }
    if (data.draft) bubble.append(element("p", "answer-footer", t("response.draftFooter", { revision: data.draft.revision, status: data.draft.status })));
    bubble.append(detailsFor(data));
    return;
  }
  if (data.status === "waiting_input") {
    renderClarification(bubble, result, data);
    return;
  }
  if (["failed", "cancelled", "budget_exhausted"].includes(data.status)) {
    const detail = data.error || result.error || {};
    renderError(bubble, detail.message || t("status.runState", { status: data.status }), data);
    return;
  }
  if (result.kind === "clarification") {
    renderClarification(bubble, result, data);
    return;
  }
  if (result.kind === "ontology_answer") {
    renderOntologyAnswer(bubble, result, data);
    return;
  }
  renderError(bubble, t("response.unsupported", { kind: data.result_kind || result.kind || "unknown" }), data);
}

function apiErrorMessage(data, fallback) {
  if (data && data.error && data.error.message) return data.error.message;
  if (data && Array.isArray(data.detail)) {
    return data.detail.map((item) => item.msg || String(item)).join("；");
  }
  if (data && data.detail) return String(data.detail);
  return fallback;
}

async function submitMessage(rawText, replyTo = null) {
  const text = String(rawText || "").trim();
  if (!text || state.busy) return;
  if (state.info?.editable) {
    if (state.pending) {
      setStatusKey("status.pendingRecovery");
      return;
    }
    els.input.value = "";
    autoGrow();
    addUserMessage(text);
    await sendConversation(text, addAssistantMessage(), replyTo);
    return;
  }

  els.input.value = "";
  autoGrow();
  addUserMessage(text);
  const bubble = addAssistantMessage();
  showTyping(bubble);
  setBusy(true);
  setStatusKey("status.querying");

  const payload = {
    message: text,
    planner: els.planner.value,
    use_memory: els.memory.checked,
  };
  if (state.runId) payload.run_id = state.runId;

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const raw = await response.text();
    let data = {};
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (_error) {
      data = {};
    }
    if (!response.ok) {
      throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
    }
    renderResponse(bubble, data);
    state.runId = data.status === "waiting_input" ? data.run_id : null;
    setStatusKey(
      data.status === "waiting_input" ? "status.waitingInput" : "status.readonlyConnected"
    );
  } catch (error) {
    state.runId = null;
    renderError(bubble, error instanceof Error ? error.message : String(error));
    setStatusKey("status.requestFailed");
  } finally {
    setBusy(false);
    els.input.focus();
    scrollToBottom();
  }
}

function applyMode() {
  const editable = Boolean(state.info?.editable);
  const footerLabel = document.querySelector(".sidebar-footer [data-i18n]");
  if (footerLabel) footerLabel.textContent = t(editable ? "sidebar.footerEditable" : "sidebar.footer");
  else document.querySelector(".sidebar-footer").textContent = t(editable ? "sidebar.footerEditable" : "sidebar.footer");
  els.welcome.querySelector("h2").textContent = t(editable ? "welcome.titleEditable" : "welcome.title");
  els.welcome.querySelector("p").textContent = t(editable ? "welcome.bodyEditable" : "welcome.body");
  els.input.placeholder = t(editable ? "composer.placeholderEditable" : "composer.placeholder");
  document.querySelector(".hint").textContent = t(editable ? "hint.editable" : "hint");
}

function syncInfo() {
  if (!state.info) return;
  $("ontology-id").textContent = state.info.ontology_id || "-";
  $("concept-count").textContent = String(state.info.concept_count ?? "-");
  $("relation-count").textContent = String(state.info.relation_count ?? "-");
  $("version-label").textContent = shortVersion(state.info.version);
  $("version-label").title = state.info.version || "";
}

function syncOntologyMeta() {
  const meta = $("ontology-meta");
  if (state.info) {
    meta.textContent = [state.info.model_provider || "model", state.info.model || ""].filter(Boolean).join(" · ");
  } else if (state.connection && !state.connection.ok) {
    meta.textContent = t("status.serverHint");
  }
}

async function loadInfo() {
  try {
    const response = await fetch("/api/info", { cache: "no-store" });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
    }
    state.info = data;
    syncInfo();
    $("ontology-meta").textContent = [
      data.model_provider || "model",
      data.model || "",
    ].filter(Boolean).join(" · ");
    if (data.planner) els.planner.value = data.planner;
    applyMode();
    if (data.editable) {
      els.memory.disabled = true;
      const stored = localStorage.getItem(conversationStorageKey());
      if (stored) {
        try {
          state.conversationId = stored;
          const conv = await conversationFetch(`/api/conversations/${stored}`);
          state.revision = conv.revision;
          state.reviewId = conv.pending_review_id;
          const messages = await conversationFetch(`/api/conversations/${stored}/messages`);
          for (const message of messages) {
            if (message.role === "user") addUserMessage(message.text);
            else if (message.response) renderResponse(addAssistantMessage(), message.response);
          }
          if (conv.pending_review_id) await conversationFetch(`/api/conversations/${stored}/reviews/${conv.pending_review_id}/present`, {});
          state.pending = JSON.parse(localStorage.getItem(`${conversationStorageKey()}:pending`) || "null");
          if (conv.active_turn_id || state.pending) showRecovery();
        } catch (_error) {
          state.conversationId = null;
          localStorage.removeItem(conversationStorageKey());
          setStatusKey("status.oldConversation");
        }
      }
    }
    setConnection(true, "connection.connected");
    syncOntologyMeta();
    document.dispatchEvent(new Event("ontology:changed"));
    if (data.editable) {
      setStatusKey(state.conversationId ? "status.editableRestored" : "status.editablePending");
    } else {
      setStatusKey("status.readonlyCount", { concepts: data.concept_count ?? 0 });
    }
  } catch (error) {
    setConnection(false, "connection.disconnected");
    setStatus(error instanceof Error ? error.message : t("status.connectionFailed"));
    syncOntologyMeta();
  }
}

function clearChat() {
  if (state.busy) return;
  if (state.info?.editable) {
    localStorage.removeItem(conversationStorageKey());
    localStorage.removeItem(`${conversationStorageKey()}:pending`);
    state.conversationId = null;
    state.pending = null;
    state.reviewId = null;
    state.revision = 0;
  }
  state.runId = null;
  for (const child of [...els.messages.children]) {
    if (child.id !== "welcome") child.remove();
  }
  els.welcome.hidden = false;
  document.dispatchEvent(new Event("ontology:changed"));
  if (state.info?.editable) setStatusKey("status.newSession");
  else if (state.info) setStatusKey("status.readonlyCount", { concepts: state.info.concept_count ?? 0 });
  else setStatusKey("status.newChat");
  els.input.focus();
}

els.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  submitMessage(els.input.value);
});

els.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    els.composer.requestSubmit();
  }
});
els.input.addEventListener("input", autoGrow);

document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => submitMessage(button.dataset.prompt));
});

$("new-chat").addEventListener("click", clearChat);
els.menu.addEventListener("click", () => {
  const open = els.sidebar.classList.toggle("open");
  els.backdrop.hidden = !open;
});
els.backdrop.addEventListener("click", () => {
  els.sidebar.classList.remove("open");
  els.backdrop.hidden = true;
});

function conversationStorageKey() {
  return `ob-harness:${state.info.workspace}:${state.info.ontology_id}`;
}

async function conversationFetch(path, payload) {
  const options = payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Harness-CSRF": state.info.csrf_token },
    body: JSON.stringify(payload),
  };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(apiErrorMessage(data, `HTTP ${response.status}`));
  return data;
}

function renderRecovery(bubble) {
  bubble.replaceChildren();
  bubble.append(notice(t("recovery.notice"), ""));
  const retry = element("button", "review-confirm", t("recovery.action"));
  retry.type = "button";
  retry.addEventListener("click", () => sendConversation(null, bubble));
  bubble.append(retry);
}

function showRecovery() {
  const bubble = addAssistantMessage();
  bubble.__obRecovery = true;
  renderRecovery(bubble);
}

async function sendConversation(text, bubble, replyTo) {
  if (state.busy) return;
  setBusy(true);
  showTyping(bubble);
  try {
    if (!state.conversationId) {
      const conv = await conversationFetch("/api/conversations", {});
      state.conversationId = conv.id;
      state.revision = conv.revision;
      localStorage.setItem(conversationStorageKey(), conv.id);
    }
    if (text !== null) {
      state.pending = { message: text, client_message_id: crypto.randomUUID(),
        expected_revision: state.revision, planner: els.planner.value,
        reply_to: replyTo || (text === "确认保存" ? state.reviewId : null) };
      localStorage.setItem(`${conversationStorageKey()}:pending`, JSON.stringify(state.pending));
    }
    const data = state.pending
      ? await conversationFetch(`/api/conversations/${state.conversationId}/messages`, state.pending)
      : await conversationFetch(`/api/conversations/${state.conversationId}/resume`, {});
    state.pending = null;
    localStorage.removeItem(`${conversationStorageKey()}:pending`);
    state.revision = data.conversation_revision;
    document.dispatchEvent(new Event("ontology:changed"));
    state.reviewId = data.result?.review?.id || null;
    renderResponse(bubble, data);
    if (state.reviewId) await conversationFetch(`/api/conversations/${state.conversationId}/reviews/${state.reviewId}/present`, {});
    setStatusKey("status.draft", { revision: data.draft?.revision ?? 0, status: data.status });
  } catch (error) {
    renderError(bubble, error instanceof Error ? error.message : String(error));
    showRecovery();
  } finally {
    setBusy(false);
    els.input.focus();
    scrollToBottom();
  }
}

function onLanguageChanged() {
  applyMode();
  syncInfo();
  if (state.connection) setConnection(state.connection.ok, state.connection.key);
  if (state.status) setStatusKey(state.status.key, state.status.params);
  syncOntologyMeta();
  setBusy(state.busy);
  for (const avatar of document.querySelectorAll(".message.user .avatar")) avatar.textContent = t("common.you");
  for (const bubble of document.querySelectorAll(".message.assistant .bubble")) {
    if (bubble.__obResponse) renderResponse(bubble, bubble.__obResponse);
    else if (bubble.__obRecovery) renderRecovery(bubble);
    else if (bubble.__obError) renderError(bubble, bubble.__obError.message, bubble.__obError.data);
  }
}

document.addEventListener("language:changed", onLanguageChanged);

loadInfo();
autoGrow();
