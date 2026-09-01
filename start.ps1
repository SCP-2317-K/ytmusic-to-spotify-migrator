$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

$CodexPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$PythonCommand = if (Get-Command py -ErrorAction SilentlyContinue) {
    "py"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    "python"
} elseif (Test-Path -LiteralPath $CodexPython) {
    $CodexPython
} else {
    throw "Python was not found. Install Python 3.11 or newer and enable Add Python to PATH."
}

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    & $PythonCommand -m venv .venv
}

& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
Write-Host ""
Write-Host "Starting tool at http://127.0.0.1:8787" -ForegroundColor Green
Start-Process "http://127.0.0.1:8787"
& ".venv\Scripts\python.exe" app.py
