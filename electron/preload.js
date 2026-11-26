"use strict";
// The only things the page can reach from Electron: the native folder picker.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("openay", {
  isElectron: true,
  pickFolder: () => ipcRenderer.invoke("pick-folder"),
});
