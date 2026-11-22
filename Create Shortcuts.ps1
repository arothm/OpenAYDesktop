# Creates "Open AY Desktop" shortcuts on the Desktop and in the Start menu.
# Removes matching legacy "Ollama Desk" shortcuts during the one-time rename.
# Run from this folder: powershell -ExecutionPolicy Bypass -File ".\Create Shortcuts.ps1"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = (Get-Command pythonw -ErrorAction Stop).Source
$edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
$shell = New-Object -ComObject WScript.Shell
$targets = @(
    [Environment]::GetFolderPath("Desktop"),
    (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs")
)
foreach ($dir in $targets) {
    $legacy = Join-Path $dir "Ollama Desk.lnk"
    if (Test-Path $legacy) {
        Remove-Item -LiteralPath $legacy -Force
        Write-Host "Removed legacy shortcut $legacy"
    }
    $lnk = $shell.CreateShortcut((Join-Path $dir "Open AY Desktop.lnk"))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "`"$here\server.py`""
    $lnk.WorkingDirectory = $here
    $lnk.Description = "Chat with Ollama Cloud models in Open AY Desktop"
    if (Test-Path $edge) { $lnk.IconLocation = "$edge,0" }
    $lnk.Save()
    Write-Host "Created $dir\Open AY Desktop.lnk"
}
