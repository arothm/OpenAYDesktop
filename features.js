"use strict";
// Open AY Desktop, part 2 of the window: projects, scheduled tasks, the terminal and preview panel,
// export, dictation and quick entry. Uses the helpers and state from app.js (loaded first).

// ---------------------------------------------------------------- projects
const proj = { current: null, folders: [] };
async function loadProjects() {
  try { state.projects = (await api("/api/projects")).projects; } catch { state.projects = []; }
  if (state.projectFilter && !state.projects.some((p) => p.id === state.projectFilter)) state.projectFilter = "";
  renderProjectPicker(); renderChatList();
}
function renderProjectPicker() {
  const p = state.projects.find((x) => x.id === state.projectFilter);
  $("#projectBtn .name").textContent = p ? p.name : "All chats";
  $("#projectBtn").classList.toggle("on", Boolean(p));
  $("#projectMenu").innerHTML = `<button type="button" role="menuitem" data-proj="" class="${p ? "" : "sel"}">${ic("project", "sm")}All chats</button>`
    + state.projects.map((x) => `<button type="button" role="menuitem" data-proj="${esc(x.id)}" class="${x.id === state.projectFilter ? "sel" : ""}">${ic("folder", "sm")}${esc(x.name)}</button>`).join("")
    + `<div class="menu-sep"></div><button type="button" role="menuitem" data-proj-manage>${ic("settings", "sm")}Manage projects…</button>`;
}
$("#projectBtn").addEventListener("click", (e) => { e.stopPropagation(); $("#projectMenu").classList.toggle("open"); });
document.addEventListener("click", (e) => { if (!e.target.closest(".proj-picker")) $("#projectMenu").classList.remove("open"); });
$("#projectMenu").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return; $("#projectMenu").classList.remove("open");
  if (b.hasAttribute("data-proj-manage")) { openProjects(state.projectFilter); return; }
  state.projectFilter = b.dataset.proj || ""; renderProjectPicker(); renderChatList();
  if (state.chat && state.projectFilter && state.chat.projectId !== state.projectFilter) newChat();
  toast(state.projectFilter ? "New chats start in this project" : "Showing all chats");
});

function openProjects(id) { openSettings("secProjects", { project: id }); }
async function showProjects(id) {  // Settings > Projects
  await loadProjects();
  const pick = [id, proj.current, state.projects[0]?.id].find((x) => x && state.projects.some((p) => p.id === x));
  editProject(pick || null);
}
function renderProjectList() {
  $("#projectList").innerHTML = state.projects.map((p) => `<button class="skill-item ${p.id === proj.current ? "sel" : ""}" data-pid="${esc(p.id)}" type="button"><span class="av c${hashIndex(p.id, 4)}">${esc(initials(p.name))}</span><span class="tx"><div class="n">${esc(p.name)}</div><div class="d">${(p.folders || []).length} folder${(p.folders || []).length === 1 ? "" : "s"}</div></span></button>`).join("")
    || '<div class="skill-list-note">No projects yet. Create one to group chats that share folders, instructions and memory.</div>';
}
async function editProject(id) {
  proj.current = id; const p = state.projects.find((x) => x.id === id) || { name: "", instructions: "", folders: [], capabilities: {}, skills: [] };
  $("#projName").value = p.name; $("#projInstructions").value = p.instructions || ""; proj.folders = (p.folders || []).map((f) => ({ ...f }));
  const c = p.capabilities || {};
  $("#projMode").value = c.mode || (c.editMode ? modeOf(c) : "");
  $("#projDelete").disabled = !id; $("#projNewChat").disabled = !id; $("#projMemory").value = ""; $("#projMemory").disabled = !id;
  $("#projMemory").placeholder = id ? "- Ships on Fridays" : "Save the project first";
  renderProjFolders(); renderProjectList();
  try {
    const sk = (await api("/api/skills")).skills; const on = new Set(p.skills || []);
    $("#projSkills").innerHTML = sk.map((s) => `<label class="skill-check"><span>${esc(s.name)}<span class="d">${esc(s.description)}</span></span><input type="checkbox" data-skill="${esc(s.id)}" ${on.has(s.id) ? "checked" : ""}></label>`).join("") || '<span class="help">No skills yet.</span>';
  } catch {}
  if (id) { try { const m = await api("/api/memory?scope=" + encodeURIComponent(id)); if (proj.current === id) { $("#projMemory").value = m.text; proj.memory = m.text; } } catch {} }
}
function renderProjFolders() {
  $("#projFolders").innerHTML = proj.folders.map((f, i) => `<div class="folder-row" data-i="${i}">${ic("folder", "sm")}<span class="p" title="${esc(f.path)}">&lrm;${esc(f.path)}</span><label>Allow edits <input type="checkbox" class="small" data-pw="${i}" ${f.write ? "checked" : ""}></label><button class="icon-btn sm" type="button" data-pdel="${i}" aria-label="Remove folder">${ic("x", "sm")}</button></div>`).join("")
    || '<div class="help">No folders: chats in this project start without file access.</div>';
}
$("#projFolders").addEventListener("click", (e) => {
  const d = e.target.closest("[data-pdel]"); if (d) { proj.folders.splice(+d.dataset.pdel, 1); renderProjFolders(); return; }
  const w = e.target.closest("[data-pw]"); if (w) proj.folders[+w.dataset.pw].write = w.checked;
});
function projAddFolder(raw) { const p = String(raw || "").trim().replace(/^"|"$/g, ""); if (!p) return; if (!proj.folders.some((f) => f.path.toLowerCase() === p.toLowerCase())) proj.folders.push({ path: p, write: false }); $("#projFolderPath").value = ""; renderProjFolders(); }
$("#projAddFolder").addEventListener("click", () => projAddFolder($("#projFolderPath").value));
$("#projBrowse").addEventListener("click", async () => {
  try { const picked = window.openay?.pickFolder ? await window.openay.pickFolder() : (await api("/api/pick-folder", { method: "POST" })).path; if (picked) projAddFolder(picked); } catch (e) { toast(e.message); }
});
$("#projectList").addEventListener("click", (e) => { const b = e.target.closest("[data-pid]"); if (b) editProject(b.dataset.pid); });
$("#projectNew").addEventListener("click", () => { editProject(null); $("#projName").focus(); });
$("#projSave").addEventListener("click", async () => {
  const body = { id: proj.current || undefined, name: $("#projName").value, instructions: $("#projInstructions").value, folders: proj.folders,
    capabilities: $("#projMode").value ? { mode: $("#projMode").value } : {},
    skills: [...document.querySelectorAll("#projSkills [data-skill]")].filter((x) => x.checked).map((x) => x.dataset.skill) };
  try {
    const saved = await api("/api/projects", { method: "POST", body: JSON.stringify(body) });
    if (proj.current && $("#projMemory").value !== (proj.memory ?? ""))
      await api("/api/memory", { method: "POST", body: JSON.stringify({ scope: saved.id, text: $("#projMemory").value }) });
    await loadProjects(); editProject(saved.id); toast("Project saved");
  } catch (e) { toast(e.message); }
});
$("#projDelete").addEventListener("click", async () => {
  const p = state.projects.find((x) => x.id === proj.current); if (!p || !confirm(`Delete the project "${p.name}" and its memory?\n\nIts chats stay, outside any project. Shared folders aren't touched.`)) return;
  try { await api("/api/projects/" + encodeURIComponent(p.id), { method: "DELETE" }); await loadProjects(); editProject(state.projects[0]?.id || null); loadChats(); toast("Project deleted"); } catch (e) { toast(e.message); }
});
$("#projNewChat").addEventListener("click", () => { state.projectFilter = proj.current; renderProjectPicker(); closeSettings(); newChat(); renderChatList(); });

// ---------------------------------------------------------------- scheduled tasks
const sched = { current: null, tasks: [] };
function openSchedules() { openSettings("secSchedules"); }
async function showSchedules() {  // Settings > Scheduled tasks
  await loadSchedules();
  editSchedule(sched.tasks.some((t) => t.id === sched.current) ? sched.current : sched.tasks[0]?.id || null);
}
async function loadSchedules() {
  try { sched.tasks = (await api("/api/schedules")).tasks; } catch (e) { sched.tasks = []; toast(e.message); }
  if (!state.projects.length) await loadProjects();
  renderScheduleList();
}
const WHEN = { daily: "Every day", weekdays: "Weekdays", weekly: "Weekly", hourly: "Every few hours", once: "Once" };
const fmtTime = (ms) => ms ? new Date(ms).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
function renderScheduleList() {
  $("#schedList").innerHTML = sched.tasks.map((t) => `<button class="skill-item ${t.id === sched.current ? "sel" : ""}" data-sid="${esc(t.id)}" type="button"><span class="av c${hashIndex(t.id, 4)}">${ic("clock", "sm")}</span><span class="tx"><div class="n">${esc(t.name)}</div><div class="d">${t.enabled ? (t.nextRun ? "Next: " + esc(fmtTime(t.nextRun)) : WHEN[t.kind]) : "Off"}${t.lastError ? " · last run failed" : ""}</div></span></button>`).join("")
    || '<div class="skill-list-note">No scheduled tasks yet.</div>';
}
function editSchedule(id) {
  sched.current = id; const t = sched.tasks.find((x) => x.id === id) || { name: "", prompt: "", kind: "daily", time: "09:00", day: 0, every: 4, enabled: true, model: state.config.defaultModel || currentModel() };
  $("#schedName").value = t.name; $("#schedPrompt").value = t.prompt; $("#schedKind").value = t.kind; $("#schedTime").value = t.time || "09:00";
  $("#schedDay").value = String(t.day ?? 0); $("#schedEvery").value = t.every || 4; $("#schedAt").value = t.at || "";
  $("#schedProject").innerHTML = '<option value="">No project (no folders)</option>' + state.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  $("#schedProject").value = t.projectId || "";
  const names = [...new Set([t.model, ...state.models.map((m) => m.name)].filter(Boolean))];
  $("#schedModel").innerHTML = names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join(""); $("#schedModel").value = t.model || names[0] || "";
  $("#schedEnabled").checked = t.enabled !== false; $("#schedDelete").disabled = !id; $("#schedRunNow").disabled = !id;
  const last = t.lastRun ? `Last run ${fmtTime(t.lastRun)}${t.lastError ? " failed: " + t.lastError : ""}.` : "Hasn't run yet.";
  $("#schedNext").textContent = (t.nextRun ? `Next run ${fmtTime(t.nextRun)}. ` : "") + last;
  schedKindChanged(); renderScheduleList();
}
function schedKindChanged() {
  const k = $("#schedKind").value;
  $("#schedTime").hidden = !["daily", "weekdays", "weekly"].includes(k); $("#schedDay").hidden = k !== "weekly";
  $("#schedEvery").hidden = k !== "hourly"; $("#schedAt").hidden = k !== "once";
}
$("#schedKind").addEventListener("change", schedKindChanged);
$("#schedList").addEventListener("click", (e) => { const b = e.target.closest("[data-sid]"); if (b) editSchedule(b.dataset.sid); });
$("#schedNew").addEventListener("click", () => { editSchedule(null); $("#schedName").focus(); });
$("#schedSave").addEventListener("click", async () => {
  const body = { id: sched.current || undefined, name: $("#schedName").value, prompt: $("#schedPrompt").value, kind: $("#schedKind").value,
                 time: $("#schedTime").value, day: Number($("#schedDay").value), every: Number($("#schedEvery").value), at: $("#schedAt").value,
                 projectId: $("#schedProject").value || null, model: $("#schedModel").value, enabled: $("#schedEnabled").checked };
  try { const t = await api("/api/schedules", { method: "POST", body: JSON.stringify(body) }); await loadSchedules(); editSchedule(t.id); toast("Task saved"); }
  catch (e) { toast(e.message); }
});
$("#schedDelete").addEventListener("click", async () => {
  if (!sched.current || !confirm("Delete this scheduled task? Chats it already started stay.")) return;
  try { await api("/api/schedules/" + encodeURIComponent(sched.current), { method: "DELETE" }); await loadSchedules(); editSchedule(sched.tasks[0]?.id || null); } catch (e) { toast(e.message); }
});
$("#schedRunNow").addEventListener("click", async () => {
  try {
    const r = await api("/api/schedules/run", { method: "POST", body: JSON.stringify({ id: sched.current }) });
    if (r.lastError) { toast(r.lastError); return; }
    closeSettings(); await loadChats(); openChat(r.lastChat);
  } catch (e) { toast(e.message); }
});

// ---------------------------------------------------------------- terminal and preview panel
const panel = { tab: "terminal", lines: [] };
function togglePanel(open) {
  const show = open ?? $("#panel").hidden; $("#panel").hidden = !show;
  if (show) { refreshPanel(); if (panel.tab === "terminal") $("#termInput").focus(); }
}
function setTab(tab) {
  panel.tab = tab; $("#tabTerminal").classList.toggle("primary", tab === "terminal"); $("#tabPreview").classList.toggle("primary", tab === "preview");
  $("#termPane").hidden = tab !== "terminal"; $("#previewPane").hidden = tab !== "preview"; refreshPanel();
}
function writableFolders() { return (state.chat?.folders || []).filter((f) => f.write); }
function refreshPanel() {
  if ($("#panel").hidden) return;
  const fs = writableFolders(); const sel = $("#termCwd"); const prev = sel.value;
  sel.innerHTML = fs.map((f) => `<option value="${esc(f.path)}">${esc(f.path)}</option>`).join("") || '<option value="">Share a folder with Allow edits first</option>';
  if (fs.some((f) => f.path === prev)) sel.value = prev;
  $("#termInput").disabled = $("#termRun").disabled = !fs.length || !state.chat;
  if (!panel.lines.length) renderTerminal();
  if (panel.tab === "preview") renderPreviewList();
}
function renderTerminal() {
  const ran = (state.chat?.messages || []).filter((m) => m.tool_name === "run_command" && m.content).slice(-5).map((m) => ({ who: "model", text: m.content }));
  const items = panel.lines.length ? panel.lines : ran; const box = $("#termOut");
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  box.innerHTML = items.map((l) => `<div class="term-entry ${l.who}${l.running ? " running" : ""}" data-key="${esc(l.key || "")}"><div class="who">${l.who === "you" ? (l.running ? "Running" : "You ran") : "The model ran"}${l.running && l.jobId ? `<button class="btn sm danger" type="button" data-stop-job="${esc(l.jobId)}">Stop</button>` : ""}</div><pre>${esc(l.text)}</pre></div>`).join("")
    || '<div class="empty-note">Commands you run here, and the last commands the model ran in this chat, appear here.</div>';
  if (atEnd) box.scrollTop = box.scrollHeight;
}
async function followJob(entry) {  // output as it arrives, then the final report (exit code, time)
  while (entry.running) {
    let r;
    try { r = await api(`/api/terminal/job?id=${encodeURIComponent(entry.jobId)}&since=${entry.since}`); }
    catch (e) { entry.running = false; entry.text += "\n" + e.message; break; }
    entry.live += r.output; entry.since = r.next;
    if (r.done) { entry.running = false; entry.text = r.result || entry.text; renderTerminal(); }
    else {  // only the output text changes while it runs, so the Stop button stays put under the pointer
      entry.text = `$ ${r.line}\n(${r.seconds}s)\n\n${entry.live}`;
      const box = $("#termOut"); const pre = box.querySelector(`[data-key="${entry.key}"] pre`);
      if (!pre) renderTerminal();
      else { const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40; pre.textContent = entry.text; if (atEnd) box.scrollTop = box.scrollHeight; }
    }
    if (entry.running) await new Promise((ok) => setTimeout(ok, 400));
  }
  renderTerminal();
}
$("#termForm").addEventListener("submit", async (e) => {
  e.preventDefault(); const command = $("#termInput").value.trim(); if (!command || !state.chat) return;
  $("#termRun").disabled = true; $("#termInput").value = "";
  const entry = { who: "you", text: `$ ${command}\n(starting…)`, running: true, jobId: "", since: 0, live: "", key: uid() }; panel.lines.push(entry); renderTerminal();
  try {
    const r = await api("/api/terminal/start", { method: "POST", body: JSON.stringify({ chatId: state.chat.id, cwd: $("#termCwd").value, command }) });
    entry.jobId = r.jobId; entry.text = `$ ${r.line}\n`; renderTerminal(); followJob(entry);
  } catch (err) { entry.running = false; entry.text = `$ ${command}\n${err.message}`; renderTerminal(); }
  $("#termRun").disabled = false; $("#termInput").focus();
});
$("#termOut").addEventListener("click", (e) => {
  const b = e.target.closest("[data-stop-job]"); if (!b) return; b.disabled = true;
  api("/api/terminal/stop", { method: "POST", body: JSON.stringify({ jobId: b.dataset.stopJob }) }).catch((err) => toast(err.message));
});
// Preview: files this chat created or edited (from its tool steps), shown sandboxed (see server.preview_file)
function changedFiles() {
  const out = [];
  for (const m of state.chat?.messages || []) {
    if (!["write_file", "edit_file", "copy_path", "move_path"].includes(m.tool_name) || m.isError) continue;
    const p = String(m.target || m.display || "").trim(); if (p && !out.includes(p)) out.push(p);
  }
  return out.reverse();
}
function previewUrl(path) {
  const roots = state.chat?.folders || []; const norm = (s) => s.replace(/\//g, "\\").toLowerCase().replace(/\\+$/, "");
  for (let i = 0; i < roots.length; i++) {
    const r = norm(roots[i].path); const p = norm(path);
    if (p.startsWith(r + "\\")) return `/preview/${encodeURIComponent(state.chat.id)}/${i}/${path.slice(r.length + 1).split(/[\\/]/).map(encodeURIComponent).join("/")}`;
  }
  return "";
}
function renderPreviewList() {
  const files = changedFiles(); const sel = $("#previewPick"); const prev = sel.value;
  sel.innerHTML = files.map((f) => `<option value="${esc(f)}">${esc(f.split(/[\\/]/).slice(-2).join("/"))}</option>`).join("") || '<option value="">No files changed in this chat yet</option>';
  if (files.includes(prev)) sel.value = prev;
  showPreview();
}
async function showPreview() {
  const path = $("#previewPick").value; const box = $("#previewBody"); const url = path && previewUrl(path);
  if (!url) { box.innerHTML = '<div class="empty-note">Files this chat creates or edits appear here. HTML pages run in a sandbox with no access to the app.</div>'; return; }
  const ext = path.split(".").pop().toLowerCase();
  if (["html", "htm", "svg", "pdf"].includes(ext)) { box.innerHTML = `<iframe class="preview-frame" src="${esc(url)}" title="Preview"></iframe>`; return; }  // isolated by the server: Content-Security-Policy sandbox (see server.PREVIEW_CSP)
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico"].includes(ext)) { box.innerHTML = `<div class="preview-img"><img src="${esc(url)}" alt=""></div>`; return; }
  try {
    const r = await fetch(url); const text = await r.text();
    box.innerHTML = ext === "md" || ext === "markdown" ? `<div class="content preview-md">${renderMarkdown(text)}</div>` : `<pre class="preview-text">${esc(text.slice(0, 4e5))}</pre>`;
  } catch (e) { box.innerHTML = `<div class="empty-note">${esc(e.message)}</div>`; }
}
$("#previewPick").addEventListener("change", showPreview); $("#previewReload").addEventListener("click", showPreview);
$("#tabTerminal").addEventListener("click", () => setTab("terminal")); $("#tabPreview").addEventListener("click", () => setTab("preview"));
$("#panelClose").addEventListener("click", () => togglePanel(false));
addEventListener("keydown", (e) => { if (e.ctrlKey && e.key.toLowerCase() === "j") { e.preventDefault(); togglePanel(); } });
function onChatChanged() { panel.lines = []; renderTerminal(); refreshPanel(); }

// ---------------------------------------------------------------- export as Markdown
function chatMarkdown(chat) {
  const lines = [`# ${chat.title || "Chat"}`, "", `*Exported from Open AY Desktop on ${new Date().toLocaleString()} · model ${chat.model || ""}*`, ""];
  if (chat.compaction?.summary) lines.push("> Earlier messages were summarized:", ...chat.compaction.summary.split("\n").map((l) => "> " + l), "");
  for (const m of chat.messages || []) {
    if (m.role === "user" && !m.auto) {
      lines.push("## You", "");
      for (const a of m.attachments || []) lines.push(`*Attached: ${a.name}*`);
      lines.push(m.content || "", "");
    } else if (m.role === "assistant" && !m.tool_calls?.length && (m.content || m.error)) {
      lines.push(`## ${m.model || "Assistant"}`, "", m.content || `*Error: ${m.error}*`, "");
    } else if (m.role === "tool") {
      lines.push(`*${TOOL_LABEL[m.tool_name] || m.tool_name}: ${m.display || ""}*`, "");
    }
  }
  return lines.join("\n");
}
$("#exportBtn").addEventListener("click", async () => {
  if (!state.chat) { toast("Open a chat to export it"); return; }
  const chat = await api("/api/chats/" + state.chat.id).catch(() => state.chat); const md = chatMarkdown(chat);
  if (window.openay?.saveText) { const where = await window.openay.saveText(chat.title || "chat", md); if (where) toast("Saved to " + where); return; }
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
  a.download = (chat.title || "chat").replace(/[\\/:*?"<>|]+/g, " ").trim() + ".md"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
});

// ---------------------------------------------------------------- dictation and quick entry
let listening = false;
$("#micBtn").addEventListener("click", async () => {
  const b = $("#micBtn");
  if (window.openay?.dictate) {
    if (listening) { window.openay.dictate(); return; }  // a second click stops listening
    listening = true; b.classList.add("rec"); b.title = "Listening… click to stop"; toast("Listening… speak, then pause");
    const r = await window.openay.dictate(); listening = false; b.classList.remove("rec"); b.title = "Dictate (Windows speech recognition)";
    if (r.error) toast(r.error); else if (r.text) insertText(r.text); else if (!r.stopped) toast("Didn't catch anything");
    return;
  }
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { toast("Dictation needs the desktop app. In Windows you can also press Win+H in any text box."); return; }
  const rec = new SR(); rec.lang = navigator.language || "en-US"; rec.interimResults = false;
  rec.onresult = (e) => insertText(e.results[0][0].transcript); rec.onerror = (e) => toast("Dictation: " + e.error);
  rec.onend = () => b.classList.remove("rec"); b.classList.add("rec"); rec.start();
});
function insertText(text) {
  const t = $("#input"); const s = t.selectionStart ?? t.value.length;
  const before = t.value.slice(0, s); const pad = before && !/\s$/.test(before) ? " " : "";
  t.value = before + pad + text + t.value.slice(t.selectionEnd ?? s); t.focus(); autoGrow();
}
if (window.openay?.onQuickEntry) window.openay.onQuickEntry(() => { $(".back.open")?.classList.remove("open"); newChat(); $("#input").focus(); });

loadProjects();

// ---------------------------------------------------------------- Settings > Diagnostics (diagnostics.py)
const fmtN = (n) => (n || 0).toLocaleString();
async function showDiagnostics() {
  $("#diagOn").checked = state.config.diagnostics !== false;
  const days = $("#diagDays").value; const body = $("#diagBody"); body.innerHTML = '<div class="help">Loading…</div>';
  let d; try { d = await api("/api/diagnostics?days=" + days); } catch (e) { body.innerHTML = `<div class="menu-note">${esc(e.message)}</div>`; return; }
  if (!d.events) { body.innerHTML = '<div class="empty-note">Nothing recorded yet. Diagnostics start with the next reply.</div>'; return; }
  const t = d.totals;
  const stat = (v, l, title) => `<div class="stat"${title ? ` title="${esc(title)}"` : ""}><div class="v">${v}</div><div class="l">${l}</div></div>`;
  const secs = (s) => s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s}s`;
  const outcomes = Object.entries(t.outcomes || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${esc(DIAG_OUTCOMES[k] || k)} ${fmtN(v)}`).join(" · ");
  // Where input tokens go: one bar per part, longest first (single hue; labels carry identity)
  const parts = Object.entries(d.requestParts || {}).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  const partTotal = parts.reduce((s, [, v]) => s + v, 0) || 1;
  const partRows = parts.map(([k, v]) => `<div class="hbar-row"><span class="hbar-l">${esc(DIAG_PARTS[k] || k)}</span><span class="hbar"><i data-pct="${(v / partTotal * 100).toFixed(1)}"></i></span><span class="hbar-v">${kTokens(v)} · ${Math.round(v / partTotal * 100)}%</span></div>`).join("");
  // Tokens per day: single series, hover for the value
  // one slot per day of the period, ending today, so quiet days show as gaps
  const iso = (dt) => `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
  const daily = [...Array(Number(days))].map((_, i) => { const dt = new Date(); dt.setDate(dt.getDate() - (Number(days) - 1 - i)); const k = iso(dt);
    return [k, d.daily?.[k] || { replies: 0, promptTokens: 0, outputTokens: 0 }]; });
  const maxDay = Math.max(1, ...daily.map(([, x]) => x.promptTokens + x.outputTokens));
  const dayBars = daily.map(([day, x]) => { const v = x.promptTokens + x.outputTokens;
    return `<span class="vbar" title="${esc(day)}: ${fmtN(v)} tokens, ${x.replies} repl${x.replies === 1 ? "y" : "ies"}"><i data-pct="${v ? Math.max(2, v / maxDay * 100).toFixed(1) : 0}"></i></span>`; }).join("");
  const table = (head, rows) => rows.length ? `<table class="diag-table"><thead><tr>${head.map((h, i) => `<th${i ? ' class="num"' : ""}>${h}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c, i) => `<td${i ? ' class="num"' : ""}>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>` : '<div class="help">None yet.</div>';
  const models = Object.entries(d.models || {}).sort((a, b) => b[1].steps - a[1].steps).map(([m, x]) => [esc(m), fmtN(x.replies), fmtN(x.steps), kTokens(x.promptTokens), kTokens(x.outputTokens), kTokens(x.thinkingTokens), x.tokensPerSecond || "–", x.firstTokenSecondsMedian ? x.firstTokenSecondsMedian + "s" : "–", fmtN(x.errorReplies)]);
  const tools = Object.entries(d.tools || {}).sort((a, b) => b[1].resultTokens - a[1].resultTokens).map(([n, x]) => [esc(n), fmtN(x.calls), x.msMedian + " ms", x.msP90 + " ms", kTokens(x.resultTokensAvg), kTokens(x.resultTokens), fmtN(x.errors), fmtN(x.denied)]);
  const approvals = Object.entries(d.approvals || {}).sort((a, b) => b[1].asked - a[1].asked).map(([n, x]) => [esc(n), fmtN(x.asked), fmtN(x.allowed), fmtN(x.asked - x.allowed), secs(x.waitSecondsMedian)]);
  const c = d.compaction || {}; const w = d.window || {};
  const retries = Object.entries(t.retries || {}).map(([k, v]) => `${esc(k)} ×${v}`).join(", ") || "none";
  const errors = Object.entries(t.errors || {}).map(([k, v]) => `${esc(k)} ×${v}`).join(", ") || "none";
  body.innerHTML = `<div class="stat-grid inline diag-stats">
      ${stat(fmtN(t.replies), "Replies" + (t.subtaskReplies ? ` (+${fmtN(t.subtaskReplies)} subtask)` : ""))}
      ${stat(t.stepsPerReply, "Steps per reply")}${stat(kTokens(t.tokensPerReply), "Tokens per reply")}
      ${stat(Math.round(t.cachedShare * 100) + "%", "Input from cache", "Cached input is billed at a much lower rate")}
      ${stat(secs(t.replySecondsMedian), "Typical reply time", `90% of replies finish within ${secs(t.replySecondsP90)}`)}
      ${stat(t.firstTokenSecondsMedian + "s", "Typical wait for first token")}
    </div>
    <div class="help diag-line">Outcomes: ${outcomes || "–"} · Paused at the step limit: ${fmtN(t.stepLimitHits)} · Retries: ${retries} · Messages sent while a reply ran: ${fmtN(t.messagesWhileRunning)} · Errors: ${errors}</div>
    <div class="card"><div class="card-t">Where input tokens go</div><div class="help">Estimated from the size of each part of every request. Total sent: ${kTokens(t.promptTokens)}, of which ${kTokens(t.cachedTokens)} came from the cache.</div><div class="hbars">${partRows}</div></div>
    <div class="card"><div class="card-t">Tokens per day</div><div class="vbars" role="img" aria-label="Tokens sent and received per day">${dayBars}</div><div class="vbar-axis"><span>${esc(daily[0]?.[0] || "")}</span><span>max ${kTokens(maxDay)}</span><span>${esc(daily.at(-1)?.[0] || "")}</span></div></div>
    <div class="card"><div class="card-t">Models</div>${table(["Model", "Replies", "Steps", "Input", "Output", "Thinking", "Tokens/s", "First token", "Errors"], models)}</div>
    <div class="card"><div class="card-t">Tools</div><div class="help">Sorted by how many tokens their results added to later requests.</div>${table(["Tool", "Calls", "Median", "Slowest 10%", "Result avg", "Result total", "Errors", "Denied"], tools)}</div>
    <div class="card"><div class="card-t">Approvals</div>${table(["For", "Asked", "Allowed", "Denied", "Typical wait"], approvals)}</div>
    <div class="card"><div class="card-t">Summaries and the window</div><div class="help">Summaries of older messages: ${fmtN(c.count)} (${fmtN(c.manual)} from Compact, ${fmtN(c.failed)} failed), ${kTokens(c.tokensIn)} summarized into ${kTokens(c.tokensOut)}.<br>Chat redraws: ${fmtN(w.renders)}, ${w.renderMsAvg} ms on average, slowest ${Math.round(w.renderMsMax)} ms; frames that blocked the window for over 50 ms: ${fmtN(w.longFrames)}.</div></div>`;
  body.querySelectorAll("[data-pct]").forEach((el) => el.style.setProperty("--pct", el.dataset.pct + "%"));  // the page's CSP blocks inline styles
}
const DIAG_OUTCOMES = { done: "finished", stopped: "stopped", stepLimit: "step limit", tokenLimit: "token limit", noKey: "no API key", unreachable: "Ollama unreachable", broken: "connection dropped", unexpected: "unexpected error", modelError: "Ollama error" };
const DIAG_PARTS = { systemTokens: "Instructions (system prompt, skills, memory)", toolSpecTokens: "Tool definitions", assistantTokens: "Model's earlier replies and tool calls",
  toolResultTokens: "Tool results", userTokens: "Your messages", summaryTokens: "Summary of older messages" };
$("#diagDays").addEventListener("change", showDiagnostics);
$("#diagOn").addEventListener("change", async (e) => {
  try { state.config = await api("/api/config", { method: "POST", body: JSON.stringify({ diagnostics: e.target.checked }) }); toast(e.target.checked ? "Recording diagnostics" : "Diagnostics recording is off"); }
  catch (err) { toast(err.message); e.target.checked = !e.target.checked; }
});
$("#diagClear").addEventListener("click", async () => {
  if (!confirm("Delete all recorded diagnostics? This can't be undone.")) return;
  try { const r = await api("/api/diagnostics/clear", { method: "POST", body: "{}" }); toast(`Cleared ${r.removed} day${r.removed === 1 ? "" : "s"} of diagnostics`); showDiagnostics(); } catch (e) { toast(e.message); }
});
$("#diagExport").addEventListener("click", async () => {
  let report; try { report = await api("/api/diagnostics/export?days=" + $("#diagDays").value); } catch (e) { toast(e.message); return; }
  const text = JSON.stringify(report, null, 1); const name = "open-ay-diagnostics-" + new Date().toISOString().slice(0, 10);
  if (window.openay?.saveText) { const where = await window.openay.saveText(name, text, "json"); if (where) toast("Saved to " + where); return; }
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type: "application/json" })); a.download = name + ".json"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
});
