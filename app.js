"use strict";
const $ = (s) => document.querySelector(s);
const ICON = {
  send: "↑", stop: "■", copy: "⧉", redo: "↻", edit: "✎", trash: "🗑",
};
const state = {
  config: {}, models: [], modelErrors: [], chats: [], chat: null,
  streaming: null, attachments: [], kbIndex: -1, draftFolders: [],
  approval: null, saveQueues: new Map(), saveSeq: new Map(), navSeq: 0,
  clearKey: false, updateUrl: "", sessionDraft: null,
};
const DEFAULT_CAPABILITIES = { editMode: "review", fileManage: false, deleteFiles: false, modelControl: "ask" };
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
function setModel(name) { state.selectedModel = name; if (state.chat) state.chat.model = name; updateModelBtn(); }
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

// Chats and ordered persistence
async function loadChats() { state.chats = await api("/api/chats").catch(() => []); renderChatList(); }
function renderChatList() {
  const q = $("#search").value.trim().toLowerCase(); const items = state.chats.filter((c) => c.title.toLowerCase().includes(q));
  $("#chatList").innerHTML = items.map((c) => `<div class="chat-item ${state.chat?.id === c.id ? "active" : ""}" data-id="${c.id}" title="${esc(c.model || "")}"><span class="t">${esc(c.title)}</span><button class="icon-btn del" title="Delete chat">${ICON.trash}</button></div>`).join("") || `<div style="color:var(--muted);font-size:13px;padding:8px 10px">${q ? "No matches" : "No chats yet"}</div>`;
}
function updateChatMeta(snapshot) {
  const i = state.chats.findIndex((x) => x.id === snapshot.id); const meta = { id: snapshot.id, title: snapshot.title, updated: snapshot.updated, model: snapshot.model };
  if (i >= 0) state.chats.splice(i, 1); state.chats.unshift(meta); state.chats.sort((a, b) => b.updated - a.updated); renderChatList();
}
async function saveChat(chat = state.chat) {
  if (!chat) return; const id = chat.id; const seq = (state.saveSeq.get(id) || 0) + 1; state.saveSeq.set(id, seq); chat.updated = Date.now();
  let json; try { json = JSON.stringify(chat, (k, v) => k.startsWith("_") ? undefined : v); } catch (e) { toast("Save failed: " + e.message); return; }
  const snapshot = JSON.parse(json); const previous = state.saveQueues.get(id) || Promise.resolve();
  const operation = previous.catch(() => {}).then(async () => { if (seq !== state.saveSeq.get(id)) return; await api("/api/chats/" + id, { method: "PUT", body: json }); updateChatMeta(snapshot); });
  state.saveQueues.set(id, operation);
  try { await operation; } catch (e) { toast("Save failed: " + e.message); throw e; }
  finally { if (state.saveQueues.get(id) === operation) state.saveQueues.delete(id); }
}
async function flushChat(chat) {
  if (!chat) return;
  while (state.saveQueues.has(chat.id)) { const pending = state.saveQueues.get(chat.id); try { await pending; } catch { return; } if (state.saveQueues.get(chat.id) === pending) break; }
}
async function stopAndWait() {
  const session = state.streaming; if (!session) return; session.ctrl.abort(); if (state.approval?.session === session) decideApproval(false); try { await session.done; } catch {}
}
function newChat() {
  state.navSeq++; if (state.streaming) stop(); state.chat = null; state.draftFolders = [];
  state.selectedModel = state.config.defaultModel || state.models[0]?.name || "";
  renderMessages(); renderChatList(); updateModelBtn(); updateFolderBtn(); toggleFolderPop(false); $("#title").textContent = ""; $("#input").focus();
}
async function openChat(id) {
  const ticket = ++state.navSeq; if (state.streaming) await stopAndWait(); let chat;
  try { chat = await api("/api/chats/" + id); } catch (e) { toast(e.message); return; }
  if (ticket !== state.navSeq) return; state.chat = chat; state.selectedModel = chat.model;
  updateModelBtn(); renderMessages(); renderChatList(); updateFolderBtn(); toggleFolderPop(false); $("#title").textContent = state.chat.title; scrollBottom(true);
}
async function deleteChat(id) {
  if (!confirm("Delete this chat?")) return; const deletingCurrent = state.chat?.id === id;
  if (deletingCurrent && state.streaming) await stopAndWait();
  try { await api("/api/chats/" + id, { method: "DELETE" }); } catch (e) { toast("Delete failed: " + e.message); return; }
  state.chats = state.chats.filter((c) => c.id !== id); if (deletingCurrent) newChat(); else renderChatList();
}

// Rendering
const TOOL_LABEL = { list_directory: "Listed folder", read_file: "Read file", search_files: "Searched files", write_file: "Wrote file", edit_file: "Edited file", create_folder: "Created folder", copy_path: "Copied item", move_path: "Moved/renamed item", delete_path: "Moved to trash", restore_path: "Restored item", set_session_model: "Changed model" };
function toolHTML(m, idx) {
  if (m._modelApproval) return `<div class="approve" data-idx="${idx}"><div class="q">Switch this session to ${esc(m._modelApproval.model)}?</div><div class="detail">Nothing has changed yet. The model is waiting for your approval.</div><div class="btns"><button class="btn primary" data-model-review="yes">Review change</button><button class="btn" data-model-approve="no">Keep current model</button></div></div>`;
  if (m._approval) return `<div class="approve" data-idx="${idx}"><div class="q">Review requested change to ${esc(m._approval.path)}</div><div class="detail">Nothing has been written. Review the complete change before deciding.</div><div class="btns"><button class="btn primary" data-review="yes">Review change</button><button class="btn" data-approve="no">Deny</button></div></div>`;
  const status = m._running ? "running…" : m.isError ? "failed" : "";
  return `<details class="tool ${m.isError ? "err" : ""}" data-idx="${idx}"><summary><b>${esc(TOOL_LABEL[m.tool_name] || m.tool_name || "Tool")}</b><span class="arg">${esc(m.display || "")}</span><span>${status}</span></summary><div class="out">${esc(m.content || "")}</div></details>`;
}
function msgHTML(m, idx, isLast) {
  if (m.role === "system") return ""; if (m.role === "tool") return toolHTML(m, idx);
  if (m.role === "user") {
    const imgs = (m.images || []).map((b) => `<img src="data:image/*;base64,${b}">`).join("");
    return `<div class="msg user" data-idx="${idx}">${imgs ? `<div class="imgs">${imgs}</div>` : ""}<div class="bubble">${esc(m.content)}</div><div class="actions"><button class="icon-btn act-copy" title="Copy">${ICON.copy}</button><button class="icon-btn act-edit" title="Edit & resend">${ICON.edit}</button></div></div>`;
  }
  const thinking = m.thinking ? `<details class="thinking" ${m._live && !m.content ? "open" : ""}><summary>${m._live && !m.content ? "Thinking…" : "Thought process"}</summary><div class="tbody">${esc(m.thinking)}</div></details>` : "";
  const stats = m.stats ? `<span>· ${m.stats}</span>` : ""; const body = m.content ? renderMarkdown(m.content) : "";
  if (m.tool_calls?.length && !m._live) return `<div class="msg assistant step" data-idx="${idx}" style="margin-bottom:4px">${thinking}${body ? `<div class="content">${body}</div>` : ""}</div>`;
  return `<div class="msg assistant ${isLast ? "last" : ""}" data-idx="${idx}"><div class="meta"><span>${esc(m.model || "")}</span>${stats}</div>${thinking}<div class="content ${m._live ? "cursor" : ""}">${body}</div>${m.error ? `<div class="err">${linkify(m.error)}</div>` : ""}${m._live ? "" : `<div class="actions"><button class="icon-btn act-copy" title="Copy">${ICON.copy}</button>${isLast ? `<button class="icon-btn act-regen" title="Regenerate">${ICON.redo}</button>` : ""}</div>`}</div>`;
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
  if (state.streaming) { b.textContent = ICON.stop; b.title = "Stop"; b.disabled = false; }
  else { b.textContent = ICON.send; b.title = "Send (Enter)"; b.disabled = !$("#input").value.trim() && !state.attachments.length; }
}

// Sending and streaming
async function send() {
  const text = $("#input").value.trim(); if (state.streaming || (!text && !state.attachments.length)) return;
  const model = currentModel(); if (!model) { toast("Pick a cloud model first"); openModelMenu(true); return; }
  if (!state.chat) {
    state.chat = { id: uid(), title: (text || "Image").slice(0, 60).replace(/\s+/g, " "), model, created: Date.now(), messages: [], folders: state.draftFolders, capabilities: { ...DEFAULT_CAPABILITIES } };
    state.draftFolders = []; $("#title").textContent = state.chat.title;
  }
  const chat = state.chat; const userMsg = { role: "user", content: text }; if (state.attachments.length) userMsg.images = state.attachments.slice();
  chat.messages.push(userMsg); $("#input").value = ""; autoGrow(); state.attachments = []; renderAttachments();
  try { await saveChat(chat); await runAssistant(chat); } catch (e) { toast(e.message); }
}
async function runAssistant(chat = state.chat) {
  if (!chat) return; if (chat._run) return chat._run;
  const ctrl = new AbortController(); const session = { ctrl, chatId: chat.id, done: null }; state.streaming = session; updateSendBtn();
  let useTools = true; const rerender = () => { if (state.chat === chat) { renderMessages(); scrollBottom(); } };
  session.done = (async () => {
    try {
      await flushChat(chat);
      for (let step = 0; step < 25; step++) {
        const model = chat.model || currentModel(); chat.model = model;
        const reply = await streamReply(chat, model, ctrl, useTools);
        if (reply === "retry-no-tools") { useTools = false; toast("This model doesn't support tools for this reply"); continue; }
        if (ctrl.signal.aborted || reply.error || !reply.tool_calls?.length) break;
        for (const call of reply.tool_calls) { if (ctrl.signal.aborted) break; await runToolCall(chat, call, ctrl, session, rerender); }
        if (ctrl.signal.aborted) break; await saveChat(chat);
      }
    } finally { rerender(); try { await saveChat(chat); } catch {} if (state.streaming === session) { state.streaming = null; updateSendBtn(); } }
  })();
  chat._run = session.done; try { await session.done; } finally { if (chat._run === session.done) delete chat._run; }
}
function historyFor(chat) {
  return chat.messages.filter((m) => !(m.role === "assistant" && m.error && !m.content && !m.tool_calls)).map((m) => {
    const o = { role: m.role, content: m.content || "" }; if (m.images) o.images = m.images; if (m.tool_calls) o.tool_calls = m.tool_calls; if (m.tool_name) o.tool_name = m.tool_name; return o;
  });
}
async function streamReply(chat, model, ctrl, useTools) {
  const body = { chatId: chat.id, model, messages: historyFor(chat), stream: true, toolsEnabled: useTools };
  const t = $("#thinkSel").value; if (t === "true") body.think = true; else if (t === "false") body.think = false; else if (t !== "default") body.think = t;
  const reply = { role: "assistant", content: "", thinking: "", model, _live: true }; chat.messages.push(reply);
  if (state.chat === chat) { renderMessages(); scrollBottom(true); }
  let raf = 0;
  const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; if (state.chat === chat) { const nb = nearBottom(); renderLive(); if (nb) scrollBottom(true); } }); };
  try {
    const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: ctrl.signal });
    if (!r.ok) {
      const j = await r.json().catch(() => ({})); const msg = j.error || `HTTP ${r.status}`;
      if (useTools && /does not support tools/i.test(msg)) { chat.messages.pop(); return "retry-no-tools"; } throw new Error(msg);
    }
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const { value, done } = await reader.read(); if (done) break; buf += dec.decode(value, { stream: true }); let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim(); buf = buf.slice(nl + 1); if (!line) continue;
        let j; try { j = JSON.parse(line); } catch { continue; } if (j.error) throw new Error(j.error);
        if (j.message?.content) reply.content += j.message.content; if (j.message?.thinking) reply.thinking += j.message.thinking;
        if (j.message?.tool_calls?.length) reply.tool_calls = (reply.tool_calls || []).concat(j.message.tool_calls);
        if (j.done && j.eval_count && j.eval_duration) reply.stats = `${j.eval_count} tokens · ${(j.eval_count / (j.eval_duration / 1e9)).toFixed(1)} tok/s`;
        schedule();
      }
    }
  } catch (e) { if (e.name === "AbortError") reply.stats = (reply.stats ? reply.stats + " · " : "") + "stopped"; else reply.error = e.message; }
  finally { cancelAnimationFrame(raf); delete reply._live; if (!reply.thinking) delete reply.thinking; } return reply;
}

// Tools and approval
async function runToolCall(chat, call, ctrl, session, rerender) {
  const name = call.function?.name || ""; let args = call.function?.arguments || {};
  if (typeof args === "string") { try { args = JSON.parse(args); } catch { args = {}; } }
  if (!args || typeof args !== "object" || Array.isArray(args)) args = {};
  const tm = { role: "tool", tool_name: name, content: "", display: args.path || args.source || args.model || args.pattern || args.contains || "", _running: true };
  if (name === "search_files" && args.contains) tm.display = `"${args.contains}"` + (args.path ? " in " + args.path : "");
  chat.messages.push(tm); let approvalToken = "";
  try {
    if ((name === "write_file" || name === "edit_file") && (chat.capabilities?.editMode || "review") === "review") {
      const prepared = await api("/api/tool/prepare", { method: "POST", body: JSON.stringify({ chatId: chat.id, name, arguments: args }) }); const a = prepared.approval;
      if (ctrl.signal.aborted) { cancelApprovalOnServer(chat.id, a.token); tm.content = "The write was cancelled."; tm.isError = true; return; }
      tm.display = a.path; tm._approval = { path: a.path }; rerender(); const approved = await requestApproval(a, chat, session, tm); delete tm._approval;
      if (!approved) { tm.content = ctrl.signal.aborted ? "The write was cancelled." : "The user denied this write."; tm.isError = true; return; } approvalToken = a.token;
    }
    rerender(); const payload = { chatId: chat.id, name, arguments: args }; if (approvalToken) payload.approvalToken = approvalToken;
    let r = await api("/api/tool", { method: "POST", body: JSON.stringify(payload) });
    if (name === "set_session_model" && r.needsApproval) {
      tm._modelApproval = { model: r.model }; rerender();
      const ok = await requestModelApproval(r, chat, session); delete tm._modelApproval;
      if (!ok) { cancelApprovalOnServer(chat.id, r.approvalToken); tm.content = ctrl.signal.aborted ? "The model change was cancelled." : "The user denied the model change."; tm.isError = true; return; }
      payload.approvalToken = r.approvalToken;
      r = await api("/api/tool", { method: "POST", body: JSON.stringify(payload) });
    }
    tm.content = r.content; if (r.error) tm.isError = true;
    if (name === "set_session_model" && !r.error && !r.needsApproval && r.model) {
      chat.model = r.model; state.selectedModel = r.model; updateModelBtn();
      const meta = state.chats.find((c) => c.id === chat.id); if (meta) meta.model = r.model;
    }
  } catch (e) { if (approvalToken) cancelApprovalOnServer(chat.id, approvalToken); tm.content = "Error: " + e.message; tm.isError = true; }
  finally { delete tm._running; delete tm._approval; delete tm._modelApproval; rerender(); }
}
function cancelApprovalOnServer(chatId, token) { if (token) api("/api/tool/cancel", { method: "POST", body: JSON.stringify({ chatId, approvalToken: token }) }).catch(() => {}); }
function requestApproval(approval, chat, session, tm) {
  return new Promise((resolve) => {
    state.approval = { approval, chat, session, tm, resolve, mode: approval.exists ? "diff" : "content", decided: false };
    session.ctrl.signal.addEventListener("abort", () => { if (state.approval?.approval.token === approval.token) decideApproval(false); }, { once: true }); openApprovalPreview();
  });
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
  if (!approved) cancelApprovalOnServer(pending.chat.id, pending.approval.token); pending.resolve(Boolean(approved));
}
// Model-change approval (set_session_model in "ask" mode)
function requestModelApproval(r, chat, session) {
  return new Promise((resolve) => {
    const pending = { resolve, decided: false, chatId: chat.id }; state.modelApproval = pending;
    $("#maFrom").textContent = r.previous || "none"; $("#maTo").textContent = r.model;
    $("#maReason").textContent = r.reason || "No reason given.";
    const caps = (r.capabilities || []).filter((c) => c !== "completion");
    $("#maCaps").textContent = caps.length ? `${r.model} supports: ${caps.join(", ")}` : "";
    $("#maWarn").textContent = (r.warnings || []).join(" "); $("#maWarn").hidden = !(r.warnings || []).length;
    $("#modelApproveBack").classList.add("open"); $("#maAllow").focus();
    session.ctrl.signal.addEventListener("abort", () => { if (state.modelApproval === pending) decideModelApproval(false); }, { once: true });
  });
}
function decideModelApproval(approved) {
  const pending = state.modelApproval; if (!pending || pending.decided) return;
  pending.decided = true; state.modelApproval = null; $("#modelApproveBack").classList.remove("open"); pending.resolve(Boolean(approved));
}
function stop() { state.streaming?.ctrl.abort(); }
function regenerate() {
  if (state.streaming || !state.chat) return; const chat = state.chat; const msgs = chat.messages;
  while (msgs.length && msgs[msgs.length - 1].role !== "user") msgs.pop(); saveChat(chat).then(() => runAssistant(chat)).catch((e) => toast(e.message));
}
function editMessage(idx) {
  if (state.streaming) return; const m = state.chat.messages[idx]; $("#input").value = m.content; autoGrow(); state.attachments = (m.images || []).slice(); renderAttachments();
  state.chat.messages.splice(idx); saveChat(state.chat).catch(() => {}); renderMessages(); updateSendBtn(); $("#input").focus();
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
  const body = { defaultModel: $("#defaultModel").value }; const key = $("#apiKey").value.trim(); if (key) body.apiKey = key;
  if (state.clearKey && !key) body.clearApiKey = true;
  try {
    const saved = await api("/api/config", { method: "POST", body: JSON.stringify(body) }); const oldDefault = state.config.defaultModel; state.config = saved;
    if (!state.chat && oldDefault !== saved.defaultModel) { state.selectedModel = saved.defaultModel || state.models[0]?.name || ""; renderMessages(); updateModelBtn(); }
    $("#modalBack").classList.remove("open"); await loadModels(); toast("Settings saved");
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
  $("#sessionBack").classList.add("open"); $("#sessionName").focus();
}
async function saveSession() {
  const draft = state.sessionDraft; if (!draft || !state.chat || draft.id !== state.chat.id) return;
  const title = $("#sessionName").value.trim();
  const c = { editMode: $("#editMode").value, fileManage: $("#fileManage").checked,
              deleteFiles: $("#deleteFiles").checked, modelControl: $("#modelControl").value };
  try {
    if (title !== state.chat.title) {
      const result = await api("/api/session/rename", { method: "POST", body: JSON.stringify({ chatId: state.chat.id, title }) });
      state.chat.title = result.title; $("#title").textContent = result.title;
    }
    await savePermissions(state.chat, { capabilities: c }); await loadChats();
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

// Folder access
function chatFolders() { return state.chat ? (state.chat.folders ||= []) : state.draftFolders; }
function renderFolderList() {
  $("#folderList").innerHTML = chatFolders().map((f, i) => `<div class="folder-row" data-i="${i}"><span class="p" title="${esc(f.path)}">&lrm;${esc(f.path)}</span><label><input type="checkbox" class="fw" ${f.write ? "checked" : ""}> Allow edits</label><button class="icon-btn fdel" title="Remove">${ICON.trash}</button></div>`).join("") || '<div class="help" style="margin:0 0 4px">No folders yet, so this chat can\'t see any of your files.</div>';
}
// Folders and permissions are server-owned: normal chat saves can't change them, so they go
// through /api/session/permissions, and the window shows what the server actually stored.
async function savePermissions(chat, changes) {
  await flushChat(chat);
  const r = await api("/api/session/permissions", { method: "POST", body: JSON.stringify({ chatId: chat.id, ...changes }) });
  chat.folders = r.folders; chat.capabilities = r.capabilities; return r;
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
$("#sendBtn").addEventListener("click", () => state.streaming ? stop() : send()); $("#attachBtn").addEventListener("click", () => $("#fileInput").click());
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
$("#modelOptions").addEventListener("click", (e) => { const o = e.target.closest(".opt"); if (!o) return; setModel(o.dataset.name); openModelMenu(false); if (state.chat) saveChat().catch(() => {}); else renderMessages(); $("#input").focus(); });
document.addEventListener("click", (e) => { if (!e.target.closest(".picker")) openModelMenu(false); });
$("#thinkSel").addEventListener("change", (e) => api("/api/config", { method: "POST", body: JSON.stringify({ think: e.target.value }) }).catch(() => {}));
$("#messages").addEventListener("click", async (e) => {
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
    if (state.modelApproval) decideModelApproval(false); else if ($("#trashBack").classList.contains("open")) $("#trashBack").classList.remove("open"); else if (state.approval) decideApproval(false); else if ($("#sessionBack").classList.contains("open")) $("#sessionBack").classList.remove("open");
    else if ($("#folderPop").classList.contains("open")) toggleFolderPop(false); else if ($("#modalBack").classList.contains("open")) $("#modalBack").classList.remove("open"); else if (state.streaming) stop();
  }
});
setInterval(() => fetch("/api/ping").catch(() => {}), 5000); fetch("/api/ping").catch(() => {});
(async function init() {
  updateSendBtn(); await loadConfig().catch((e) => toast(e.message)); await Promise.all([loadModels(), loadChats()]); renderMessages();
  if (document.activeElement === document.body) $("#input").focus();
})();
