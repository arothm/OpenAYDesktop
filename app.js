"use strict";
const $ = (s) => document.querySelector(s);
// Icons are <symbol>s in index.html; this draws one (cls: "sm" or "lg" for 14px or 18px).
const ic = (name, cls) => `<svg class="ico${cls ? " " + cls : ""}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const state = {
  config: {}, models: [], modelErrors: [], chats: [], chat: null,
  streaming: null, attachments: [], kbIndex: -1, draftFolders: [],
  approval: null, modelApproval: null, navSeq: 0,
  clearKey: false, updateUrl: "", sessionDraft: null,
  es: null, runStatus: "idle", runs: {}, editIndex: null, usage: null, today: null,
  documents: [], plan: false, projects: [], projectFilter: "", undoRuns: [], searchHits: null,
};
const DEFAULT_CAPABILITIES = { editMode: "review", fileManage: false, deleteFiles: false, modelControl: "ask", webSearch: false, subtasks: false, git: "off", commands: false, instructions: true, memory: true };
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
  if (!window.marked || !window.DOMPurify) return `<p class="pre-wrap">${esc(text)}</p>`;
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
  $("#brandVer").textContent = state.config.version || ""; updateFolderBtn();
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
function updateModelBtn() { const m = currentModel(); $("#modelBtn .name").textContent = m || "Choose a model"; $("#modelBtn .dot").hidden = !m; updateSubtitle(); }
function renderModelOptions() {
  const q = $("#modelFilter").value.trim().toLowerCase(); const list = state.models.filter((m) => m.name.toLowerCase().includes(q));
  let html = state.modelErrors.map((e) => `<div class="menu-note">${esc(e)}</div>`).join("");
  list.forEach((m, i) => { html += `<div class="opt ${m.name === currentModel() ? "sel" : ""} ${i === state.kbIndex ? "kb" : ""}" data-name="${esc(m.name)}" data-i="${i}"><span class="n">${esc(m.name)}</span>${ic("check", "sm")}</div>`; });
  if (!list.length && !state.modelErrors.length) html += '<div class="menu-note quiet">No cloud models match.</div>';
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
  const inProject = (c) => !state.projectFilter || c.projectId === state.projectFilter;
  if (q) {  // flat while searching: title matches at once, then message matches from the server
    const hits = state.searchHits?.q === q ? state.searchHits.results : [];
    const byId = new Map(state.chats.filter((c) => c.title.toLowerCase().includes(q)).map((c) => [c.id, c]));
    for (const h of hits) if (!byId.has(h.id)) byId.set(h.id, h);
    return [...byId.values()].filter(inProject);
  }
  const chats = state.chats.filter(inProject);
  const ids = new Set(chats.map((c) => c.id)); const out = [];
  for (const c of chats) {
    if (c.parentId && ids.has(c.parentId)) continue;  // shown under its parent
    out.push(c); for (const k of chats) if (k.parentId === c.id) out.push(k);
  }
  return out;
}
function renderChatList() {
  const q = $("#search").value.trim().toLowerCase(); const items = chatOrder(q);
  let group = "";
  $("#chatList").innerHTML = items.map((c) => {
    const run = state.runs[c.id];
    const badge = run === "waiting" ? '<span class="dot wait" title="Waiting for your approval"></span>'
      : run === "paused" ? '<span class="dot pause" title="Paused: low Ollama balance"></span>' : run ? '<span class="dot run" title="Replying…"></span>' : "";
    let label = "";
    if (!q && !c.parentId) { const g = dayGroup(c.updated); if (g !== group) { group = g; label = `<div class="list-label">${g}</div>`; } }
    const mark = c.scheduled ? ic("clock", "sm") : "";
    const snippet = q && c.snippet ? `<span class="snip">${esc(c.snippet)}</span>` : "";
    return `${label}<div class="chat-item ${state.chat?.id === c.id ? "active" : ""} ${c.parentId ? "child" : ""} ${snippet ? "hit" : ""}" data-id="${c.id}" data-index="${c.index ?? ""}" title="${esc(c.model || "")}">${badge}${mark}<span class="t">${esc(c.title)}${snippet}</span><button class="icon-btn del" type="button" title="Delete chat" aria-label="Delete chat">${ic("trash", "sm")}</button></div>`;
  }).join("") || `<div class="list-note">${q ? (state.searchHits?.q === q ? "No matches" : "Searching…") : state.projectFilter ? "No chats in this project yet" : "No chats yet"}</div>`;
}
// Full-text search over every chat's messages, a moment after typing stops
let searchTimer = 0;
function searchChats() {
  clearTimeout(searchTimer); const q = $("#search").value.trim().toLowerCase(); renderChatList();
  if (q.length < 2) return;
  searchTimer = setTimeout(async () => {
    try { const r = await api("/api/search?q=" + encodeURIComponent(q)); state.searchHits = { q, results: r.results }; } catch { state.searchHits = { q, results: [] }; }
    if ($("#search").value.trim().toLowerCase() === q) renderChatList();
  }, 250);
}
function dayGroup(ms) {
  const d = new Date(ms || 0); const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.floor((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 864e5);
  return diff <= 0 ? "Today" : diff === 1 ? "Yesterday" : diff < 7 ? "This week" : "Earlier";
}
// The header's second line: model · folders · Git, so the chat's reach is always visible
function updateSubtitle() {
  const parts = []; const m = currentModel(); if (m && state.chat) parts.push(m);
  const n = chatFolders().length; if (state.chat || n) parts.push(n ? `${n} folder${n > 1 ? "s" : ""}` : "no folders");
  const git = state.chat?.capabilities?.git; const gitText = { read: "Git: read", commit: "Git: commit", push: "Git: push" }[git] || "";
  if (gitText) parts.push(gitText);
  if (state.chat?.parentId) { const p = state.chats.find((c) => c.id === state.chat.parentId); if (p) parts.push("subtask of " + p.title); }
  $("#subtitle").textContent = state.chat ? parts.join(" · ") : "";
  $("#gitLabel").innerHTML = gitText ? `${ic("git", "sm")}${esc(gitText)}` : "";
}
function titleFor(chat) { return chat.title; }
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
      if (ev.status === "idle") { loadChats(); loadUndoRuns().then(renderMessages); }
      break;
    case "compacting": setStatusBar(ev.active ? "Summarizing earlier messages to fit the context budget…" : ""); break;
    case "compaction": chat.compaction = ev.compaction; renderMessages(); break;
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
  renderBalanceBadge();
  let pct = 0;
  if (c && c.limit) {
    pct = Math.min(100, Math.round((c.used / c.limit) * 100));
    parts.push(`<span class="${pct >= 80 ? "warn" : ""}" title="Tokens in the last request, out of this model's context window (${pct}%)">Context <span class="bar"><i></i></span> ${kTokens(c.used)} / ${kTokens(c.limit)}</span>`);
    if (c.omittedMessages) parts.push(`<span class="warn" title="Older messages are left out of requests so the conversation fits">${c.omittedMessages} older message${c.omittedMessages > 1 ? "s" : ""} left out</span>`);
  }
  if (u) {
    const cached = u.cachedTokens ? ` (${Math.round((u.cachedTokens / Math.max(1, u.promptTokens)) * 100)}% cached)` : "";
    parts.push(`<span title="Tokens sent and generated in this chat. Cached input is billed at Ollama's cheaper cached-input rate.">This chat ${kTokens(u.promptTokens)} in${cached} · ${kTokens(u.outputTokens)} out</span>`);
  }
  if (t) {
    const used = t.promptTokens + t.outputTokens; const lim = state.limits?.dailyTokenLimit || 0;
    const of = lim ? ` / ${kTokens(lim)}` : ""; const warn = lim && used >= lim * 0.8 ? "warn" : "";
    parts.push(`<span class="${warn}" title="All chats today${lim ? ", against your daily limit" : ""}${t.cachedTokens ? `. ${kTokens(t.cachedTokens)} input tokens came from Ollama's cache` : ""}">Today ${kTokens(used)}${of} tokens · ${t.requests} requests</span>`);
  }
  $("#usageLine").innerHTML = parts.join('<span class="sep">·</span>');
  const fill = $("#usageLine .bar i"); if (fill) fill.style.width = pct + "%";  // CSSOM: allowed by the page's CSP
}
// Balance level: "low" at or below the pause amount, "warn" within 50 cents of it
function balanceLevel() {
  const b = state.balance?.balance; const min = state.balance?.minimumUsd || 0;
  if (!b || !("totalUsd" in b)) return "";
  return min && b.totalUsd <= min ? "low" : min && b.totalUsd <= min + 0.5 ? "warn" : "ok";
}
function renderBalanceBadge() {
  const badge = $("#connBadge"); const b = state.balance?.balance; const min = state.balance?.minimumUsd || 0;
  let text = "Ollama Cloud", tip = "Ollama balance. Click to check now.";
  if (b && "totalUsd" in b) {
    text = "$" + b.totalUsd.toFixed(2);
    tip = `Ollama balance: $${b.purchasedUsd.toFixed(2)} purchased + $${b.includedUsd.toFixed(2)} plan allowance` + (min ? `. Replies pause at $${min.toFixed(2)}.` : ".") + " Click to check now.";
  } else if (b && b.error) { text = "$ –"; tip = "Balance unavailable: " + b.error; }
  badge.className = "badge " + (balanceLevel() || (b && b.error && min ? "warn" : ""));
  badge.querySelector("span").textContent = text; badge.title = tip;
}
async function loadUsage() { try { const u = await api("/api/usage"); state.today = u.today; state.limits = u.limits; renderUsage(); } catch {} }
function setStatusBar(text) { $("#statusBar").hidden = !text; $("#statusText").textContent = text || ""; }
// Replies in this chat that still have file changes to undo (see undo.py)
async function loadUndoRuns() {
  const chat = state.chat; if (!chat) { state.undoRuns = []; return; }
  try { const r = await api(`/api/undo?chat=${encodeURIComponent(chat.id)}&run=`); if (state.chat?.id === chat.id) state.undoRuns = r.runs || []; } catch { state.undoRuns = []; }
}

function newChat() {
  state.navSeq++; subscribe(null); state.chat = null; state.pause = null; renderPause(); state.draftFolders = []; state.editIndex = null; closeApproval(); setRunStatus("idle");
  state.selectedModel = state.config.defaultModel || state.models[0]?.name || "";
  state.undoRuns = []; setStatusBar("");
  renderMessages(); renderChatList(); updateModelBtn(); updateFolderBtn(); updateUntrustedBadge(); toggleFolderPop(false); renderUsage(); $("#title").textContent = ""; $("#input").focus();
  if (typeof onChatChanged === "function") onChatChanged();
}
async function openChat(id) {
  const ticket = ++state.navSeq; let chat;
  try { chat = await api("/api/chats/" + id); } catch (e) { toast(e.message); return; }
  if (ticket !== state.navSeq) return;
  closeApproval(); state.pause = null; renderPause(); state.editIndex = null; state.chat = chat; state.selectedModel = chat.model; setRunStatus(state.runs[id] || "idle");
  state.undoRuns = []; setStatusBar("");
  updateModelBtn(); renderMessages(); renderChatList(); updateFolderBtn(); updateUntrustedBadge(); toggleFolderPop(false); renderUsage(); $("#title").textContent = titleFor(chat); scrollBottom(true);
  subscribe(id);  // the snapshot that follows brings any live run state
  loadUndoRuns().then(() => { if (state.chat?.id === chat.id) renderMessages(); });
  if (typeof onChatChanged === "function") onChatChanged();
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
const TOOL_LABEL = { list_directory: "Listed folder", read_file: "Read file", search_files: "Searched files", write_file: "Wrote file", edit_file: "Edited file", create_folder: "Created folder", copy_path: "Copied item", move_path: "Moved/renamed item", delete_path: "Moved to trash", restore_path: "Restored item", set_session_model: "Changed model", web_search: "Searched the web", web_fetch: "Read web page", start_subtask: "Subtask", message_subtask: "Messaged subtask", wait_subtasks: "Waited for subtasks", message_parent: "Messaged parent chat", read_skill_file: "Read skill file", git_status: "Git status", git_diff: "Git diff", git_log: "Git log", git_show: "Git show", git_branches: "Git branches", git_commit: "Git commit", git_push: "Git push", gh_pr_list: "Pull requests", gh_pr_create: "Opened pull request", run_command: "Ran command" };
const TOOL_ICON = { list_directory: "folder", read_file: "file", search_files: "search", write_file: "edit", edit_file: "edit", create_folder: "folder", copy_path: "copy", move_path: "file", delete_path: "trash", restore_path: "restore", set_session_model: "model", web_search: "globe", web_fetch: "globe", start_subtask: "subtask", message_subtask: "subtask", wait_subtasks: "subtask", message_parent: "subtask", read_skill_file: "skills", run_command: "terminal" };
const toolIcon = (name) => TOOL_ICON[name] || (/^(git|gh)_/.test(name || "") ? "git" : "settings");
// Messages added since the chat was opened slide in; re-rendering an existing message never animates it again.
const shown = { chatId: null, count: 0 };
const enterCls = (idx) => (idx >= shown.count ? " enter" : "");
function approvalCard(idx, icon, tint, question, detail, buttons) {
  return `<div class="approve${enterCls(idx)}" data-idx="${idx}"><span class="sq ${tint}">${ic(icon)}</span><div class="info"><div class="q">${question}</div><div class="detail">${detail}</div></div><div class="btns">${buttons}</div></div>`;
}
function toolHTML(m, idx) {
  if (m._actionApproval) { const t = m._actionApproval.title; const k = actionKind(t);
    return approvalCard(idx, k.icon, k.tint, esc(t), "Nothing has happened yet. This needs your approval.", `<button class="btn" data-action-approve="no" type="button">Deny</button><button class="btn primary" data-action-review="yes" type="button">Review</button>`); }
  if (m._modelApproval) return approvalCard(idx, "model", "teal", `Switch this session to ${esc(m._modelApproval.model)}?`, "Nothing has changed yet. The model is waiting for your approval.", `<button class="btn" data-model-approve="no" type="button">Keep current</button><button class="btn primary" data-model-review="yes" type="button">Review change</button>`);
  if (m._approval) return approvalCard(idx, "edit", "", `Review change to <span class="mono">${esc(m._approval.path.split(/[\\/]/).pop())}</span>`, `Nothing has been written. <span class="mono">${esc(m._approval.path)}</span>`, `<button class="btn" data-approve="no" type="button">Deny</button><button class="btn primary" data-review="yes" type="button">Review change</button>`);
  const status = m._waitingParent ? "waiting for the parent…" : m._running ? "running…" : m.isError ? "failed" : "";
  const open = m.subtaskId ? `<button class="btn sm open-sub" data-open-chat="${esc(m.subtaskId)}" type="button">Open</button>` : "";
  return `<details class="tool${m.isError ? " err" : ""}${m._running ? " running" : ""}${enterCls(idx)}" data-idx="${idx}"><summary>${ic(toolIcon(m.tool_name))}<b>${esc(TOOL_LABEL[m.tool_name] || m.tool_name || "Tool")}</b><span class="arg">${esc(m.display || "")}</span><span class="st">${status}</span>${open}${ic("chevron", "sm chev")}</summary><div class="out">${esc(m.content || "")}</div></details>`;
}
function msgHTML(m, idx, isLast) {
  if (m.role === "system") return ""; if (m.role === "tool") return toolHTML(m, idx);
  const enter = enterCls(idx);
  if (m.role === "user" && m.fromParent) return `<div class="msg user auto from-parent${enter}" data-idx="${idx}"><div class="bubble"><span class="from">${ic("subtask", "sm")}From the parent chat</span>${esc(String(m.content).replace(/^\[Message from the parent chat\]\n?/, ""))}</div></div>`;
  if (m.role === "user" && m.auto) return `<div class="msg user auto${enter}" data-idx="${idx}"><div class="bubble">${esc(m.content)}</div></div>`;
  if (m.role === "user") {
    const imgs = (m.images || []).map((b) => `<img src="data:image/*;base64,${b}" alt="">`).join("");
    const docs = (m.attachments || []).map((a) => `<span class="doc-chip" title="${esc(a.note || "")}">${ic("doc", "sm")}${esc(a.name)}<span class="faint">${kTokens(Math.round((a.chars || 0) / 4))} tokens</span></span>`).join("");
    const plan = m.plan ? `<span class="chip running plan-chip">${ic("plan", "sm")}Plan mode</span>` : "";
    return `<div class="msg user${enter}" data-idx="${idx}">${imgs ? `<div class="imgs">${imgs}</div>` : ""}${docs || plan ? `<div class="docs">${plan}${docs}</div>` : ""}${m.content ? `<div class="bubble">${esc(m.content)}</div>` : ""}<div class="actions"><button class="icon-btn act-copy" type="button" title="Copy" aria-label="Copy">${ic("copy", "sm")}</button><button class="icon-btn act-edit" type="button" title="Edit & resend" aria-label="Edit and resend">${ic("edit", "sm")}</button></div></div>`;
  }
  const liveThought = m._live && !m.content;
  const thinking = m.thinking ? `<details class="thinking${liveThought ? " live" : ""}" ${liveThought ? "open" : ""}><summary>${ic("thought", "sm")}<span class="lbl">${liveThought ? "Thinking…" : "Thought process"}</span>${ic("chevron", "sm chev")}</summary><div class="tbody">${esc(m.thinking)}</div></details>` : "";
  const stats = m.stats ? `<span>· ${m.stats}</span>` : ""; const body = m.content ? renderMarkdown(m.content) : "";
  if (m.tool_calls?.length && !m._live) return `<div class="msg assistant step${enter}" data-idx="${idx}">${thinking}${body ? `<div class="content">${body}</div>` : ""}</div>`;
  const turn = turnMeta[idx] || {};
  const undoRun = !m._live && (turn.runs || []).filter((r) => state.undoRuns.includes(r)).pop();
  const nChanged = undoRun ? (state.chat?.messages || []).filter((x) => x.runId === undoRun).length : 0;
  const undoBtn = undoRun ? `<span class="help">${nChanged} file change${nChanged === 1 ? "" : "s"} in this reply</span><button class="btn sm ghost" type="button" data-undo="${esc(undoRun)}" title="Put back the files this reply changed">${ic("undo", "sm")}Undo changes</button>` : "";
  const planBtn = turn.plan && isLast && !m._live && !m.error && !busy() ? `<div class="plan-go"><button class="btn sm primary" type="button" data-carry-out>${ic("play", "sm")}Carry out this plan</button><span class="help">Or reply with changes to the plan first.</span></div>` : "";
  return `<div class="msg assistant${isLast ? " last" : ""}${enter}" data-idx="${idx}"><div class="meta"><span>${esc(m.model || "")}</span>${stats}${m.wrapUp ? '<span class="wrapup-tag" title="Low balance: the model summarized its progress before pausing">wrap-up</span>' : ""}</div>${thinking}<div class="content ${m._live ? "cursor" : ""}">${body}</div>${m.notice ? `<div class="notice">${esc(m.notice)}</div>` : ""}${m.error ? `<div class="err">${linkify(m.error)}</div>` : ""}${planBtn}${undoBtn ? `<div class="reply-foot">${undoBtn}</div>` : ""}${m._live ? "" : `<div class="actions"><button class="icon-btn act-copy" type="button" title="Copy" aria-label="Copy">${ic("copy", "sm")}</button>${isLast ? `<button class="icon-btn act-regen" type="button" title="Regenerate" aria-label="Regenerate">${ic("redo", "sm")}</button>` : ""}</div>`}</div>`;
}
// For each turn's answer: the replies (run IDs) that changed files in it, and whether it was asked in plan mode
let turnMeta = {};
function computeTurns(msgs) {
  const meta = {}; let plan = false, runs = [];
  msgs.forEach((m, i) => {
    if (m.role === "user" && !m.auto) { plan = Boolean(m.plan); runs = []; }
    else if (m.role === "tool" && m.runId && !runs.includes(m.runId)) runs.push(m.runId);
    else if (m.role === "assistant" && !m.tool_calls?.length) meta[i] = { plan, runs: runs.slice() };
  });
  return meta;
}
function summaryDivider(c) {
  return `<div class="summary-divider" data-summary><span class="line"></span><button class="btn sm ghost" type="button" data-show-summary>${ic("memory", "sm")}Earlier messages were summarized to fit the context budget · View summary</button><span class="line"></span></div><div class="summary-box content" hidden>${renderMarkdown(c.summary || "")}</div>`;
}
const APP_LOGO = '<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="#1D2026"/><rect x=".5" y=".5" width="31" height="31" rx="8.5" fill="none" stroke="rgba(255,255,255,.1)"/><path d="M9.5 22V13.5a6.5 6.5 0 0 1 13 0V22" fill="none" stroke="#F2F3F5" stroke-width="2.6" stroke-linecap="round"/><circle cx="13.3" cy="15.4" r="1.6" fill="#8B9CFF"/><circle cx="18.7" cy="15.4" r="1.6" fill="#8B9CFF"/></svg>';
const SUGGESTIONS = [
  ["folder", "Review a project", "Read a folder and point out bugs", "Review the code in the shared folder and list real bugs first, with file and line.", true],
  ["globe", "Research a topic", "Search the web, with sources", "Research this and cite your sources: "],
  ["terminal", "Fix failing tests", "Run tests, edit, run again", "Run the tests in the shared folder, fix what fails, and run them again."],
];
function renderMessages() {
  const box = $("#messages"); const msgs = state.chat?.messages || [];
  const id = state.chat?.id || null; if (shown.chatId !== id) { shown.chatId = id; shown.count = msgs.length; shown.times = {}; }
  if (!msgs.filter((m) => m.role !== "system").length) {
    const m = currentModel();
    box.innerHTML = `<div id="empty">${APP_LOGO}<h1>What should we work on?</h1><div class="sub">${m ? `Chatting with <b>${esc(m)}</b>. Share folders to let it read and edit files.` : "Pick a cloud model to start."}</div>
      <div class="suggest">${SUGGESTIONS.map((s, i) => `<button type="button" data-suggest="${i}">${ic(s[0])}<b>${s[1]}</b><span>${s[2]}</span></button>`).join("")}</div></div>`;
    shown.count = msgs.length; return;
  }
  const lastIdx = msgs.length - 1; turnMeta = computeTurns(msgs);
  const c = state.chat?.compaction; const upTo = c && c.upTo > 0 && c.upTo < msgs.length ? c.upTo : -1;
  box.innerHTML = msgs.map((m, i) => (i === upTo ? summaryDivider(c) : "") + msgHTML(m, i, i === lastIdx)).join("");
  // A message that slid in moments ago and was re-rendered continues its slide where it was
  const now = performance.now(); shown.times ||= {};
  for (let i = shown.count; i < msgs.length; i++) shown.times[i] = now;
  for (const [i, t] of Object.entries(shown.times)) {
    if (now - t > 300) { delete shown.times[i]; continue; }
    const el = box.querySelector(`[data-idx="${i}"]`);
    if (el && !el.classList.contains("enter")) { el.classList.add("enter"); el.style.animationDelay = `-${Math.round(now - t)}ms`; }
  }
  shown.count = msgs.length;
}
function renderLive() {
  const msgs = state.chat.messages; const i = msgs.length - 1; const el = $(`#messages .msg[data-idx="${i}"]`);
  if (!el) { renderMessages(); return; }
  // Update the element in place so its slide-in keeps playing while text streams in
  const tmp = document.createElement("div"); tmp.innerHTML = msgHTML(msgs[i], i, true); const next = tmp.firstElementChild; if (!next) return;
  const open = el.querySelector("details.thinking")?.open;
  el.className = next.className + (el.classList.contains("enter") ? " enter" : ""); el.innerHTML = next.innerHTML;
  const th = el.querySelector("details.thinking"); if (th && open !== undefined && !msgs[i].content) th.open = open;
  const tb = el.querySelector(".tbody"); if (tb) tb.scrollTop = tb.scrollHeight;
}
function nearBottom() { const s = $("#scroller"); return s.scrollHeight - s.scrollTop - s.clientHeight < 120; }
function scrollBottom(force) { const s = $("#scroller"); if (force || nearBottom()) s.scrollTop = s.scrollHeight; }
function updateSendBtn() {
  const b = $("#sendBtn");
  const stopping = busy(); b.classList.toggle("stop", stopping);
  if (b.dataset.icon !== (stopping ? "stop" : "send")) { b.dataset.icon = stopping ? "stop" : "send"; b.innerHTML = ic(b.dataset.icon); }
  if (stopping) { b.title = "Stop"; b.setAttribute("aria-label", "Stop"); b.disabled = false; }
  else { b.title = "Send (Enter)"; b.setAttribute("aria-label", "Send"); b.disabled = !$("#input").value.trim() && !state.attachments.length && !state.documents.length; }
}

// Sending: the server saves the message and runs the reply
function thinkOption() { const t = $("#thinkSel").value; return t === "default" ? null : t === "true" ? true : t === "false" ? false : t; }
async function send(override) {
  const text = override ?? $("#input").value.trim(); if (busy() || (!text && !state.attachments.length && !state.documents.length)) return;
  if (state.documents.some((d) => d.loading)) { toast("Wait for the attached documents to finish reading"); return; }
  const model = currentModel(); if (!model) { toast("Pick a cloud model first"); openModelMenu(true); return; }
  const images = state.attachments.slice(); const docs = state.documents.slice(); const truncateAt = state.editIndex;
  const plan = override === undefined && state.plan;
  try {
    if (!state.chat) {
      const title = (text || docs[0]?.name || "Image").slice(0, 60).replace(/\s+/g, " ");
      const chat = await api("/api/chats", { method: "POST", body: JSON.stringify({ title, model, folders: state.draftFolders, projectId: state.projectFilter || undefined }) });
      state.draftFolders = []; state.chat = chat; shown.chatId = chat.id; shown.count = 0; $("#title").textContent = chat.title; updateChatMeta(chat); subscribe(chat.id);
    }
    const chat = state.chat; setRunStatus("running");
    if (override === undefined) { $("#input").value = ""; autoGrow(); state.attachments = []; renderAttachments(); state.documents = []; renderDocs(); setPlan(false); }
    state.editIndex = null; updateEditBanner();
    await api("/api/runs", { method: "POST", body: JSON.stringify({ chatId: chat.id, text, images, truncateAt, think: thinkOption(), plan,
      documents: docs.map((d) => ({ name: d.name, text: d.text, note: d.note })) }) });
  } catch (e) {
    toast(e.message); setRunStatus("idle");
    if (override === undefined && !$("#input").value) { $("#input").value = text; state.attachments = images; state.documents = docs; renderAttachments(); renderDocs(); autoGrow(); }
  }
}
function setPlan(on) { state.plan = Boolean(on); $("#planBtn").setAttribute("aria-pressed", String(state.plan)); $("#input").placeholder = state.plan ? "Describe the task: the model investigates and proposes a plan, without changing anything…" : "Ask anything, or describe a task…"; }

// Documents: the server reads their text (documents.py); it goes to the model with the message
async function addDocument(file) {
  const doc = { name: file.name, loading: true, text: "", note: "", chars: 0 }; state.documents.push(doc); renderDocs();
  try {
    const data = await new Promise((ok, fail) => { const rd = new FileReader(); rd.onload = () => ok(String(rd.result).split(",")[1] || ""); rd.onerror = fail; rd.readAsDataURL(file); });
    const r = await api("/api/documents/extract", { method: "POST", body: JSON.stringify({ name: file.name, data }) });
    Object.assign(doc, { text: r.text, note: r.note, chars: r.chars, loading: false });
    if (r.note) toast(`${file.name}: ${r.note}`);
  } catch (e) { state.documents = state.documents.filter((d) => d !== doc); toast(`${file.name}: ${e.message}`); }
  renderDocs();
}
function renderDocs() {
  $("#docList").innerHTML = state.documents.map((d, i) => `<span class="doc-chip${d.loading ? " loading" : ""}" title="${esc(d.note || "")}">${ic("doc", "sm")}${esc(d.name)}<span class="faint">${d.loading ? "reading…" : `${kTokens(Math.round(d.chars / 4))} tokens`}</span><button type="button" data-doc="${i}" aria-label="Remove ${esc(d.name)}">×</button></span>`).join("");
  updateSendBtn();
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
function actionKind(title) {
  const t = String(title || "");
  if (/command/i.test(t)) return { icon: "terminal", tint: "" };
  if (/push|pull request/i.test(t)) return { icon: "git", tint: "amber" };
  if (/commit/i.test(t)) return { icon: "git", tint: "" };
  if (/web page/i.test(t)) return { icon: "globe", tint: "red" };
  if (/model/i.test(t)) return { icon: "model", tint: "teal" };
  return { icon: "shield", tint: "amber" };
}
function showApproval(a) {
  if (!a) { closeApproval(); return; }
  if (a.kind === "write") {
    if (state.approval?.id === a.id) return;
    state.approval = { id: a.id, chatId: state.chat.id, approval: a.data, mode: a.data.exists ? "diff" : "content", decided: false }; openApprovalPreview();
  } else if (a.kind === "action") {
    if (state.actionApproval?.id === a.id) return;
    state.actionApproval = { id: a.id, chatId: state.chat.id, decided: false };
    $("#aaTitle").textContent = a.data.title || "Allow this action?"; $("#aaDetail").textContent = a.data.detail || ""; $("#aaWhy").textContent = a.data.why || "";
    const k = actionKind(a.data.title); $("#aaIcon").className = "sq lg " + k.tint; $("#aaIcon").innerHTML = ic(k.icon);
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
  const lines = (a.diff || "").split("\n"); const added = lines.filter((l) => l.startsWith("+") && !l.startsWith("+++")).length; const removed = lines.filter((l) => l.startsWith("-") && !l.startsWith("---")).length;
  $("#previewInfo").innerHTML = a.exists ? `<span class="a">+${added}</span> · <span class="d">−${removed}</span> · ${humanBytes(a.currentBytes)} → ${humanBytes(a.newBytes)}` : `New file · ${humanBytes(a.newBytes)}`;
  $("#previewInfo").title = a.exists ? "Lines added and removed, and the file size before and after. The diff is complete." : "This creates a new file.";
  const text = pending.mode === "diff" ? a.diff : a.content; const box = $("#previewText");
  if (pending.mode === "diff" && text) {
    box.classList.remove("plain");
    box.innerHTML = text.split("\n").map((l) => {
      const k = l.startsWith("@@") ? "hunk" : l.startsWith("+++") || l.startsWith("---") ? "meta" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
      return `<span class="l ${k}">${esc(l) || " "}</span>`;
    }).join("");
  } else { box.classList.add("plain"); box.textContent = text || (pending.mode === "content" ? "(empty file)" : "(no differences)"); }
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
// Pictures are shrunk to at most 1600 px on the long side before sending: vision models bill by image
// size, and they read text in screenshots just as well at that size. Small pictures go as they are.
const MAX_IMAGE_SIDE = 1600;
async function imageBase64(file) {
  const original = await new Promise((ok, fail) => { const rd = new FileReader(); rd.onload = () => ok(String(rd.result).split(",")[1]); rd.onerror = fail; rd.readAsDataURL(file); });
  try {
    const bmp = await createImageBitmap(file); const side = Math.max(bmp.width, bmp.height);
    if (side <= MAX_IMAGE_SIDE && file.size <= 400_000) { bmp.close(); return original; }
    const k = Math.min(1, MAX_IMAGE_SIDE / side); const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * k); c.height = Math.round(bmp.height * k);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height); bmp.close();
    const type = file.type === "image/png" ? "image/png" : "image/jpeg";  // screenshots stay PNG for sharp text
    const smaller = c.toDataURL(type, 0.9).split(",")[1];
    return smaller.length < original.length ? smaller : original;
  } catch { return original; }
}
async function addFiles(files) {
  for (const f of files) {
    if (f.type.startsWith("image/")) { state.attachments.push(await imageBase64(f)); renderAttachments(); }
    else if (typeof addDocument === "function") await addDocument(f);
  }
}

// Settings and updates
function openSettings() {
  const c = state.config; state.clearKey = false; state.updateUrl = ""; $("#apiKey").value = "";
  $("#apiKey").placeholder = c.hasApiKey ? `Saved ${c.apiKeyHint || "(from OLLAMA_API_KEY)"} - leave blank to keep` : "Paste key…";
  $("#clearKey").disabled = !c.hasApiKey; $("#versionLabel").textContent = `Open AY Desktop ${c.version || ""}`;
  $("#updateStatus").textContent = `Checks github.com/${c.repository || "arothm/OpenAYDesktop"}`; $("#openUpdate").hidden = true;
  $("#dailyLimit").value = c.dailyTokenLimit || 0; $("#chatLimit").value = c.chatTokenLimit || 0; $("#maxRuns").value = c.maxConcurrentRuns || 4; $("#allowedCommands").value = (c.allowedCommands || []).join("\n"); $("#minBalance").value = c.minBalanceUsd ?? 1;
  $("#setVer").textContent = c.version ? "Version " + c.version : "";
  $("#keyState").className = "key-state" + (c.hasApiKey ? " ok" : ""); $("#keyState").textContent = c.hasApiKey ? `Saved ${c.apiKeyHint || "(from OLLAMA_API_KEY)"}` : "Not set";
  renderSettingsBalance(); showSettingsSection("secConn", false);
  setBudgetButtons(c.contextBudget || "full");
  $("#autoUpdate").checked = c.autoUpdate !== false; $("#quickEntry").checked = c.quickEntry !== false;
  $("#quickEntry").closest(".perm").hidden = !window.openay?.isElectron;
  $("#globalMemory").value = ""; state.memoryLoaded = null;
  api("/api/memory?scope=global").then((r) => { $("#globalMemory").value = r.text; state.memoryLoaded = r.text; }).catch(() => {});
  if (window.openay?.updates) window.openay.updates.state().then(renderUpdateState).catch(() => {});
  populateDefaultModels(); $("#modalBack").classList.add("open");
}
function setBudgetButtons(name) { document.querySelectorAll("#ctxBudget [data-budget]").forEach((b) => b.classList.toggle("primary", b.dataset.budget === name)); }
document.querySelector("#ctxBudget").addEventListener("click", (e) => { const b = e.target.closest("[data-budget]"); if (b) setBudgetButtons(b.dataset.budget); });
// In the Electron app, updates install from GitHub Releases (electron/main.js); in a browser window the
// older check only links to the release page.
function renderUpdateState(s) {
  if (!s) return;
  const st = $("#updateStatus"), meter = $("#updateMeter");
  const text = { dev: "Updates install in the installed app (this is a development copy).", idle: "Checks for updates a few seconds after start, then every 6 hours.",
    checking: "Checking for updates…", current: "You have the latest version.", available: `Version ${s.latest} is available.`,
    downloading: `Downloading version ${s.latest || ""}… ${s.percent || 0}%`, ready: `Version ${s.latest} is ready. It installs when you restart the app.`,
    error: "Couldn't check for updates: " + (s.error || "unknown error") }[s.state] || "";
  st.textContent = text; meter.hidden = s.state !== "downloading";
  if (s.state === "downloading") meter.querySelector("i").style.width = (s.percent || 0) + "%";
  $("#downloadUpdate").hidden = s.state !== "available"; $("#installUpdate").hidden = s.state !== "ready";
  $("#checkUpdates").disabled = ["checking", "downloading", "dev"].includes(s.state);
  const badge = $("#settingsBtn"); badge.classList.toggle("has-update", ["available", "ready"].includes(s.state));
}
function renderSettingsBalance() {
  const b = state.balance?.balance; const min = state.balance?.minimumUsd ?? state.config.minBalanceUsd ?? 0; const level = balanceLevel();
  const meter = $("#setBalanceMeter"); meter.className = "meter " + level;
  if (b && "totalUsd" in b) {
    $("#setBalanceAmt").textContent = "$" + b.totalUsd.toFixed(2);
    $("#setBalanceNote").textContent = `$${b.purchasedUsd.toFixed(2)} purchased + $${b.includedUsd.toFixed(2)} plan allowance`;
    // The bar fills toward $10 above the pause amount, so it empties as replies approach the pause
    meter.querySelector("i").style.width = Math.max(2, Math.min(100, ((b.totalUsd - min) / 10) * 100)) + "%";
  } else {
    $("#setBalanceAmt").textContent = "–"; $("#setBalanceNote").textContent = b?.error || "Balance appears after your API key is saved.";
    meter.querySelector("i").style.width = "0";
  }
}
function showSettingsSection(id, scroll) {
  document.querySelectorAll(".set-nav [data-sec]").forEach((b) => b.classList.toggle("on", b.dataset.sec === id));
  if (scroll) $("#" + id).scrollIntoView({ block: "start", behavior: "smooth" }); else $("#setBody").scrollTop = 0;
}
document.querySelector(".set-nav").addEventListener("click", (e) => { const b = e.target.closest("[data-sec]"); if (b) showSettingsSection(b.dataset.sec, true); });
function populateDefaultModels() {
  const select = $("#defaultModel"); const selected = state.config.defaultModel || "";
  const names = [...new Set([selected, ...state.models.map((m) => m.name)].filter(Boolean))];
  select.innerHTML = '<option value="">First available cloud model</option>' + names.map((name) => `<option value="${esc(name)}" ${name === selected ? "selected" : ""}>${esc(name)}</option>`).join("");
  select.value = selected; if (!state.models.length && !selected) select.innerHTML = '<option value="">Choose a model after saving your API key</option>';
}
async function checkUpdates() {
  if (window.openay?.updates) { renderUpdateState(await window.openay.updates.check()); return; }
  const button = $("#checkUpdates"); button.disabled = true; button.textContent = "Checking…";
  $("#updateStatus").textContent = "Contacting GitHub Releases…"; $("#openUpdate").hidden = true; state.updateUrl = "";
  try {
    const result = await api("/api/updates/check", { method: "POST", body: "{}" }); $("#updateStatus").textContent = result.message;
    if (result.updateAvailable) { state.updateUrl = result.releaseUrl; $("#openUpdate").hidden = false; }
  } catch (e) { $("#updateStatus").textContent = e.message; }
  finally { button.disabled = false; button.textContent = "Check now"; }
}
async function saveSettings() {
  const budget = document.querySelector("#ctxBudget .primary")?.dataset.budget || "full";
  const body = { defaultModel: $("#defaultModel").value, dailyTokenLimit: Number($("#dailyLimit").value || 0),
                 chatTokenLimit: Number($("#chatLimit").value || 0), maxConcurrentRuns: Number($("#maxRuns").value || 4), minBalanceUsd: Number($("#minBalance").value || 0),
                 allowedCommands: $("#allowedCommands").value.split(/\r?\n/).map((l) => l.trim()).filter(Boolean),
                 contextBudget: budget, autoUpdate: $("#autoUpdate").checked, quickEntry: $("#quickEntry").checked };
  const key = $("#apiKey").value.trim(); if (key) body.apiKey = key;
  if (state.clearKey && !key) body.clearApiKey = true;
  try {
    if (state.memoryLoaded !== null && $("#globalMemory").value !== state.memoryLoaded)
      await api("/api/memory", { method: "POST", body: JSON.stringify({ scope: "global", text: $("#globalMemory").value }) });
    const saved = await api("/api/config", { method: "POST", body: JSON.stringify(body) }); const oldDefault = state.config.defaultModel; state.config = saved;
    if (window.openay?.updates) { window.openay.updates.setAuto(saved.autoUpdate !== false); window.openay.setQuickEntry(saved.quickEntry !== false); }
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
  $("#webSearch").checked = Boolean(c.webSearch); $("#gitMode").value = c.git || "off";
  $("#gitMode").disabled = Boolean(state.chat.parentId);
  $("#commandsOn").checked = Boolean(c.commands) && writable && !state.chat.parentId; $("#commandsOn").disabled = !writable || Boolean(state.chat.parentId);
  const isChild = Boolean(state.chat.parentId); $("#subtasks").checked = !isChild && Boolean(c.subtasks); $("#subtasks").disabled = isChild;
  if (isChild) $("#subtasksHelp").textContent = "This chat is a subtask. Its permissions come from the chat that started it, and it can't start subtasks of its own.";
  const u = state.chat.untrusted; $("#untrustedBox").hidden = !u;
  if (u) $("#untrustedSources").textContent = "Since " + new Date(u.since).toLocaleString() + ". Read: " + (u.sources || []).join("; ");
  if (!isChild) $("#subtasksHelp").textContent = "Helper chats that work on parts of a task at the same time and report back. Each gets this chat's folders and skills, at most its permissions, and a fixed model. With Git set to commit, a helper can work in its own branch (a Git worktree). Stopping this chat stops its helpers.";
  $("#instructionsOn").checked = c.instructions !== false; $("#memoryOn").checked = c.memory !== false && !isChild; $("#memoryOn").disabled = isChild;
  $("#sessionProject").innerHTML = '<option value="">No project</option>' + state.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  $("#sessionProject").value = state.chat.projectId && state.projects.some((p) => p.id === state.chat.projectId) ? state.chat.projectId : "";
  $("#sessionSkills").innerHTML = '<span class="help">Loading skills…</span>';
  api("/api/skills").then((r) => {
    const on = new Set(state.chat?.skills || []);
    $("#sessionSkills").innerHTML = r.skills.map((s) => `<label class="skill-check"><span>${esc(s.name)}<span class="d">${esc(s.description)}</span></span><input type="checkbox" data-skill="${esc(s.id)}" ${on.has(s.id) ? "checked" : ""}></label>`).join("")
      || '<span class="help">No skills yet.</span>';
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
              deleteFiles: $("#deleteFiles").checked, modelControl: $("#modelControl").value, webSearch: $("#webSearch").checked, git: $("#gitMode").value, commands: $("#commandsOn").checked && !$("#commandsOn").disabled, subtasks: $("#subtasks").checked && !$("#subtasks").disabled,
              instructions: $("#instructionsOn").checked, memory: $("#memoryOn").checked && !$("#memoryOn").disabled };
  const skillIds = [...document.querySelectorAll("#sessionSkills [data-skill]")].filter((x) => x.checked).map((x) => x.dataset.skill);
  try {
    if (title !== state.chat.title) {
      const result = await api("/api/session/rename", { method: "POST", body: JSON.stringify({ chatId: state.chat.id, title }) });
      state.chat.title = result.title; $("#title").textContent = result.title;
    }
    const changes = { capabilities: c, projectId: $("#sessionProject").value || null }; if (document.querySelector("#sessionSkills [data-skill]")) changes.skills = skillIds;
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
    ? `${items.length} item${items.length > 1 ? "s" : ""} · ${humanBytes(r.totalBytes || 0)} · kept until you remove them`
    : "Empty";
  let group = "";
  $("#trashList").innerHTML = items.map((m) => {
    const chat = state.chats.find((c) => c.id === m.chatId)?.title;
    const d = m.deletedAt ? new Date(m.deletedAt * 1000) : null;
    const g = d ? dayGroup(d.getTime()) : "Earlier"; const label = g !== group ? `<div class="group-label">${g}</div>` : ""; group = g;
    const when = d ? (g === "Today" || g === "Yesterday" ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : d.toLocaleString()) : "";
    const kind = m.kind === "version" ? '<span class="chip version">Earlier version</span>' : `<span class="chip">Deleted${m.isDir ? " folder" : ""}</span>`;
    return `${label}<div class="row-card trash-row" data-id="${esc(m.id)}">${kind}<div class="info"><div class="p" title="${esc(m.originalPath)}">&lrm;${esc(m.originalPath)}</div>
      <div class="m">${esc(when)} · ${humanBytes(m.size || 0)}${chat ? " · " + esc(chat) : ""}</div></div>
      <button class="icon-btn sm" data-trash="purge" type="button" title="Delete permanently" aria-label="Delete permanently">${ic("x", "sm")}</button><button class="btn sm" data-trash="restore" type="button">${ic("restore", "sm")}${m.kind === "version" ? "Restore version" : "Restore"}</button></div>`;
  }).join("") || '<div class="empty-note">Files a session deletes, and earlier versions of files it edits, appear here.</div>';
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
async function openActivity() { $("#activityBack").classList.add("open"); renderActivity.seen = false; await renderActivity(); clearInterval(activityTimer); activityTimer = setInterval(renderActivity, 2000); }
function closeActivity() { clearInterval(activityTimer); $("#activityBack").classList.remove("open"); }
const ago = (s) => s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
function activityRow(a, live) {
  const parent = a.parentId && state.chats.find((c) => c.id === a.parentId);
  const label = live ? (a.approval ? "waiting for you" : a.status === "paused" ? "paused: low balance" : a.status) : a.status;
  const cls = live ? (a.approval ? "waiting" : a.status === "paused" ? "paused" : "running") : a.status === "idle" || a.status === "done" ? "done" : a.status;
  const meta = [a.model, ago(a.seconds), `${a.steps} step${a.steps === 1 ? "" : "s"}`, `${kTokens(a.tokens)} tokens`];
  if (parent) meta.push("subtask of " + parent.title); if (a.children?.length) meta.push(`${a.children.length} subtask${a.children.length > 1 ? "s" : ""}`);
  const busyRow = live && cls === "running" ? " busy" : "";
  return `<div class="row-card act-row${parent ? " child" : ""}${busyRow}" data-chat="${esc(a.chatId)}"><span class="chip ${esc(cls)}">${esc(label)}</span><div class="info"><div class="n">${esc(a.title || "Untitled")}</div><div class="m">${esc(meta.join(" · "))}</div></div>
    <button class="btn sm" data-act="open" type="button">${a.approval ? "Review" : "Open"}</button>${live ? '<button class="btn sm danger" data-act="stop" type="button">Stop</button>' : ""}</div>`;
}
async function renderActivity() {
  if (!$("#activityBack").classList.contains("open")) { clearInterval(activityTimer); return; }
  let r; try { r = await api("/api/activity"); } catch (e) { $("#activityList").innerHTML = `<div class="menu-note">${esc(e.message)}</div>`; return; }
  const waiting = r.active.filter((a) => a.approval).length; const running = r.active.length - waiting;
  const t = state.today; const used = t ? t.promptTokens + t.outputTokens : 0; const lim = state.limits?.dailyTokenLimit || 0;
  const stat = (v, l, cls) => `<div class="stat"><div class="v ${cls || ""}">${v}</div><div class="l">${l}</div></div>`;
  $("#activityStats").innerHTML = stat(running, "Running") + stat(waiting, "Waiting for you", waiting ? "amber" : "")
    + stat(`${r.active.length} / ${state.config.maxConcurrentRuns || 4}`, "Replies at once") + stat(kTokens(used), "Tokens today" + (lim ? ` · limit ${kTokens(lim)}` : ""));
  // Rows slide in when the dialog opens; the refresh every 2 s must not replay that
  const still = renderActivity.seen ? " still" : ""; renderActivity.seen = true;
  const rows = (list, liveRows, empty) => (list.map((a) => activityRow(a, liveRows)).join("") || `<div class="empty-note">${empty}</div>`).replaceAll('class="row-card', 'class="row-card' + still);
  $("#activityList").innerHTML = rows(r.active, true, "Nothing is running.");
  $("#activityRecent").innerHTML = rows(r.recent, false, "Nothing yet since the app started.");
}
$("#activityBtn").addEventListener("click", openActivity);
$("#activityClose").addEventListener("click", closeActivity);
$("#activityBack").addEventListener("click", async (e) => {
  if (e.target.id === "activityBack") { closeActivity(); return; }
  const b = e.target.closest("[data-act]"); if (!b) return; const id = b.closest(".act-row").dataset.chat;
  if (b.dataset.act === "open") { closeActivity(); openChat(id); }
  else { try { await api("/api/runs/stop", { method: "POST", body: JSON.stringify({ chatId: id }) }); } catch (err) { toast(err.message); } renderActivity(); }
});

// Skills: Markdown instructions or skill folders stored in data/skills, turned on per chat in Session settings
const skillsUi = { list: [], current: null };
async function openSkills() { $("#skillsBack").classList.add("open"); await loadSkills(); if (!skillsUi.current) editSkill(null); }
async function loadSkills() {
  try { skillsUi.list = (await api("/api/skills")).skills; } catch (e) { toast(e.message); skillsUi.list = []; }
  $("#skillsList").innerHTML = skillsUi.list.map((s) => `<button class="skill-item ${skillsUi.current === s.id ? "sel" : ""}" data-skill-id="${esc(s.id)}" type="button"><span class="av c${hashIndex(s.id, 4)}">${esc(initials(s.name))}</span><span class="tx"><div class="n">${esc(s.name)}${s.folder ? ` <span class="pkg-tag">${ic("folder", "sm")}${s.files} file${s.files === 1 ? "" : "s"}</span>` : ""}</div><div class="d">${esc(s.description || "")}</div></span></button>`).join("")
    || '<div class="skill-list-note">No skills yet. Create one, or import a Markdown file, a .skill file or a skill folder.</div>';
}
const initials = (name) => (String(name).match(/[A-Za-z0-9]+/g) || ["?"]).slice(0, 2).map((w) => w[0].toUpperCase()).join("");
function hashIndex(s, n) { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h % n; }
function updateSkillCount() { const n = $("#skillBody").value.length; $("#skillCount").textContent = `${n.toLocaleString()} characters`; }
async function editSkill(id) {
  skillsUi.current = id; let s = { name: "", description: "", body: "" };
  if (id) { try { s = await api("/api/skills/" + encodeURIComponent(id)); } catch (e) { toast(e.message); return; } }
  $("#skillName").value = s.name; $("#skillDesc").value = s.description; $("#skillBody").value = s.body; $("#skillDelete").disabled = !id; updateSkillCount();
  renderSkillPackage(s);
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
function renderSkillPackage(s) {
  const box = $("#skillPkg"); box.hidden = !s.folder; if (!s.folder) { box.innerHTML = ""; return; }
  const files = s.files || [];
  box.innerHTML = `<div class="pkg-row">${ic("folder")}<div class="pkg-text"><b>Skill folder</b> with ${files.length} other file${files.length === 1 ? "" : "s"}. The model reads them when the instructions call for them; scripts run only with Run commands.
      <div class="help">${s.source ? `Imported from <span class="mono">${esc(s.source)}</span>. Edits you make here are replaced by Update from folder.` : "Imported from a .skill or .zip file. Import it again to update it."}</div></div>
      ${s.source ? `<button class="btn sm" id="skillUpdate" type="button">${ic("redo", "sm")}Update from folder</button>` : ""}</div>
    ${files.length ? `<details class="pkg-files"><summary>Show files</summary><div class="mono">${files.map((f) => `<div><span>${esc(f.path)}</span><span>${humanBytes(f.size)}</span></div>`).join("")}</div></details>` : ""}`;
}
async function importSkillPackage(body, label) {
  for (;;) {
    const r = await fetch("/api/skills/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 409 && j.exists && !body.replace) { if (!confirm(j.error + "\n\nIts files and instructions are replaced. Chats that use it keep using it.")) return; body.replace = true; continue; }
    if (!r.ok) throw new Error(j.error || r.statusText);
    skillsUi.current = j.id; await loadSkills(); await editSkill(j.id); toast(`Imported ${label}`); return;
  }
}
async function importSkillFolder() {
  try {
    const picked = window.openay?.pickFolder ? await window.openay.pickFolder() : (await api("/api/pick-folder", { method: "POST" })).path;
    if (picked) await importSkillPackage({ path: picked }, "the skill folder");
  } catch (e) { toast(e.message); }
}
function importSkill(file) {
  if (/\.(skill|zip)$/i.test(file.name)) {
    const rd = new FileReader();
    rd.onload = () => importSkillPackage({ zip: String(rd.result).split(",")[1] || "", name: file.name }, file.name).catch((e) => toast(e.message));
    rd.onerror = () => toast("Couldn't read " + file.name);
    rd.readAsDataURL(file); return;
  }
  const rd = new FileReader();
  rd.onload = () => {
    let text = String(rd.result); let name = file.name.replace(/\.(md|markdown|txt)$/i, ""); let description = "";
    const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
    if (m) { for (const line of m[1].split(/\r?\n/)) { const [k, ...v] = line.split(":"); if (/^name$/i.test(k.trim())) name = v.join(":").trim(); if (/^description$/i.test(k.trim())) description = v.join(":").trim(); } text = m[2]; }
    skillsUi.current = null; $("#skillName").value = name; $("#skillDesc").value = description; $("#skillBody").value = text.trim(); $("#skillDelete").disabled = true; updateSkillCount(); renderSkillPackage({});
    toast("Imported. Review it, then Save skill.");
  };
  rd.readAsText(file);
}
$("#skillsBtn").addEventListener("click", openSkills);
$("#skillsClose").addEventListener("click", () => $("#skillsBack").classList.remove("open"));
$("#skillsBack").addEventListener("click", (e) => { if (e.target.id === "skillsBack") $("#skillsBack").classList.remove("open"); });
$("#skillsList").addEventListener("click", (e) => { const b = e.target.closest("[data-skill-id]"); if (b) editSkill(b.dataset.skillId); });
$("#skillNew").addEventListener("click", () => editSkill(null)); $("#skillSave").addEventListener("click", saveSkill); $("#skillDelete").addEventListener("click", deleteSkill);
$("#skillImport").addEventListener("click", () => $("#skillFile").click()); $("#skillImportFolder").addEventListener("click", importSkillFolder);
$("#skillPkg").addEventListener("click", async (e) => {
  if (!e.target.closest("#skillUpdate") || !skillsUi.current) return;
  try { const s = await api("/api/skills/update", { method: "POST", body: JSON.stringify({ id: skillsUi.current }) }); await loadSkills(); await editSkill(s.id); toast("Updated from the folder"); }
  catch (err) { toast(err.message); }
}); $("#skillBody").addEventListener("input", updateSkillCount);
$("#skillFile").addEventListener("change", (e) => { if (e.target.files[0]) importSkill(e.target.files[0]); e.target.value = ""; });
$("#trustBtn").addEventListener("click", trustChat);
$("#untrustedBadge").addEventListener("click", openSession);

// Folder access
function chatFolders() { return state.chat ? (state.chat.folders ||= []) : state.draftFolders; }
function renderFolderList() {
  $("#folderList").innerHTML = chatFolders().map((f, i) => `<div class="folder-row" data-i="${i}">${ic("folder", "sm")}<span class="p" title="${esc(f.path)}">&lrm;${esc(f.path)}</span><label>Allow edits <input type="checkbox" class="fw small" ${f.write ? "checked" : ""}></label><button class="icon-btn sm fdel" type="button" title="Remove" aria-label="Remove folder">${ic("x", "sm")}</button></div>`).join("") || '<div class="help">No folders yet, so this chat can\'t see any of your files.</div>';
}
// Folders and permissions are server-owned: normal chat saves can't change them, so they go
// through /api/session/permissions, and the window shows what the server actually stored.
async function savePermissions(chat, changes) {
  const r = await api("/api/session/permissions", { method: "POST", body: JSON.stringify({ chatId: chat.id, ...changes }) });
  chat.folders = r.folders; chat.capabilities = r.capabilities; chat.skills = r.skills; chat.projectId = r.projectId; if (state.chat === chat) updateSubtitle(); return r;
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
  updateSubtitle();
}
function toggleFolderPop(open) { $("#folderPop").classList.toggle("open", open); if (open) { renderFolderList(); $("#folderPath").focus(); } }

// Events
$("#folderBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleFolderPop(!$("#folderPop").classList.contains("open")); });
document.addEventListener("click", (e) => { if (!e.target.closest("#folderPop, #folderBtn")) toggleFolderPop(false); });
$("#addFolder").addEventListener("click", () => addFolder($("#folderPath").value));
$("#folderPath").addEventListener("keydown", (e) => { if (e.key === "Enter") addFolder($("#folderPath").value); else if (e.key === "Escape") { e.stopPropagation(); toggleFolderPop(false); $("#input").focus(); } });
$("#browseFolder").addEventListener("click", async () => {
  const b = $("#browseFolder"); b.disabled = true; b.textContent = "Choosing…";
  try {
    // In the Electron app this is the native Windows folder dialog; in a browser window the server opens one.
    const picked = window.openay?.pickFolder ? await window.openay.pickFolder() : (await api("/api/pick-folder", { method: "POST" })).path;
    if (picked) addFolder(picked);
  } catch (e) { toast(e.message); }
  finally { b.disabled = false; b.textContent = "Browse…"; }
});
$("#folderList").addEventListener("click", (e) => {
  const row = e.target.closest(".folder-row"); if (!row) return; const i = +row.dataset.i; const list = chatFolders();
  if (e.target.closest(".fdel")) { list.splice(i, 1); foldersChanged(); } else if (e.target.classList.contains("fw")) { list[i].write = e.target.checked; foldersChanged(); }
});
function autoGrow() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 240) + "px"; updateSendBtn(); }
$("#input").addEventListener("input", autoGrow);
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("#input").addEventListener("paste", (e) => { const files = [...(e.clipboardData?.files || [])]; if (files.length) { e.preventDefault(); addFiles(files); } });
$("#sendBtn").addEventListener("click", () => busy() ? stop() : send()); $("#attachBtn").addEventListener("click", () => $("#fileInput").click());
$("#fileInput").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
$("#attachments").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { state.attachments.splice(+b.dataset.i, 1); renderAttachments(); } });
$("#docList").addEventListener("click", (e) => { const b = e.target.closest("[data-doc]"); if (b) { state.documents.splice(+b.dataset.doc, 1); renderDocs(); } });
$("#planBtn").addEventListener("click", () => { setPlan(!state.plan); $("#input").focus(); });
let dragDepth = 0;
addEventListener("dragenter", (e) => { if (e.dataTransfer?.types.includes("Files")) { dragDepth++; $("#drop").classList.add("show"); } });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop").classList.remove("show"); } });
addEventListener("dragover", (e) => e.preventDefault()); addEventListener("drop", (e) => { e.preventDefault(); dragDepth = 0; $("#drop").classList.remove("show"); addFiles(e.dataTransfer.files); });
$("#newChat").addEventListener("click", newChat); $("#search").addEventListener("input", searchChats);
$("#chatList").addEventListener("click", async (e) => {
  const item = e.target.closest(".chat-item"); if (!item) return; if (e.target.closest(".del")) { deleteChat(item.dataset.id); return; }
  await openChat(item.dataset.id);
  const at = item.dataset.index; if (at === "") return;  // a search hit: show the matching message
  const el = $(`#messages [data-idx="${at}"]`); if (el) { el.scrollIntoView({ block: "center" }); el.classList.add("found"); setTimeout(() => el.classList.remove("found"), 1600); }
});
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
// Close buttons (×) in dialog headers. "Later" semantics: approvals keep waiting on the server.
document.addEventListener("click", (e) => { const b = e.target.closest("[data-close]"); if (b) b.closest(".back")?.classList.remove("open"); });
$("#connBadge").addEventListener("click", () => { toast("Checking your Ollama balance…"); loadBalance(true).then(() => { const b = state.balance?.balance; toast(b && "totalUsd" in b ? `Balance $${b.totalUsd.toFixed(2)}` : (b?.error || "Balance unavailable")); }); });
$("#messages").addEventListener("click", async (e) => {
  const sg = e.target.closest("[data-suggest]");
  if (sg) { const s = SUGGESTIONS[+sg.dataset.suggest]; $("#input").value = s[3]; autoGrow(); $("#input").focus(); if (s[4] && !chatFolders().length) { e.stopPropagation(); toggleFolderPop(true); } return; }
  const oc = e.target.closest("[data-open-chat]"); if (oc) { e.preventDefault(); openChat(oc.dataset.openChat); return; }
  if (e.target.closest("[data-action-review]")) { if (state.actionApproval) $("#actionApproveBack").classList.add("open"); return; }
  if (e.target.closest("[data-action-approve]")) { decideActionApproval(false); return; }
  if (e.target.closest("[data-model-review]")) { if (state.modelApproval) $("#modelApproveBack").classList.add("open"); return; }
  if (e.target.closest("[data-model-approve]")) { decideModelApproval(false); return; }
  if (e.target.closest("[data-review]")) { openApprovalPreview(); return; }
  const ap = e.target.closest("[data-approve]"); if (ap) { decideApproval(ap.dataset.approve === "yes"); return; }
  const cc = e.target.closest(".copy-code");
  if (cc) { await navigator.clipboard.writeText(cc.parentElement.querySelector("code").innerText); cc.textContent = "Copied"; setTimeout(() => (cc.textContent = "Copy"), 1200); return; }
  if (e.target.closest("[data-show-summary]")) { const box = e.target.closest(".summary-divider").nextElementSibling; box.hidden = !box.hidden; return; }
  if (e.target.closest("[data-carry-out]")) { send("Go ahead and carry out the plan."); return; }
  const un = e.target.closest("[data-undo]"); if (un) { openUndo(un.dataset.undo); return; }
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
    else if (document.querySelector("#projectsBack.open, #schedBack.open, #undoBack.open")) document.querySelectorAll("#projectsBack, #schedBack, #undoBack").forEach((b) => b.classList.remove("open"));
    else if ($("#folderPop").classList.contains("open")) toggleFolderPop(false); else if ($("#modalBack").classList.contains("open")) $("#modalBack").classList.remove("open"); else if (state.editIndex !== null) { state.editIndex = null; updateEditBanner(); } else if (busy()) stop();
  }
});
setInterval(() => fetch("/api/ping").catch(() => {}), 5000); fetch("/api/ping").catch(() => {});
setInterval(pollRuns, 3000);
async function loadBalance(force) {
  try { state.balance = await api(force ? "/api/balance/check" : "/api/balance", force ? { method: "POST", body: "{}" } : undefined); renderUsage(); renderPause(); if ($("#modalBack").classList.contains("open")) renderSettingsBalance(); } catch {}
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

// Undo this reply: shows what will be put back, then does it (undo.py on the server)
async function openUndo(runId) {
  const chat = state.chat; if (!chat || busy()) { toast("Wait for the reply to finish first"); return; }
  let r; try { r = await api(`/api/undo?chat=${encodeURIComponent(chat.id)}&run=${encodeURIComponent(runId)}`); } catch (e) { toast(e.message); return; }
  if (!r.steps.length) { toast("Nothing left to undo for this reply"); return; }
  $("#undoList").innerHTML = r.steps.map((s) => `<div class="undo-step ${s.ok ? "" : "skip"}">${ic(s.ok ? "undo" : "x", "sm")}<span>${esc(s.text)}</span></div>`).join("");
  $("#undoConfirm").disabled = !r.steps.some((s) => s.ok); $("#undoConfirm").dataset.run = runId;
  $("#undoBack").classList.add("open");
}
$("#undoConfirm").addEventListener("click", async () => {
  const chat = state.chat; const runId = $("#undoConfirm").dataset.run; if (!chat) return;
  try {
    const r = await api("/api/runs/undo", { method: "POST", body: JSON.stringify({ chatId: chat.id, runId }) });
    const done = r.results.filter((x) => x.ok).length, skipped = r.results.length - done;
    toast(`Undid ${done} change${done === 1 ? "" : "s"}${skipped ? `; ${skipped} skipped` : ""}. Earlier versions are in Trash.`);
  } catch (e) { toast(e.message); }
  $("#undoBack").classList.remove("open"); await loadUndoRuns(); renderMessages();
});
if (window.openay?.updates) window.openay.updates.onStatus(renderUpdateState);
$("#downloadUpdate").addEventListener("click", async () => { if (window.openay?.updates) renderUpdateState(await window.openay.updates.download()); });
$("#installUpdate").addEventListener("click", () => { if (window.openay?.updates) window.openay.updates.install(); });
(async function init() {
  updateSendBtn(); await loadConfig().catch((e) => toast(e.message)); await Promise.all([loadModels(), loadChats(), pollRuns(), loadUsage(), loadBalance(false)]); renderMessages();
  if (document.activeElement === document.body) $("#input").focus();
})();
