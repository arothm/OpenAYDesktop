"use strict";
// Open AY Desktop: Electron shell.
//
// The Python server (server.py) does all the work: chats, replies, tools, approvals, balance.
// This file only:
//   - starts that server on a free local port (or reuses one already running from this folder),
//   - shows it in a locked-down window (no Node in the page; navigation stays on the app),
//   - keeps replies running from the tray when the window closes, if you choose,
//   - shuts the server down when you quit. The server also exits by itself if this process dies,
//     because it watches its stdin (--exit-with-stdin),
//   - installs updates from GitHub Releases (electron-updater), as allowed in Settings,
//   - registers the quick-entry shortcut (Ctrl+Alt+Space), dictation (Windows speech
//     recognition) and saving exported chats.
const { app, BrowserWindow, Tray, Menu, dialog, shell, ipcMain, nativeImage, nativeTheme, session, globalShortcut } = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");

const SMOKE = process.argv.includes("--smoke");          // npm run smoke: start, load, report, quit
const DEV = !app.isPackaged;
const SERVER_DIR = DEV ? path.join(__dirname, "..") : path.join(process.resourcesPath, "app-server");
const ICON = path.join(__dirname, "icon.png");
const TRAY_ICON = path.join(__dirname, "tray.png");  // simplified arch: the eyes blur at 16 px
const APP_SLUG = "open-ay-desktop";

let server = null;     // the Python process, when this shell started it
let origin = "";       // http://127.0.0.1:<port>
let win = null;
let tray = null;
let quitting = false;
let serverLog = "";
let pythonUsed = "";

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());
  app.whenReady().then(start);
}

async function start() {
  session.defaultSession.setPermissionRequestHandler((_wc, permission, allow) => allow(permission === "clipboard-sanitized-write"));
  ipcMain.handle("pick-folder", async () => {
    const r = await dialog.showOpenDialog(win, { title: "Choose a folder to share with this chat", properties: ["openDirectory"] });
    return r.canceled ? "" : r.filePaths[0];
  });
  ipcMain.handle("save-text", (_e, name, text) => saveText(name, text));
  ipcMain.handle("dictate", () => dictate());
  ipcMain.handle("quick-entry", (_e, on) => setQuickEntry(Boolean(on)));
  ipcMain.handle("update-check", () => updates.check(true));
  ipcMain.handle("update-download", () => updates.download());
  ipcMain.handle("update-install", () => updates.install());
  ipcMain.handle("update-auto", (_e, on) => updates.setAuto(Boolean(on)));
  ipcMain.handle("update-state", () => updates.state);
  try {
    origin = (DEV && (await findRunningServer())) || (await startServer());
    createWindow();
    const cfg = await fetch(origin + "/api/config").then((r) => r.json()).catch(() => ({}));
    setQuickEntry(cfg.quickEntry !== false);
    updates.start(cfg.autoUpdate !== false);
  } catch (e) {
    dialog.showErrorBox("Open AY Desktop", String(e.message || e));
    app.exit(1);
  }
}

// ---------------------------------------------------------------- the Python server
function findPython() {
  // Order: OPENAY_PYTHON (if set), the Python bundled with the installer (scripts/fetch-python.js),
  // then a system install. pythonw avoids a console window.
  const bundled = DEV ? null : path.join(process.resourcesPath, "python", "pythonw.exe");
  const candidates = [process.env.OPENAY_PYTHON, bundled && fs.existsSync(bundled) ? bundled : null, "pythonw", "python", "py"].filter(Boolean);
  for (const cmd of candidates) {
    const pre = cmd === "py" ? ["-3"] : [];
    const r = spawnSync(cmd, [...pre, "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"], { windowsHide: true });
    if (r.status === 0) return { cmd, pre };
  }
  return null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer(); s.unref(); s.on("error", reject);
    s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

async function info(base) {
  try { const r = await fetch(base + "/api/info", { signal: AbortSignal.timeout(800) }); return r.ok ? await r.json() : null; } catch { return null; }
}

async function findRunningServer() {
  // In development the repo's data/ folder is shared with the Edge launcher, so never run two servers on it.
  for (let port = 8765; port < 8775; port++) {
    const i = await info(`http://127.0.0.1:${port}`);
    if (i && i.app === APP_SLUG && path.resolve(i.dir).toLowerCase() === path.resolve(SERVER_DIR).toLowerCase()) return `http://127.0.0.1:${port}`;
  }
  return "";
}

async function startServer() {
  const py = findPython();
  if (!py) throw new Error("Python 3.9 or newer was not found.\n\nInstall it from python.org and tick \"Add python.exe to PATH\", or set OPENAY_PYTHON to a python.exe path."
    + (DEV ? "" : "\n\nThe installer normally includes Python; reinstalling Open AY Desktop may fix this."));
  pythonUsed = py.cmd;
  const port = await freePort();
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONIOENCODING: "utf-8" };
  if (!DEV) env.OPENAY_DATA_DIR = path.join(app.getPath("userData"), "data");  // Program Files isn't writable
  const args = [...py.pre, path.join(SERVER_DIR, "server.py"), "--no-window", "--stay", "--exit-with-stdin", "--port", String(port)];
  server = spawn(py.cmd, args, { cwd: SERVER_DIR, env, windowsHide: true, stdio: ["pipe", "pipe", "pipe"] });
  const keep = (d) => { serverLog = (serverLog + d).slice(-4000); };
  server.stdout.on("data", keep); server.stderr.on("data", keep);
  server.on("exit", (code) => {
    if (quitting) return;
    dialog.showErrorBox("Open AY Desktop", `The app's server stopped unexpectedly (exit code ${code}).\n\n${serverLog.slice(-1500)}`);
    app.exit(1);
  });
  const base = `http://127.0.0.1:${port}`;
  for (const end = Date.now() + 20000; Date.now() < end;) {
    if (await info(base)) return base;
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`The app's server didn't start.\n\n${serverLog.slice(-1500)}`);
}

// ---------------------------------------------------------------- window and tray
function createWindow() {
  win = new BrowserWindow({
    width: 1200, height: 820, minWidth: 720, minHeight: 520, title: "Open AY Desktop", icon: ICON,
    show: false, backgroundColor: nativeTheme.shouldUseDarkColors ? "#0E0F12" : "#F7F8FA", autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, "preload.js"), contextIsolation: true, sandbox: true, nodeIntegration: false },
  });
  win.removeMenu();
  const external = (url) => { if (/^https:\/\//i.test(url)) shell.openExternal(url); };
  win.webContents.setWindowOpenHandler(({ url }) => { external(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e, url) => { if (!url.startsWith(origin + "/")) { e.preventDefault(); external(url); } });
  win.webContents.on("context-menu", (_e, p) => textMenu(p));  // pages that show their own menu (the sidebar) prevent this
  win.on("close", onClose);
  if (SMOKE) win.webContents.once("did-finish-load", smokeReport);
  else win.once("ready-to-show", () => win.show());
  win.loadURL(origin + "/");
}

// Right-click on text, a text box or a link: the usual Cut / Copy / Paste menu, with spelling fixes
function textMenu(p) {
  const items = [];
  for (const word of (p.dictionarySuggestions || []).slice(0, 5)) items.push({ label: word, click: () => win.webContents.replaceMisspelling(word) });
  if (items.length) items.push({ type: "separator" });
  if (p.isEditable) {
    items.push({ role: "undo", enabled: p.editFlags.canUndo }, { role: "redo", enabled: p.editFlags.canRedo }, { type: "separator" },
               { role: "cut", enabled: p.editFlags.canCut }, { role: "copy", enabled: p.editFlags.canCopy },
               { role: "paste", enabled: p.editFlags.canPaste }, { type: "separator" }, { role: "selectAll" });
  } else if (p.selectionText && p.selectionText.trim()) {
    items.push({ role: "copy" }, { role: "selectAll" });
  }
  if (p.linkURL && /^https?:\/\//i.test(p.linkURL)) {
    if (items.length) items.push({ type: "separator" });
    items.push({ label: "Copy link", click: () => require("electron").clipboard.writeText(p.linkURL) });
  }
  if (items.length) Menu.buildFromTemplate(items).popup({ window: win });
}

function showWindow() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show(); win.focus();
}

function ensureTray() {
  if (tray) return;
  tray = new Tray(nativeImage.createFromPath(TRAY_ICON).resize({ width: 16, height: 16 }));
  tray.setToolTip("Open AY Desktop: replies running in the background");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open Open AY Desktop", click: showWindow },
    { type: "separator" },
    { label: "Stop replies and quit", click: quit },
  ]));
  tray.on("click", showWindow);
}

async function runningReplies() {
  try { const r = await (await fetch(origin + "/api/runs")).json(); return Object.keys(r.runs || {}).length; } catch { return 0; }
}

async function onClose(e) {
  if (quitting) return;
  e.preventDefault();
  const running = await runningReplies();
  if (!running) return quit();
  const { response } = await dialog.showMessageBox(win, {
    type: "question", title: "Open AY Desktop", defaultId: 0, cancelId: 2,
    buttons: ["Keep running in the background", "Stop replies and quit", "Cancel"],
    message: `${running} ${running === 1 ? "reply is" : "replies are"} still running.`,
    detail: "Keep them running from the system tray, or stop them and quit. Stopped replies keep what they did so far.",
  });
  if (response === 0) { win.hide(); ensureTray(); }
  else if (response === 1) quit();
}

async function quit(then) {
  if (quitting) return;
  quitting = true;
  globalShortcut.unregisterAll();
  if (server) {  // only shut down a server this shell started
    try { await fetch(origin + "/api/shutdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", signal: AbortSignal.timeout(8000) }); } catch {}
    await new Promise((resolve) => {
      if (server.exitCode !== null) return resolve();
      const t = setTimeout(() => { try { server.kill(); } catch {} resolve(); }, 6000);
      server.once("exit", () => { clearTimeout(t); resolve(); });
    });
  }
  if (tray) tray.destroy();
  if (typeof then === "function") return then();  // e.g. install a downloaded update once the server has stopped
  app.exit(0);
}

app.on("before-quit", (e) => { if (!quitting) { e.preventDefault(); quit(); } });

// ---------------------------------------------------------------- software updates (GitHub Releases)
// Checks at start and every 6 hours. With "Install updates automatically" on (Settings), a new
// version downloads in the background and installs when the app quits; otherwise the window
// offers Download and Restart. The window gets every state change on "update-status".
//
// Where to look: first the newest release's latest.yml, straight from its download link. That
// skips GitHub's "latest release" web page, which the built-in GitHub feed reads first and which
// sometimes fails on GitHub's side (504) while downloads still work. If the direct link fails,
// the same check is tried once through the GitHub feed.
const UPDATE_FEEDS = [
  { provider: "generic", url: "https://github.com/arothm/OpenAYDesktop/releases/latest/download" },
  { provider: "github", owner: "arothm", repo: "OpenAYDesktop", releaseType: "release" },
];
const updates = {
  state: { state: DEV ? "dev" : "idle", version: app.getVersion() },
  updater: null,
  quiet: false,  // true while a feed that has a fallback is being tried: its error isn't shown
  send(patch) {
    this.state = { ...this.state, ...patch, version: app.getVersion() };
    if (win && !win.isDestroyed()) win.webContents.send("update-status", this.state);
  },
  start(auto) {
    if (DEV || SMOKE) return;
    try { this.updater = require("electron-updater").autoUpdater; } catch (e) { this.send({ state: "error", error: String(e.message || e) }); return; }
    const u = this.updater;
    u.autoDownload = auto; u.autoInstallOnAppQuit = true; u.logger = null;
    u.on("checking-for-update", () => this.send({ state: "checking", error: "" }));
    u.on("update-available", (i) => this.send({ state: u.autoDownload ? "downloading" : "available", latest: i.version, percent: 0 }));
    u.on("update-not-available", () => this.send({ state: "current", checkedAt: Date.now() }));
    u.on("download-progress", (p) => this.send({ state: "downloading", percent: Math.round(p.percent || 0) }));
    u.on("update-downloaded", (i) => this.send({ state: "ready", latest: i.version }));
    u.on("error", (e) => { if (!this.quiet) this.send({ state: "error", error: String((e && e.message) || e).split("\n")[0].slice(0, 300) }); });
    setTimeout(() => this.check(false), 15000);
    setInterval(() => this.check(false), 6 * 3600 * 1000);
  },
  async check(manual) {
    if (!this.updater || ["downloading", "ready"].includes(this.state.state)) return this.state;
    for (let i = 0; i < UPDATE_FEEDS.length; i++) {
      const last = i === UPDATE_FEEDS.length - 1;
      this.updater.setFeedURL(UPDATE_FEEDS[i]);
      this.quiet = !last;
      try { await this.updater.checkForUpdates(); this.quiet = false; return this.state; }  // a download uses the feed that answered
      catch (e) { this.quiet = false; if (last && manual) this.send({ state: "error", error: String(e.message || e).split("\n")[0].slice(0, 300) }); }
    }
    return this.state;
  },
  async download() {
    if (!this.updater || this.state.state !== "available") return this.state;
    this.send({ state: "downloading", percent: 0 });
    try { await this.updater.downloadUpdate(); } catch (e) { this.send({ state: "error", error: String(e.message || e).slice(0, 300) }); }
    return this.state;
  },
  install() {
    if (!this.updater || this.state.state !== "ready") return false;
    // Stop the server first (the installer replaces its files), then restart into the new version.
    quit(() => this.updater.quitAndInstall(true, true));
    return true;
  },
  setAuto(on) { if (this.updater) this.updater.autoDownload = on; return on; },
};

// ---------------------------------------------------------------- quick entry, dictation, export
function setQuickEntry(on) {
  globalShortcut.unregister("Control+Alt+Space");
  if (!on || SMOKE) return false;
  return globalShortcut.register("Control+Alt+Space", () => {
    showWindow();
    if (win) win.webContents.send("quick-entry");
  });
}

// Dictation uses Windows' own speech recognition (System.Speech), offline: it listens until you
// pause, then returns the text. It needs a Windows speech language and microphone access for desktop apps.
const DICTATE_PS = [
  "$ErrorActionPreference = 'Stop'",
  "Add-Type -AssemblyName System.Speech",
  "$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine",
  "$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))",
  "$r.SetInputToDefaultAudioDevice()",
  "$r.InitialSilenceTimeout = [TimeSpan]::FromSeconds(6)",
  "$r.EndSilenceTimeout = [TimeSpan]::FromSeconds(1.2)",
  "$res = $r.Recognize([TimeSpan]::FromSeconds(30))",
  "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
  "if ($res) { [Console]::Out.Write($res.Text) }",
].join("; ");
let dictation = null;
function dictate() {
  if (dictation) { try { dictation.kill(); } catch {} dictation = null; return Promise.resolve({ stopped: true }); }
  return new Promise((resolve) => {
    const ps = spawn("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", DICTATE_PS], { windowsHide: true });
    dictation = ps; let out = "", err = "";
    ps.stdout.on("data", (d) => { out += d; }); ps.stderr.on("data", (d) => { err += d; });
    ps.on("close", (code) => {
      if (dictation !== ps) return resolve({ stopped: true });
      dictation = null;
      if (code === 0) return resolve({ text: out.trim() });
      const first = err.split("\n").find((l) => l.trim()) || "";
      resolve({ error: /recognizer|language|culture/i.test(err) ? "Windows speech recognition isn't set up for a language (Settings > Time & language > Speech)."
        : /audio|device|microphone/i.test(err) ? "No microphone is available, or desktop apps aren't allowed to use it (Settings > Privacy & security > Microphone)."
        : "Dictation didn't work: " + first.slice(0, 200) });
    });
  });
}

async function saveText(name, text) {
  const safe = String(name || "chat").replace(/[\\/:*?"<>|]+/g, " ").trim().slice(0, 120) || "chat";
  const r = await dialog.showSaveDialog(win, { title: "Export chat", defaultPath: path.join(app.getPath("documents"), safe + ".md"),
                                               filters: [{ name: "Markdown", extensions: ["md"] }] });
  if (r.canceled || !r.filePath) return "";
  await fs.promises.writeFile(r.filePath, String(text || ""), "utf8");
  return r.filePath;
}

// ---------------------------------------------------------------- npm run smoke
async function smokeReport() {
  const page = await win.webContents.executeJavaScript(
    "JSON.stringify({ title: document.title, bridge: Boolean(window.openay && window.openay.isElectron), node: typeof require })");
  const apiInfo = await info(origin);
  process.stdout.write(`SMOKE ${JSON.stringify({ origin, ownServer: Boolean(server), python: pythonUsed, page: JSON.parse(page), server: apiInfo })}\n`);
  quit();
}
