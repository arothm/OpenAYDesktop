"use strict";
const $ = (s) => document.querySelector(s);
const ICON = {
  send: "↑", stop: "■", copy: "⧉", redo: "↻", edit: "✎", trash: "🗑",
};
const state = {
  config: {}, models: [], modelErrors: [], chats: [], chat: null,
  streaming: null, attachments: [], kbIndex: -1, draftFolders: [],
  approval: null, modelApproval: null, navSeq: 0,
  clearKey: false, updateUrl: "", sessionDraft: null,
  es: null, runStatus: "idle", runs: {}, editIndex: null, usage: null, today: null,
};
const DEFAULT_CAPABILITIES = { editMode: "review", fileManage: false, deleteFiles: false, modelControl: "ask", webSearch: false, subtasks: false };
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
const humanBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 2600); }
function linkify(s) { return esc(s).replace(/https?:\/\/[^\s)]+/g, (u) => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`); }
function renderMarkdown(text) {
  if (!window.marked || !window.DOMPurify) return `<p style="white-space:pre-wrap">${esc(text)}</p>`;
  const html = DOMPurify.sanitize(marked.parse(text, { gfm: true, breaks: false }));
  const div = document.createElement("div"); div.innerHTML = html;
  div.querySelectorAll("a").forEach((a) => { a.target = "_blank"; a.rel = "noopener"; });
  div.querySelectorAll("pre > code").forEach((code) => {
    if (window.hljs) { try { hljs.highlightElement(code); } catch {} }
    const b = document.createElement("button"); b.className = "copy-code"; b.textContent = "Copy"; code.parentElement.appendChild(b);
  }); return div.innerHTML;
}

// Configuration and models
async function loadConfig() {
  state.config = await api("/api/config"); $("#thinkSel").value = state.config.think || "default";
  $("#connBadge").textContent = "Ollama Cloud"; updateFolderBtn();
}
async function loadModels() {
  try { const r = await api("/api/models"); state.models = r.models; state.modelErrors = r.errors; }
  catch (e) { state.models = []; state.modelErrors = [e.message]; }
  if (!currentModel()) {
    const preferred = state.config.defaultModel || "";
    setModel(state.models.find((m) => m.name === preferred)?.name || state.models[0]?.name || preferred);
  } else updateModelBtn();
  renderModelOptions(); if ($("#modalBack").classList.contains("open")) populateDefaultModels();
}
function currentModel() { return state.chat?.model || state.selectedModel || ""; }
function setModel(name) {
  if (!state.chat) { state.selectedModel = name; updateModelBtn(); return; }
  if (busy()) { toast("Wait for the reply to finish before changing the model"); return; }
  const chat = state.chat; const previous = chat.model; chat.model = name; state.selectedModel = name; updateModelBtn();
  api("/api/session/model", { method: "POST", body: JSON.stringify({ chatId: chat.id, model: name }) })
    .then(() => { const meta = state.chats.find((c) => c.id === chat.id); if (meta) meta.model = name; })
    .catch((e) => { toast(e.message); chat.model = previous; if (state.chat === chat) { state.selectedModel = previous; updateModelBtn(); } });
}
function updateModelBtn() { const m = currentModel(); $("#modelBtn .name").innerHTML = m ? esc(m) + ' <span class="tag cloud">cloud</span>' : "Choose a model"; }
function renderModelOptions() {
  const q = $("#modelFilter").value.trim().toLowerCase(); const list = state.models.filter((m) => m.name.toLowerCase().includes(q));
  let html = state.modelErrors.map((e) => `<div class="menu-note">${esc(e)}</div>`).join("");
  list.forEach((m, i) => { html += `<div class="opt ${m.name === currentModel() ? "sel" : ""} ${i === state.kbIndex ? "kb" : ""}" data-name="${esc(m.name)}" data-i="${i}"><span class="n">${esc(m.name)}</span><span class="tag cloud">cloud</span></div>`; });
  if (!list.length && !state.modelErrors.length) html += '<div class="menu-note" style="color:var(--muted)">No cloud models match.</div>';
  $("#modelOptions").innerHTML = html; const kb = $("#modelOptions .opt.kb"); if (kb) kb.scrollIntoView({ block: "nearest" });
}
function openModelMenu(open) {
  $("#modelMenu").classList.toggle("open", open);
  if (open) { $("#modelFilter").value = ""; state.kbIndex = -1; renderModelOptions(); $("#modelFilter").focus(); loadModels(); }
}

// Chats. The server owns each chat's messages and runs its replies (runs.py); the window
// shows them through a live event stream and sends user actions as requests.
async function loadChats() { state.chats = await api("/api/chats").catch(() => []); renderChatList(); }
function chatOrder(q) {
  if (q) return state.chats.filter((c) => c.title.toLowerCase().includes(q));  // flat while searching
  const ids = new Set(state.chats.map((c) => c.id)); const out = [];
  for (const c of state.chats) {
    if (c.parentId && ids.has(c.parentId)) continue;  // shown under its parent
    out.push(c); for (const k of state.chats) if (k.parentId === c.id) out.push(k);
  }
  return out;
}
function renderChatList() {
  const q = $("#search").value.trim().toLowerCase(); const items = chatOrder(q);
  $("#chatList").innerHTML = items.map((c) => {
    const run = state.runs[c.id];
    const badge = run === "waiting" ? '<span class="run-dot waiting" title="Waiting for your approval"></span>'
      : run === "paused" ? '<span class="run-dot paused" title="Paused: low Ollama balance"></span>' : run ? '<span class="run-dot" title="Replying…"></span>' : "";
    return `<div class="chat-item ${state.chat?.id === c.id ? "active" : ""} ${c.parentId ? "child" : ""}" data-id="${c.id}" title="${esc(c.model || "")}">${badge}<span class="t">${esc(c.title)}</span><button class="icon-btn del" title="Delete chat">${ICON.trash}</button></div>`;
  }).join("") || `<div style="color:var(--muted);font-size:13px;padding:8px 10px">${q ? "No matches" : "No chats yet"}</div>`;
}
function titleFor(chat) {
  const parent = chat.parentId && state.chats.find((c) => c.id === chat.parentId);
  return parent ? `${chat.title}  ·  subtask of ${parent.title}` : chat.title;
}
function updateChatMeta(chat) {
  const i = state.chats.findIndex((x) => x.id === chat.id); const meta = { id: chat.id, title: chat.title, updated: chat.updated || Date.now(), model: chat.model };
  if (i >= 0) state.chats.splice(i, 1); state.chats.unshift(meta); state.chats.sort((a, b) => b.updated - a.updated); renderChatList();
}
const busy = () => ["running", "waiting", "paused"].includes(state.runStatus);
async function pollRuns() {
  try {
    const r = await api("/api/runs"); const before = JSON.stringify(state.runs); state.runs = r.runs || {};
    if (JSON.stringify(state.runs) !== before) { renderChatList(); if (Object.keys(state.runs).length === 0) loadChats(); }
  } catch {}
}

// Live events for the open chat (see runs.py for the event types)
function subscribe(id) {
  if (state.es) { state.es.close(); state.es = null; }
  if (!id) return;
  const es = new EventSource(`/api/chats/${encodeURIComponent(id)}/events`); state.es = es;
  es.onmessage = (e) => { if (state.es !== es) return; let ev; try { ev = JSON.parse(e.data); } catch { return; } applyEvent(id, ev); };
}
let liveFrame = 0;
function scheduleLive() { if (!liveFrame) liveFrame = requestAnimationFrame(() => { liveFrame = 0; const nb = nearBottom(); renderLive(); if (nb) scrollBottom(true); }); }
function applyEvent(id, ev) {
  if (ev.type === "gone") { if (state.chat?.id === id) { toast("This chat was deleted"); newChat(); } return; }
  if (ev.type === "snapshot") {
    if (ev.chat.id !== id) return;
    const first = state.chat?.id !== id; state.chat = ev.chat; state.selectedModel = ev.chat.model;
    state.pause = ev.pause || null; renderPause();
    setRunStatus(ev.status); showApproval(ev.approval); updateModelBtn(); updateFolderBtn(); updateUntrustedBadge(); $("#title").textContent = titleFor(ev.chat);
    renderMessages(); scrollBottom(first); renderUsage(); return;
  }
  const chat = state.chat; if (!chat || chat.id !== id) return;
  switch (ev.type) {
    case "message": {
      const isNew = ev.index >= chat.messages.length; chat.messages[ev.index] = ev.message;
      if (isNew || ev.index !== chat.messages.length - 1) { const nb = nearBottom(); renderMessages(); if (nb) scrollBottom(true); } else scheduleLive();
      break;
    }
    case "delta": {
      const m = chat.messages[ev.index]; if (!m) return;
      m.content = (m.content || "") + (ev.content || ""); m.thinking = (m.thinking || "") + (ev.thinking || ""); scheduleLive(); break;
    }
    case "status":
      setRunStatus(ev.status);
      if (ev.model && ev.model !== chat.model) { chat.model = ev.model; state.selectedModel = ev.model; updateModelBtn(); const meta = state.chats.find((c) => c.id === chat.id); if (meta) meta.model = ev.model; }
      if (ev.status === "idle") { loadChats(); renderMessages(); }
      break;
    case "approval": showApproval(ev.approval); break;
    case "approval_done": closeApproval(ev.id); break;
    case "untrusted": chat.untrusted = ev.untrusted; updateUntrustedBadge(); break;
    case "balance_wait": state.pause = ev.balance ? { balance: ev.balance, minimumUsd: ev.minimumUsd } : null; if (ev.balance) state.balance = { balance: ev.balance, minimumUsd: ev.minimumUsd }; renderPause(); renderUsage(); break;
    case "context": state.contextInfo = ev; break;
    case "usage": chat.usage = ev.session; chat.context = ev.context; state.today = ev.today; renderUsage(); break;
  }
}
function setRunStatus(status) {
  state.runStatus = status || "idle"; updateSendBtn();
  if (state.chat) { if (busy()) state.runs[state.chat.id] = state.runStatus; else delete state.runs[state.chat.id]; renderChatList(); }
}

// Token usage and context, shown under the message box
const kTokens = (n) => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n || 0);
function renderUsage() {
  const parts = []; const c = state.chat?.context; const u = state.chat?.usage; const t = state.today;
  const b = state.balance?.balance; const min = state.balance?.minimumUsd || 0;
  if (b && "totalUsd" in b) {
    const cls = min && b.totalUsd <= min ? "low" : min && b.totalUsd <= min + 0.5 ? "warn" : "";
    const tip = `Ollama balance: $${b.purchasedUsd.toFixed(2)} purchased + $${b.includedUsd.toFixed(2)} plan allowance` + (min ? `. Replies pause at $${min.toFixed(2)}.` : ".");
    parts.push(`<span class="${cls}" title="${esc(tip)}">Balance $${b.totalUsd.toFixed(2)}</span>`);
  } else if (b && b.error && min) parts.push(`<span class="warn" title="${esc(b.error)}">Balance unavailable</span>`);
  if (c && c.limit) {
    const pct = Math.round((c.used / c.limit) * 100);
    parts.push(`<span class="${pct >= 80 ? "warn" : ""}" title="Tokens in the last request, out of this model's context window">Context ${kTokens(c.used)} / ${kTokens(c.limit)} (${pct}%)</span>`);
    if (c.omittedMessages) parts.push(`<span class="warn" title="Older messages are left out of requests so the conversation fits">${c.omittedMessages} older message${c.omittedMessages > 1 ? "s" : ""} left out</span>`);
  }
  if (u) parts.push(`<span title="Tokens sent and generated in this chat">This chat ${kTokens(u.promptTokens)} in · ${kTokens(u.outputTokens)} out</span>`);
  if (t) {
    const used = t.promptTokens + t.outputTokens; const lim = state.limits?.dailyTokenLimit || 0;
    const of = lim ? ` / ${kTokens(lim)}` : ""; const warn = lim && used >= lim * 0.8 ? "warn" : "";
    parts.push(`<span class="${warn}" title="All chats today${lim ? ", against your daily limit" : ""}">Today ${kTokens(used)}${of} tokens · ${t.requests} requests</span>`);
  }
  $("#usageLine").innerHTML = parts.join('<span class="sep">·</span>');
}
async function loadUsage() { try { const u = await api("/api/usage"); state.today = u.today; state.limits = u.limits; renderUsage(); } catch {} }

function newChat() {
  state.navSeq++; subscribe(null); state.chat = null; state.pause = null; renderPause(); state.draftFolders = []; state.editIndex = null; closeApproval(); setRunStatus("idle");
  state.selectedModel = state.config.defaultModel || state.models[0]?.name || "";
  renderMessages(); renderChatList(); updateModelBtn(); updateFolderBtn(); updateUntrustedBadge(); toggleFolderPop(false); renderUsage(); $("#title").textContent = ""; $("#input").focus();
}
async function openChat(id) {
  const ticket = ++state.navSeq; let chat;
  try { chat = await api("/api/chats/" + id); } catch (e) { toast(e.message); return; }
  if (ticket !== state.navSeq) return;
  closeApproval(); state.pause = null; renderPause(); state.editIndex = null; state.chat = chat; state.selectedModel = chat.model; setRunStatus(state.runs[id] || "idle");
  updateModelBtn(); renderMessages(); renderChatList(); updateFolderBtn(); updateUntrustedBadge(); toggleFolderPop(false); renderUsage(); $("#title").textContent = titleFor(chat); scrollBottom(true);
  subscribe(id);  // the snapshot that follows brings any live run state
}
async function deleteChat(id) {
  const running = state.runs[id] ? "\n\nA reply is running in this chat; it will be stopped." : "";
  const kids = state.chats.filter((c) => c.parentId === id).length;
  if (!confirm("Delete this chat?" + running + (kids ? `\n\nIts ${kids} subtask chat${kids > 1 ? "s" : ""} will be deleted too.` : ""))) return;
  try { await api("/api/chats/" + id, { method: "DELETE" }); } catch (e) { toast("Delete failed: " + e.message); return; }
  const gone = new Set([id, ...state.chats.filter((c) => c.parentId === id).map((c) => c.id)]);
  gone.forEach((g) => delete state.runs[g]); state.chats = state.chats.filter((c) => !gone.has(c.id)); if (gone.has(state.chat?.id)) newChat(); else renderChatList();
}

// Rendering
const TOOL_LABEL = { list_directory: "Listed folder", read_file: "Read file", search_files: "Searched files", write_file: "Wrote file", edit_file: "Edited file", create_folder: "Created folder", copy_path: "Copied item", move_path: "Moved/renamed item", delete_path: "Moved to trash", restore_path: "Restored item", set_session_model: "Changed model", web_search: "Searched the web", web_fetch: "Read web page", start_subtask: "Subtask" };
function toolHTML(m, idx) {
  if (m._actionApproval) return `<div class="approve" data-idx="${idx}"><div class="q">${esc(m._actionApproval.title)}</div><div class="detail">Nothing has happened yet. This needs your approval.</div><div class="btns"><button class="btn primary" data-action-review="yes">Review</button><button class="btn" data-action-approve="no">Deny</button></div></div>`;
  if (m._modelApproval) return `<div class="approve" data-idx="${idx}"><div class="q">Switch this session to ${esc(m._modelApproval.model)}?</div><div class="detail">Nothing has changed yet. The model is waiting for your approval.</div><div class="btns"><button class="btn primary" data-model-review="yes">Review change</button><button class="btn" data-model-approve="no">Keep current model</button></div></div>`;
  if (m._approval) return `<div class="approve" data-idx="${idx}"><div class="q">Review requested change to ${esc(m._approval.path)}</div><div class="detail">Nothing has been written. Review the complete change before deciding.</div><div class="btns"><button class="btn primary" data-review="yes">Review change</button><button class="btn" data-approve="no">Deny</button></div></div>`;
  const status = m._running ? "running…" : m.isError ? "failed" : "";
  const open = m.subtaskId ? `<button class="btn open-sub" data-open-chat="${esc(m.subtaskId)}" type="button">Open</button>` : "";
  return `<details class="tool ${m.isError ? "err" : ""}" data-idx="${idx}"><summary><b>${esc(TOOL_LABEL[m.tool_name] || m.tool_name || "Tool")}</b><span class="arg">${esc(m.display || "")}</span><span>${status}</span>${open}</summary><div class="out">${esc(m.content || "")}</div></details>`;
}
function msgHTML(m, idx, isLast) {
  if (m.role === "system") return ""; if (m.role === "tool") return toolHTML(m, idx);
  if (m.role === "user" && m.auto) return `<div class="msg user auto" data-idx="${idx}"><div class="bubble">${esc(m.content)}</div></div>`;
  if (m.role === "user") {
    const imgs = (m.images || []).map((b) => `<img src="data:image/*;base64,${b}">`).join("");
    return `<div class="msg user" data-idx="${idx}">${imgs ? `<div class="imgs">${imgs}</div>` : ""}<div class="bubble">${esc(m.content)}</div><div class="actions"><button class="icon-btn act-copy" title="Copy">${ICON.copy}</button><button class="icon-btn act-edit" title="Edit & resend">${ICON.edit}</button></div></div>`;
  }
  const thinking = m.thinking ? `<details class="thinking" ${m._live && !m.content ? "open" : ""}><summary>${m._live && !m.content ? "Thinking…" : "Thought process"}</summary><div class="tbody">${esc(m.thinking)}</div></details>` : "";
  const stats = m.stats ? `<span>· ${m.stats}</span>` : ""; const body = m.content ? renderMarkdown(m.content) : "";
  if (m.tool_calls?.length && !m._live) return `<div class="msg assistant step" data-idx="${idx}" style="margin-bottom:4px">${thinking}${body ? `<div class="content">${body}</div>` : ""}</div>`;
  return `<div class="msg assistant ${isLast ? "last" : ""}" data-idx="${idx}"><div class="meta"><span>${esc(m.model || "")}</span>${stats}${m.wrapUp ? '<span class="wrapup-tag" title="Low balance: the model summarized its progress before pausing">wrap-up</span>' : ""}</div>${thinking}<div class="content ${m._live ? "cursor" : ""}">${body}</div>${m.notice ? `<div class="notice">${esc(m.notice)}</div>` : ""}${m.error ? `<div class="err">${linkify(m.error)}</div>` : ""}${m._live ? "" : `<div class="actions"><button class="icon-btn act-copy" title="Copy">${ICON.copy}</button>${isLast ? `<button class="icon-btn act-regen" title="Regenerate">${ICON.redo}</button>` : ""}</div>`}</div>`;
}
function renderMessages() {
  const box = $("#messages"); const msgs = state.chat?.messages || [];
  if (!msgs.filter((m) => m.role !== "system").length) { const m = currentModel(); box.innerHTML = `<div id="empty"><h1>What can I help with?</h1><div>${m ? "Chatting with " + esc(m) : "Pick a cloud model to start"}</div></div>`; return; }
  const lastIdx = msgs.length - 1; box.innerHTML = msgs.map((m, i) => msgHTML(m, i, i === lastIdx)).join("");
}
function renderLive() {
  const msgs = state.chat.messages; const i = msgs.length - 1; const el = $(`#messages .msg[data-idx="${i}"]`); const html = msgHTML(msgs[i], i, true);
  if (el) el.outerHTML = html; else renderMessages(); const tb = $(`#messages .msg[data-idx="${i}"] .tbody`); if (tb) tb.scrollTop = tb.scrollHeight;
}
function nearBottom() { const s = $("#scroller"); return s.scrollHeight - s.scrollTop - s.clientHeight < 120; }
function scrollBottom(force) { const s = $("#scroller"); if (force || nearBottom()) s.scrollTop = s.scrollHeight; }
function updateSendBtn() {
  const b = $("#sendBtn");
  if (busy()) { b.textContent = ICON.stop; b.title = "Stop"; b.disabled = false; }
  else { b.textContent = ICON.send; b.title = "Send (Enter)"; b.disabled = !$("#input").value.trim() && !state.attachments.length; }
}

// Sending: the server saves the message and runs the reply
function thinkOption() { const t = $("#thinkSel").value; return t === "default" ? null : t === "true" ? true : t === "false" ? false : t; }
async function send() {
  const text = $("#input").value.trim(); if (busy() || (!text && !state.attachments.length)) return;
  const model = currentModel(); if (!model) { toast("Pick a cloud model first"); openModelMenu(true); return; }
  const images = state.attachments.slice(); const truncateAt = state.editIndex;
  try {
    if (!state.chat) {
      const chat = await api("/api/chats", { method: "POST", body: JSON.stringify({ title: (text || "Image").slice(0, 60).replace(/\s+/g, " "), model, folders: state.draftFolders }) });
      state.draftFolders = []; state.chat = chat; $("#title").textContent = chat.title; updateChatMeta(chat); subscribe(chat.id);
    }
    const chat = state.chat; setRunStatus("running");
    $("#input").value = ""; autoGrow(); state.attachments = []; renderAttachments(); state.editIndex = null; updateEditBanner();
    await api("/api/runs", { method: "POST", body: JSON.stringify({ chatId: chat.id, text, images, truncateAt, think: thinkOption() }) });
  } catch (e) {
    toast(e.message); setRunStatus("idle");
    if (!$("#input").value) { $("#input").value = text; state.attachments = images; renderAttachments(); autoGrow(); }
  }
}
function stop() { if (state.chat && busy()) api("/api/runs/stop", { method: "POST", body: JSON.stringify({ chatId: state.chat.id }) }).catch((e) => toast(e.message)); }
async function regenerate() {
  if (busy() || !state.chat) return; setRunStatus("running");
  try { await api("/api/runs", { method: "POST", body: JSON.stringify({ chatId: state.chat.id, regenerate: true, think: thinkOption() }) }); }
  catch (e) { toast(e.message); setRunStatus("idle"); }
}
function editMessage(idx) {
  if (busy()) return; const m = state.chat.messages[idx];
  $("#input").value = m.content; autoGrow(); state.attachments = (m.images || []).slice(); renderAttachments();
  state.editIndex = idx; updateEditBanner(); $("#input").focus();
}
function updateEditBanner() {
  const b = $("#editBanner"); b.hidden = state.editIndex === null;
}

// Approvals: the run waits on the server until the user answers here (or in another window)
function showApproval(a) {
  if (!a) { closeApproval(); return; }
  if (a.kind === "write") {
    if (state.approval?.id === a.id) return;
    state.approval = { id: a.id, chatId: state.chat.id, approval: a.data, mode: a.data.exists ? "diff" : "content", decided: false }; openApprovalPreview();
  } else if (a.kind === "action") {
    if (state.actionApproval?.id === a.id) return;
    state.actionApproval = { id: a.id, chatId: state.chat.id, decided: false };
    $("#aaTitle").textContent = a.data.title || "Allow this action?"; $("#aaDetail").textContent = a.data.detail || ""; $("#aaWhy").textContent = a.data.why || "";
    $("#actionApproveBack").classList.add("open"); $("#aaAllow").focus();
  } else if (a.kind === "model") {
    if (state.modelApproval?.id === a.id) return; const r = a.data;
    state.modelApproval = { id: a.id, chatId: state.chat.id, decided: false };
    $("#maFrom").textContent = r.previous || "none"; $("#maTo").textContent = r.model; $("#maReason").textContent = r.reason || "No reason given.";
    const caps = (r.capabilities || []).filter((c) => c !== "completion"); $("#maCaps").textContent = caps.length ? `${r.model} supports: ${caps.join(", ")}` : "";
    $("#maWarn").textContent = (r.warnings || []).join(" "); $("#maWarn").hidden = !(r.warnings || []).length;
    $("#modelApproveBack").classList.add("open"); $("#maAllow").focus();
  }
}
function closeApproval(id) {
  if (state.approval && (!id || state.approval.id === id)) { state.approval = null; $("#previewBack").classList.remove("open"); }
  if (state.modelApproval && (!id || state.modelApproval.id === id)) { state.modelApproval = null; $("#modelApproveBack").classList.remove("open"); }
  if (state.actionApproval && (!id || state.actionApproval.id === id)) { state.actionApproval = null; $("#actionApproveBack").classList.remove("open"); }
}
function decideActionApproval(approved) {
  const pending = state.actionApproval; if (!pending || pending.decided) return; pending.decided = true; state.actionApproval = null; $("#actionApproveBack").classList.remove("open");
  answerApproval(pending, approved);
}
function updateUntrustedBadge() { $("#untrustedBadge").hidden = !state.chat?.untrusted; }
function answerApproval(pending, approved) {
  api("/api/runs/approve", { method: "POST", body: JSON.stringify({ chatId: pending.chatId, approvalId: pending.id, approve: Boolean(approved) }) }).catch((e) => toast(e.message));
}
function openApprovalPreview() {
  const pending = state.approval; if (!pending) return; const a = pending.approval;
  $("#previewTitle").textContent = a.tool === "edit_file" ? "Review file edit" : a.exists ? "Review file replacement" : "Review new file";
  $("#previewPath").textContent = a.path; $("#previewDiff").disabled = !a.exists;
  $("#previewContent").classList.toggle("primary", pending.mode === "content"); $("#previewDiff").classList.toggle("primary", pending.mode === "diff");
  $("#previewInfo").textContent = a.exists ? `Overwrite · current ${humanBytes(a.currentBytes)} → replacement ${humanBytes(a.newBytes)} · complete unified diff` : `Create new file · ${humanBytes(a.newBytes)}`;
  const text = pending.mode === "diff" ? a.diff : a.content; $("#previewText").textContent = text || (pending.mode === "content" ? "(empty file)" : "(no differences)");
  $("#previewText").scrollTop = 0; $("#previewText").scrollLeft = 0; $("#previewBack").classList.add("open"); $("#previewAllow").focus();
}
function decideApproval(approved) {
  const pending = state.approval; if (!pending || pending.decided) return; pending.decided = true; state.approval = null; $("#previewBack").classList.remove("open");
  answerApproval(pending, approved);
}
function decideModelApproval(approved) {
  const pending = state.modelApproval; if (!pending || pending.decided) return; pending.decided = true; state.modelApproval = null; $("#modelApproveBack").classList.remove("open");
  answerApproval(pending, approved);
}

// Attachments
function renderAttachments() { $("#attachments").innerHTML = state.attachments.map((b, i) => `<div class="att"><img src="data:image/*;base64,${b}"><button data-i="${i}" title="Remove">×</button></div>`).join(""); updateSendBtn(); }
function addFiles(files) {
  for (const f of files) { if (!f.type.startsWith("image/")) continue; const rd = new FileReader(); rd.onload = () => { state.attachments.push(String(rd.result).split(",")[1]); renderAttachments(); }; rd.readAsDataURL(f); }
}

// Settings and updates
function openSettings() {
  const c = state.config; state.clearKey = false; state.updateUrl = ""; $("#apiKey").value = "";
  $("#apiKey").placeholder = c.hasApiKey ? `Saved ${c.apiKeyHint || "(from OLLAMA_API_KEY)"} - leave blank to keep` : "Paste key…";
  $("#clearKey").disabled = !c.hasApiKey; $("#versionLabel").textContent = `Open AY Desktop ${c.version || ""}`;
  $("#updateStatus").textContent = `Checks github.com/${c.repository || "arothm/OpenAYDesktop"}`; $("#openUpdate").hidden = true;
  $("#dailyLimit").value = c.dailyTokenLimit || 0; $("#chatLimit").value = c.chatTokenLimit || 0; $("#maxRuns").value = c.maxConcurrentRuns || 4; $("#minBalance").value = c.minBalanceUsd ?? 1;
  populateDefaultModels(); $("#modalBack").classList.add("open");
}
function populateDefaultModels() {
  const select = $("#defaultModel"); const selected = state.config.defaultModel || "";
  const names = [...new Set([selected, ...state.models.map((m) => m.name)].filter(Boolean))];
  select.innerHTML = '<option value="">First available cloud model</option>' + names.map((name) => `<option value="${esc(name)}" ${name === selected ? "selected" : ""}>${esc(name)}</option>`).join("");
  select.value = selected; if (!state.models.length && !selected) select.innerHTML = '<option value="">Choose a model after saving your API key</option>';
}
async function checkUpdates() {
  const button = $("#checkUpdates"); button.disabled = true; button.textContent = "Checking…";
  $("#updateStatus").textContent = "Contacting GitHub Releases…"; $("#openUpdate").hidden = true; state.updateUrl = "";
  try {
    const result = await api("/api/updates/check", { method: "POST", body: "{}" }); $("#updateStatus").textContent = result.message;
    if (result.updateAvailable) { state.updateUrl = result.releaseUrl; $("#openUpdate").hidden = false; }
  } catch (e) { $("#updateStatus").textContent = e.message; }
  finally { button.disabled = false; button.textContent = "Check for Updates"; }
}
async function saveSettings() {
  const body = { defaultModel: $("#defaultModel").value, dailyTokenLimit: Number($("#dailyLimit").value || 0),
                 chatTokenLimit: Number($("#chatLimit").value || 0), maxConcurrentRuns: Number($("#maxRuns").value || 4), minBalanceUsd: Number($("#minBalance").value || 0) };
  const key = $("#apiKey").value.trim(); if (key) body.apiKey = key;
  if (state.clearKey && !key) body.clearApiKey = true;
  try {
    const saved = await api("/api/config", { method: "POST", body: JSON.stringify(body) }); const oldDefault = state.config.defaultModel; state.config = saved;
    if (!state.chat && oldDefault !== saved.defaultModel) { state.selectedModel = saved.defaultModel || state.models[0]?.name || ""; renderMessages(); updateModelBtn(); }
    $("#modalBack").classList.remove("open"); await loadModels(); loadUsage(); toast("Settings saved");
  } catch (e) { toast(e.message); }
}

// Session permissions and rename
function openSession() {
  if (!state.chat) { toast("Start a session first, then save its first message"); return; }
  const c = { ...DEFAULT_CAPABILITIES, ...(state.chat.capabilities || {}) };
  const writable = chatFolders().some((f) => f.write);
  state.sessionDraft = { id: state.chat.id, title: state.chat.title, capabilities: c };
  $("#sessionName").value = state.chat.title || "Untitled"; $("#editMode").value = c.editMode;
  $("#fileManage").checked = writable && c.fileManage; $("#deleteFiles").checked = writable && c.deleteFiles;
  $("#fileManage").disabled = !writable; $("#deleteFiles").disabled = !writable; $("#modelControl").value = c.modelControl;
  $("#webSearch").checked = Boolean(c.webSearch);
  const isChild = Boolean(state.chat.parentId); $("#subtasks").checked = !isChild && Boolean(c.subtasks); $("#subtasks").disabled = isChild;
  if (isChild) $("#subtasksHelp").textContent = "This chat is a subtask. Its permissions come from the chat that started it, and it can't start subtasks of its own.";
  const u = state.chat.untrusted; $("#untrustedBox").hidden = !u;
  if (u) $("#untrustedSources").textContent = "Since " + new Date(u.since).toLocaleString() + ". Read: " + (u.sources || []).join("; ");
  $("#sessionSkills").innerHTML = '<span class="help">Loading skills…</span>';
  api("/api/skills").then((r) => {
    const on = new Set(state.chat?.skills || []);
    $("#sessionSkills").innerHTML = r.skills.map((s) => `<label><input type="checkbox" data-skill="${esc(s.id)}" ${on.has(s.id) ? "checked" : ""}> ${esc(s.name)} <span class="d">${esc(s.description)}</span></label>`).join("")
      || '<span class="help" style="margin:0">No skills yet.</span>';
  }).catch((e) => { $("#sessionSkills").textContent = e.message; });
  $("#sessionBack").classList.add("open"); $("#sessionName").focus();
}
async function trustChat() {
  if (!state.chat || !confirm("Trust this chat again?\n\nFile changes and model changes will follow your normal settings again, even though web content is still in the conversation.")) return;
  try { await api("/api/session/trust", { method: "POST", body: JSON.stringify({ chatId: state.chat.id }) }); delete state.chat.untrusted; updateUntrustedBadge(); $("#untrustedBox").hidden = true; toast("This chat is trusted again"); }
  catch (e) { toast(e.message); }
}
async function saveSession() {
  const draft = state.sessionDraft; if (!draft || !state.chat || draft.id !== state.chat.id) return;
  const title = $("#sessionName").value.trim();
  const c = { editMode: $("#editMode").value, fileManage: $("#fileManage").checked,
              deleteFiles: $("#deleteFiles").checked, modelControl: $("#modelControl").value, webSearch: $("#webSearch").checked, subtasks: $("#subtasks").checked && !$("#subtasks").disabled };
  const skillIds = [...document.querySelectorAll("#sessionSkills [data-skill]")].filter((x) => x.checked).map((x) => x.dataset.skill);
  try {
    if (title !== state.chat.title) {
      const result = await api("/api/session/rename", { method: "POST", body: JSON.stringify({ chatId: state.chat.id, title }) });
      state.chat.title = result.title; $("#title").textContent = result.title;
    }
    const changes = { capabilities: c }; if (document.querySelector("#sessionSkills [data-skill]")) changes.skills = skillIds;
    await savePermissions(state.chat, changes); await loadChats();
    $("#sessionBack").classList.remove("open"); toast("Session settings saved");
  } catch (e) { toast(e.message); }
}

// Trash: files the model deleted, and earlier versions kept before every overwrite or edit
async function openTrash() { $("#trashBack").classList.add("open"); await renderTrash(); }
async function renderTrash() {
  let r; try { r = await api("/api/trash"); } catch (e) { $("#trashList").innerHTML = `<div class="menu-note">${esc(e.message)}</div>`; return; }
  const items = r.items || []; $("#trashEmpty").disabled = !items.length;
  $("#trashSummary").textContent = items.length
    ? `${items.length} item${items.length > 1 ? "s" : ""} · ${humanBytes(r.totalBytes || 0)}. Items stay here until you remove them. Restoring an earlier version keeps the current file here too, so a restore can itself be undone.`
    : "Trash is empty. Files a session deletes, and earlier versions of files it edits, appear here.";
  $("#trashList").innerHTML = items.map((m) => {
    const chat = state.chats.find((c) => c.id === m.chatId)?.title;
    const when = m.deletedAt ? new Date(m.deletedAt * 1000).toLocaleString() : "";
    const kind = m.kind === "version" ? '<span class="kind version">Earlier version</span>' : `<span class="kind">Deleted${m.isDir ? " folder" : ""}</span>`;
    return `<div class="trash-row" data-id="${esc(m.id)}">${kind}<div class="info"><div class="p" title="${esc(m.originalPath)}">&lrm;${esc(m.originalPath)}</div>
      <div class="m">${esc(when)} · ${humanBytes(m.size || 0)}${chat ? " · " + esc(chat) : ""}</div></div>
      <button class="btn" data-trash="restore">${m.kind === "version" ? "Restore this version" : "Restore"}</button><button class="btn danger" data-trash="purge" title="Delete permanently">Delete</button></div>`;
  }).join("");
}
$("#trashBtn").addEventListener("click", openTrash);
$("#trashClose").addEventListener("click", () => $("#trashBack").classList.remove("open"));
$("#trashBack").addEventListener("click", (e) => { if (e.target.id === "trashBack") $("#trashBack").classList.remove("open"); });
$("#trashList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-trash]"); if (!b) return; const id = b.closest(".trash-row").dataset.id;
  const path = b.closest(".trash-row").querySelector(".p").title;
  try {
    if (b.dataset.trash === "restore") { const r = await api("/api/trash/restore", { method: "POST", body: JSON.stringify({ id }) }); toast(r.content); }
    else { if (!confirm(`Permanently delete this trash item?\n\n${path}\n\nThis can't be undone.`)) return; await api("/api/trash/purge", { method: "POST", body: JSON.stringify({ ids: [id] }) }); toast("Deleted permanently"); }
  } catch (err) { toast(err.message); }
  renderTrash();
});
$("#trashEmpty").addEventListener("click", async () => {
  if (!confirm("Permanently delete everything in the trash? This can't be undone.")) return;
  try { const r = await api("/api/trash/purge", { method: "POST", body: JSON.stringify({ all: true }) }); toast(`Removed ${r.removed} item${r.removed === 1 ? "" : "s"}`); } catch (e) { toast(e.message); }
  renderTrash();
});

// Activity: running replies in every chat (subtasks included), plus recently finished ones
let activityTimer = 0;
async function openActivity() { $("#activityBack").classList.add("open"); await renderActivity(); clearInterval(activityTimer); activityTimer = setInterval(renderActivity, 2000); }
function closeActivity() { clearInterval(activityTimer); $("#activityBack").classList.remove("open"); }
const ago = (s) => s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
function activityRow(a, live) {
  const parent = a.parentId && state.chats.find((c) => c.id === a.parentId);
  const label = live ? (a.approval ? "waiting for you" : a.status === "paused" ? "paused: low balance" : a.status) : a.status;
  const cls = live ? (a.approval ? "waiting" : a.status === "paused" ? "paused" : "running") : a.status;
  const meta = [a.model, ago(a.seconds), `${a.steps} step${a.steps === 1 ? "" : "s"}`, `${kTokens(a.tokens)} tokens`];
  if (parent) meta.push("subtask of " + parent.title); if (a.children?.length) meta.push(`${a.children.length} subtask${a.children.length > 1 ? "s" : ""}`);
  return `<div class="act-row" data-chat="${esc(a.chatId)}"><span class="st ${cls}">${esc(label)}</span><div class="info"><div class="n">${esc(a.title || "Untitled")}</div><div class="m">${esc(meta.join(" · "))}</div></div>
    <button class="btn" data-act="open" type="button">Open</button>${live ? '<button class="btn danger" data-act="stop" type="button">Stop</button>' : ""}</div>`;
}
async function renderActivity() {
  if (!$("#activityBack").classList.contains("open")) { clearInterval(activityTimer); return; }
  let r; try { r = await api("/api/activity"); } catch (e) { $("#activityList").innerHTML = `<div class="menu-note">${esc(e.message)}</div>`; return; }
  $("#activityList").innerHTML = r.active.map((a) => activityRow(a, true)).join("") || '<div class="help" style="margin:0">Nothing is running.</div>';
  $("#activityRecent").innerHTML = r.recent.map((a) => activityRow(a, false)).join("") || '<div class="help" style="margin:0">Nothing yet since the app started.</div>';
}
$("#activityBtn").addEventListener("click", openActivity);
$("#activityClose").addEventListener("click", closeActivity);
$("#activityBack").addEventListener("click", async (e) => {
  if (e.target.id === "activityBack") { closeActivity(); return; }
  const b = e.target.closest("[data-act]"); if (!b) return; const id = b.closest(".act-row").dataset.chat;
  if (b.dataset.act === "open") { closeActivity(); openChat(id); }
  else { try { await api("/api/runs/stop", { method: "POST", body: JSON.stringify({ chatId: id }) }); } catch (err) { toast(err.message); } renderActivity(); }
});

// Skills: Markdown instructions stored in data/skills, turned on per chat in Session settings
const skillsUi = { list: [], current: null };
async function openSkills() { $("#skillsBack").classList.add("open"); await loadSkills(); if (!skillsUi.current) editSkill(null); }
async function loadSkills() {
  try { skillsUi.list = (await api("/api/skills")).skills; } catch (e) { toast(e.message); skillsUi.list = []; }
  $("#skillsList").innerHTML = skillsUi.list.map((s) => `<button class="skill-item ${skillsUi.current === s.id ? "sel" : ""}" data-skill-id="${esc(s.id)}" type="button"><div class="n">${esc(s.name)}</div><div class="d">${esc(s.description || "")}</div></button>`).join("")
    || '<div class="help" style="margin:0">No skills yet. Create one, or import a Markdown file.</div>';
}
async function editSkill(id) {
  skillsUi.current = id; let s = { name: "", description: "", body: "" };
  if (id) { try { s = await api("/api/skills/" + encodeURIComponent(id)); } catch (e) { toast(e.message); return; } }
  $("#skillName").value = s.name; $("#skillDesc").value = s.description; $("#skillBody").value = s.body; $("#skillDelete").disabled = !id;
  document.querySelectorAll("#skillsList .skill-item").forEach((b) => b.classList.toggle("sel", b.dataset.skillId === id));
}
async function saveSkill() {
  const body = { id: skillsUi.current, name: $("#skillName").value, description: $("#skillDesc").value, body: $("#skillBody").value };
  try { const s = await api("/api/skills", { method: "POST", body: JSON.stringify(body) }); skillsUi.current = s.id; $("#skillDelete").disabled = false; await loadSkills(); toast("Skill saved"); }
  catch (e) { toast(e.message); }
}
async function deleteSkill() {
  if (!skillsUi.current || !confirm("Delete this skill? Chats that use it will stop using it.")) return;
  try { await api("/api/skills/" + encodeURIComponent(skillsUi.current), { method: "DELETE" }); skillsUi.current = null; await loadSkills(); editSkill(null); toast("Skill deleted"); }
  catch (e) { toast(e.message); }
}
function importSkill(file) {
  const rd = new FileReader();
  rd.onload = () => {
    let text = String(rd.result); let name = file.name.replace(/\.(md|markdown|txt)$/i, ""); let description = "";
    const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
    if (m) { for (const line of m[1].split(/\r?\n/)) { const [k, ...v] = line.split(":"); if (/^name$/i.test(k.trim())) name = v.join(":").trim(); if (/^description$/i.test(k.trim())) description = v.join(":").trim(); } text = m[2]; }
    skillsUi.current = null; $("#skillName").value = name; $("#skillDesc").value = description; $("#skillBody").value = text.trim(); $("#skillDelete").disabled = true;
    toast("Imported. Review it, then Save skill.");
  };
  rd.readAsText(file);
}
$("#skillsBtn").addEventListener("click", openSkills);
$("#skillsClose").addEventListener("click", () => $("#skillsBack").classList.remove("open"));
$("#skillsBack").addEventListener("click", (e) => { if (e.target.id === "skillsBack") $("#skillsBack").classList.remove("open"); });
$("#skillsList").addEventListener("click", (e) => { const b = e.target.closest("[data-skill-id]"); if (b) editSkill(b.dataset.skillId); });
$("#skillNew").addEventListener("click", () => editSkill(null)); $("#skillSave").addEventListener("click", saveSkill); $("#skillDelete").addEventListener("click", deleteSkill);
$("#skillImport").addEventListener("click", () => $("#skillFile").click());
$("#skillFile").addEventListener("change", (e) => { if (e.target.files[0]) importSkill(e.target.files[0]); e.target.value = ""; });
$("#trustBtn").addEventListener("click", trustChat);
$("#untrustedBadge").addEventListener("click", openSession);

// Folder access
function chatFolders() { return state.chat ? (state.chat.folders ||= []) : state.draftFolders; }
function renderFolderList() {
  $("#folderList").innerHTML = chatFolders().map((f, i) => `<div class="folder-row" data-i="${i}"><span class="p" title="${esc(f.path)}">&lrm;${esc(f.path)}</span><label><input type="checkbox" class="fw" ${f.write ? "checked" : ""}> Allow edits</label><button class="icon-btn fdel" title="Remove">${ICON.trash}</button></div>`).join("") || '<div class="help" style="margin:0 0 4px">No folders yet, so this chat can\'t see any of your files.</div>';
}
// Folders and permissions are server-owned: normal chat saves can't change them, so they go
// through /api/session/permissions, and the window shows what the server actually stored.
async function savePermissions(chat, changes) {
  const r = await api("/api/session/permissions", { method: "POST", body: JSON.stringify({ chatId: chat.id, ...changes }) });
  chat.folders = r.folders; chat.capabilities = r.capabilities; chat.skills = r.skills; return r;
}
async function foldersChanged() {
  renderFolderList(); updateFolderBtn(); if (!state.chat) return; const chat = state.chat;
  try { await savePermissions(chat, { folders: chat.folders }); }
  catch (e) {
    toast("Folder change was not saved: " + e.message);
    try { chat.folders = (await api("/api/chats/" + chat.id)).folders || []; } catch {}
  }
  if (state.chat === chat) { renderFolderList(); updateFolderBtn(); }
}
function addFolder(raw) {
  const p = raw.trim().replace(/^"|"$/g, ""); if (!p) return; const list = chatFolders();
  if (!list.some((f) => f.path.toLowerCase() === p.toLowerCase())) list.push({ path: p, write: false }); $("#folderPath").value = ""; foldersChanged();
}
function updateFolderBtn() {
  const b = $("#folderBtn"); const n = chatFolders().length; b.classList.toggle("on", n > 0); b.querySelector(".count").textContent = n;
  b.title = n ? `This chat can access ${n} folder${n > 1 ? "s" : ""}` : "Give this chat access to folders";
}
function toggleFolderPop(open) { $("#folderPop").classList.toggle("open", open); if (open) { renderFolderList(); $("#folderPath").focus(); } }

// Events
$("#folderBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleFolderPop(!$("#folderPop").classList.contains("open")); });
document.addEventListener("click", (e) => { if (!e.target.closest("#folderPop, #folderBtn")) toggleFolderPop(false); });
$("#addFolder").addEventListener("click", () => addFolder($("#folderPath").value));
$("#folderPath").addEventListener("keydown", (e) => { if (e.key === "Enter") addFolder($("#folderPath").value); else if (e.key === "Escape") { e.stopPropagation(); toggleFolderPop(false); $("#input").focus(); } });
$("#browseFolder").addEventListener("click", async () => {
  const b = $("#browseFolder"); b.disabled = true; b.textContent = "Choosing…";
  try { const r = await api("/api/pick-folder", { method: "POST" }); if (r.path) addFolder(r.path); } catch (e) { toast(e.message); }
  finally { b.disabled = false; b.textContent = "Browse…"; }
});
$("#folderList").addEventListener("click", (e) => {
  const row = e.target.closest(".folder-row"); if (!row) return; const i = +row.dataset.i; const list = chatFolders();
  if (e.target.closest(".fdel")) { list.splice(i, 1); foldersChanged(); } else if (e.target.classList.contains("fw")) { list[i].write = e.target.checked; foldersChanged(); }
});
function autoGrow() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 240) + "px"; updateSendBtn(); }
$("#input").addEventListener("input", autoGrow);
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("#input").addEventListener("paste", (e) => { const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/")); if (files.length) { e.preventDefault(); addFiles(files); } });
$("#sendBtn").addEventListener("click", () => busy() ? stop() : send()); $("#attachBtn").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
$("#attachments").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { state.attachments.splice(+b.dataset.i, 1); renderAttachments(); } });
let dragDepth = 0;
addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { dragDepth++; $("#drop").classList.add("show"); } });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop").classList.remove("show"); } });
addEventListener("dragover", (e) => e.preventDefault()); addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; $("#drop").classList.remove("show"); addFiles(e.dataTransfer.files); });
$("#newChat").addEventListener("click", newChat); $("#search").addEventListener("input", renderChatList);
$("#chatList").addEventListener("click", (e) => { const item = e.target.closest(".chat-item"); if (!item) return; if (e.target.closest(".del")) deleteChat(item.dataset.id); else openChat(item.dataset.id); });
$("#toggleSide").addEventListener("click", () => $("#sidebar").classList.toggle("hidden"));
$("#modelBtn").addEventListener("click", (e) => { e.stopPropagation(); openModelMenu(!$("#modelMenu").classList.contains("open")); });
$("#modelFilter").addEventListener("input", () => { state.kbIndex = 0; renderModelOptions(); });
$("#modelFilter").addEventListener("keydown", (e) => {
  const opts = [...document.querySelectorAll("#modelOptions .opt")];
  if (e.key === "ArrowDown") { state.kbIndex = Math.min(opts.length - 1, state.kbIndex + 1); renderModelOptions(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { state.kbIndex = Math.max(0, state.kbIndex - 1); renderModelOptions(); e.preventDefault(); }
  else if (e.key === "Enter") { const o = opts[Math.max(0, state.kbIndex)]; if (o) { setModel(o.dataset.name); openModelMenu(false); if (!state.chat) renderMessages(); $("#input").focus(); } }
  else if (e.key === "Escape") { e.stopPropagation(); openModelMenu(false); $("#input").focus(); }
});
$("#modelOptions").addEventListener("click", (e) => { const o = e.target.closest(".opt"); if (!o) return; setModel(o.dataset.name); openModelMenu(false); if (!state.chat) renderMessages(); $("#input").focus(); });
document.addEventListener("click", (e) => { if (!e.target.closest(".picker")) openModelMenu(false); });
$("#thinkSel").addEventListener("change", (e) => api("/api/config", { method: "POST", body: JSON.stringify({ think: e.target.value }) }).catch(() => {}));
$("#messages").addEventListener("click", async (e) => {
  const oc = e.target.closest("[data-open-chat]"); if (oc) { e.preventDefault(); openChat(oc.dataset.openChat); return; }
  if (e.target.closest("[data-action-review]")) { if (state.actionApproval) $("#actionApproveBack").classList.add("open"); return; }
  if (e.target.closest("[data-action-approve]")) { decideActionApproval(false); return; }
  if (e.target.closest("[data-model-review]")) { if (state.modelApproval) $("#modelApproveBack").classList.add("open"); return; }
  if (e.target.closest("[data-model-approve]")) { decideModelApproval(false); return; }
  if (e.target.closest("[data-review]")) { openApprovalPreview(); return; }
  const ap = e.target.closest("[data-approve]"); if (ap) { decideApproval(ap.dataset.approve === "yes"); return; }
  const cc = e.target.closest(".copy-code");
  if (cc) { await navigator.clipboard.writeText(cc.parentElement.querySelector("code").innerText); cc.textContent = "Copied"; setTimeout(() => (cc.textContent = "Copy"), 1200); return; }
  const msgEl = e.target.closest(".msg"); if (!msgEl) return; const idx = +msgEl.dataset.idx; const m = state.chat?.messages[idx];
  if (e.target.closest(".act-copy")) { await navigator.clipboard.writeText(m.content); toast("Copied"); } else if (e.target.closest(".act-regen")) regenerate(); else if (e.target.closest(".act-edit")) editMessage(idx);
});
$("#previewAllow").addEventListener("click", () => decideApproval(true)); $("#previewDeny").addEventListener("click", () => decideApproval(false));
$("#maAllow").addEventListener("click", () => decideModelApproval(true)); $("#maDeny").addEventListener("click", () => decideModelApproval(false));
// "Later" only hides the question; the reply keeps waiting on the server, and the card in the chat reopens it
const hideApprovalDialogs = () => { for (const d of ["#previewBack", "#modelApproveBack", "#actionApproveBack"]) $(d).classList.remove("open"); };
$("#aaLater").addEventListener("click", hideApprovalDialogs); $("#aaDeny").addEventListener("click", () => decideActionApproval(false)); $("#aaAllow").addEventListener("click", () => decideActionApproval(true));
$("#previewLater").addEventListener("click", hideApprovalDialogs); $("#maLater").addEventListener("click", hideApprovalDialogs);
$("#previewDiff").addEventListener("click", () => { if (state.approval) { state.approval.mode = "diff"; openApprovalPreview(); } });
$("#previewContent").addEventListener("click", () => { if (state.approval) { state.approval.mode = "content"; openApprovalPreview(); } });
$("#settingsBtn").addEventListener("click", openSettings); $("#sessionBtn").addEventListener("click", openSession);
$("#cancelSession").addEventListener("click", () => $("#sessionBack").classList.remove("open")); $("#saveSession").addEventListener("click", saveSession);
$("#sessionBack").addEventListener("click", (e) => { if (e.target.id === "sessionBack") $("#sessionBack").classList.remove("open"); });
$("#clearKey").addEventListener("click", () => { state.clearKey = true; $("#apiKey").value = ""; $("#apiKey").placeholder = "Saved key will be cleared when you Save"; $("#clearKey").disabled = true; });
$("#checkUpdates").addEventListener("click", checkUpdates);
$("#openUpdate").addEventListener("click", () => { if (state.updateUrl.startsWith("https://github.com/arothm/OpenAYDesktop/releases/")) window.open(state.updateUrl, "_blank", "noopener"); });
$("#cancelSet").addEventListener("click", () => $("#modalBack").classList.remove("open")); $("#saveSet").addEventListener("click", saveSettings);
$("#modalBack").addEventListener("click", (e) => { if (e.target.id === "modalBack") $("#modalBack").classList.remove("open"); });
addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key.toLowerCase() === "n") { e.preventDefault(); newChat(); }
  else if (e.ctrlKey && e.key.toLowerCase() === "b") { e.preventDefault(); $("#sidebar").classList.toggle("hidden"); }
  else if (e.key === "Escape") {
    if ($("#modelApproveBack").classList.contains("open") || $("#previewBack").classList.contains("open") || $("#actionApproveBack").classList.contains("open")) hideApprovalDialogs(); else if ($("#activityBack").classList.contains("open")) closeActivity(); else if ($("#skillsBack").classList.contains("open")) $("#skillsBack").classList.remove("open"); else if ($("#trashBack").classList.contains("open")) $("#trashBack").classList.remove("open"); else if ($("#sessionBack").classList.contains("open")) $("#sessionBack").classList.remove("open");
    else if ($("#folderPop").classList.contains("open")) toggleFolderPop(false); else if ($("#modalBack").classList.contains("open")) $("#modalBack").classList.remove("open"); else if (state.editIndex !== null) { state.editIndex = null; updateEditBanner(); } else if (busy()) stop();
  }
});
setInterval(() => fetch("/api/ping").catch(() => {}), 5000); fetch("/api/ping").catch(() => {});
setInterval(pollRuns, 3000);
async function loadBalance(force) {
  try { state.balance = await api(force ? "/api/balance/check" : "/api/balance", force ? { method: "POST", body: "{}" } : undefined); renderUsage(); renderPause(); } catch {}
}
setInterval(() => loadBalance(false), 60000);
function renderPause() {
  const p = state.pause; $("#pauseBanner").hidden = !p; if (!p) return;
  const b = p.balance || {}; const min = (p.minimumUsd || 0).toFixed(2);
  $("#pauseText").textContent = "totalUsd" in b
    ? `Your balance is $${b.totalUsd.toFixed(2)}, at or below the $${min} you set. Recharge on ollama.com and this reply continues by itself (it checks about once a minute).`
    : `Waiting for the balance to be above $${min}. ${b.error || ""}`;
}
$("#pauseCheck").addEventListener("click", () => loadBalance(true));
$("#pauseStop").addEventListener("click", stop);
$("#pauseRecharge").addEventListener("click", () => window.open("https://ollama.com/settings", "_blank", "noopener"));
$("#editCancel").addEventListener("click", () => { state.editIndex = null; updateEditBanner(); $("#input").value = ""; state.attachments = []; renderAttachments(); autoGrow(); });
(async function init() {
  updateSendBtn(); await loadConfig().catch((e) => toast(e.message)); await Promise.all([loadModels(), loadChats(), pollRuns(), loadUsage(), loadBalance(false)]); renderMessages();
  if (document.activeElement === document.body) $("#input").focus();
})();
