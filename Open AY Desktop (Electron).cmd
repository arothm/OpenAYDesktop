@echo off
rem Opens Open AY Desktop in its Electron window. Run "npm install" in this folder once first.
if not exist "%~dp0node_modules\electron\dist\electron.exe" (
  echo Electron is not installed yet. Open a terminal in this folder and run: npm install
  pause
  exit /b 1
)
start "" "%~dp0node_modules\electron\dist\electron.exe" "%~dp0."
