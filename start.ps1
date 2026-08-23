<#
.SYNOPSIS
    AI Finance Controller — single-command boot script.

.DESCRIPTION
    Starts the FastAPI backend (uvicorn) and the Vite frontend dev server
    in separate PowerShell windows, then opens the app in the default browser.

.NOTES
    Prerequisites
    - Python 3.11+ on PATH
    - Node.js 18+ on PATH
    - backend\.env file populated (copy from .env.example)
    - backend\.venv exists (or will be created by this script)
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot

# ── 1. Backend: create / reuse venv ──────────────────────────────────────────
Write-Host "`n[1/4] Checking Python virtual environment..." -ForegroundColor Cyan
$venvPath = Join-Path $root "backend\.venv"
if (-not (Test-Path $venvPath)) {
    Write-Host "      Creating .venv (Python 3.11+)..." -ForegroundColor Yellow
    python -m venv $venvPath
}

$pip = Join-Path $venvPath "Scripts\pip.exe"
$uvicorn = Join-Path $venvPath "Scripts\uvicorn.exe"

Write-Host "[2/4] Installing backend dependencies..." -ForegroundColor Cyan
& $pip install -r (Join-Path $root "backend\requirements.txt") --quiet

# ── 2. Backend: start uvicorn in a new window ─────────────────────────────────
Write-Host "[3/4] Starting FastAPI backend on http://localhost:8000 ..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "& '$uvicorn' backend.main:app --reload --host 0.0.0.0 --port 8000"
) -WorkingDirectory $root

# ── 3. Frontend: install deps and start Vite ──────────────────────────────────
Write-Host "[4/4] Starting Vite dev server on http://localhost:5173 ..." -ForegroundColor Cyan
$frontendDir = Join-Path $root "frontend"

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "cd '$frontendDir'; npm install; npm run dev"
)

# ── 4. Open browser after a short delay ───────────────────────────────────────
Start-Sleep -Seconds 4
Start-Process "http://localhost:5173"

Write-Host "`n✅  Both servers started." -ForegroundColor Green
Write-Host "   Backend  → http://localhost:8000/docs" -ForegroundColor White
Write-Host "   Frontend → http://localhost:5173" -ForegroundColor White
Write-Host "`nPress Ctrl+C in each window to stop.`n" -ForegroundColor DarkGray
