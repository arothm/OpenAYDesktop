"use strict";
// What the page can reach from Electron: the folder picker, saving an exported chat, dictation,
// the quick-entry shortcut, running in the background and software updates. No Node, files or shell beyond these.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("openay", {
  isElectron: true,
  pickFolder: () => ipcRenderer.invoke("pick-folder"),
  saveText: (name, text, ext) => ipcRenderer.invoke("save-text", String(name), String(text), ext === "json" ? "json" : "md"),
  dictate: () => ipcRenderer.invoke("dictate"),
  setQuickEntry: (on) => ipcRenderer.invoke("quick-entry", Boolean(on)),
  setBackground: (opts) => ipcRenderer.invoke("background", { keepInTray: Boolean(opts.keepInTray), startAtLogin: Boolean(opts.startAtLogin) }),
  onQuickEntry: (fn) => ipcRenderer.on("quick-entry", () => fn()),
  updates: {
    state: () => ipcRenderer.invoke("update-state"),
    check: () => ipcRenderer.invoke("update-check"),
    download: () => ipcRenderer.invoke("update-download"),
    install: () => ipcRenderer.invoke("update-install"),
    setAuto: (on) => ipcRenderer.invoke("update-auto", Boolean(on)),
    onStatus: (fn) => ipcRenderer.on("update-status", (_e, s) => fn(s)),
  },
});
