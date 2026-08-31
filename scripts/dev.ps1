# dev.ps1 — boot the local dev stack with one command
# Usage (from the repo root):
#   .\scripts\dev.ps1            # boots both, Ctrl-C stops the watcher
#   .\scripts\dev.ps1 -Backend   # just the backend
#   .\scripts\dev.ps1 -Frontend  # just the frontend
#   .\scripts\dev.ps1 -Stop      # kill any running dev processes
#   .\scripts\dev.ps1 -Status    # show what's running + how to tail logs
[CmdletBinding()]
param(
    [switch] $Backend,
    [switch] $Frontend,
    [switch] $Stop,
    [switch] $Status
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path "$PSScriptRoot/..").Path
$BackendDir = Join-Path $RepoRoot 'backend'
$FrontendDir = Join-Path $RepoRoot 'frontend'
$LogDir = Join-Path $RepoRoot '.dev-logs'
$Null = New-Item -ItemType Directory -Force -Path $LogDir

$BackendPort = 8080
$FrontendPort = 5173

# ───────────────────────── helpers ──────────────────────────────────────────
function Test-Port {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$conn
}

function Stop-DevStack {
    Write-Host "==> Stopping agentops dev stack..." -ForegroundColor Yellow
    Get-Process -Name 'python' -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*webhooks.server*' } |
        ForEach-Object {
            Write-Host "  killing PID $($_.Id) (backend)"
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    Get-Process -Name 'node' -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*vite*' } |
        ForEach-Object {
            Write-Host "  killing PID $($_.Id) (frontend)"
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    Write-Host "  done." -ForegroundColor Green
}

function Get-DevStatus {
    $backend = Test-Port -Port $BackendPort
    $frontend = Test-Port -Port $FrontendPort
    Write-Host ""
    Write-Host "  Backend  (port $BackendPort):  $(if ($backend) {'UP   -> http://localhost:' + $BackendPort} else {'down'})"
    Write-Host "  Frontend (port $FrontendPort): $(if ($frontend) {'UP   -> http://localhost:' + $FrontendPort} else {'down'})"
    Write-Host ""
    Write-Host "  Logs:"
    Write-Host "    backend  : $LogDir\backend.log"
    Write-Host "    frontend : $LogDir\frontend.log"
    Write-Host "  Tail both:  Get-Content '$LogDir\backend.log','$LogDir\frontend.log' -Wait"
    Write-Host ""
}

function Start-Backend {
    $venvPython = Join-Path $BackendDir '.venv/Scripts/python.exe'
    if (-not (Test-Path $venvPython)) {
        Write-Error "No backend venv at $venvPython. Create one with: cd backend; python -m venv .venv; .venv/Scripts/python.exe -m pip install -r requirements.txt"
    }
    # Use a known dev password so the user can log in. (The server.py
    # honors BACKEND_DEV_PASSWORD; if unset, it generates a random one
    # and prints it to the log. We set it here so the dev loop is
    # predictable.)
    $env:BACKEND_DEV_PASSWORD = if ($env:BACKEND_DEV_PASSWORD) { $env:BACKEND_DEV_PASSWORD } else { 'dev123!' }
    $env:PYTHONUNBUFFERED = '1'

    # If the default admin user already exists (from a previous run with
    # a random password), reset their password so the dev credentials
    # stay in sync. This is dev-only. We write the script to a temp
    # file rather than passing via -c (PowerShell here-strings carry
    # their leading whitespace into the script, which breaks Python
    # indentation).
    $devScriptPath = Join-Path $LogDir '_dev_reset_password.py'
    @'
import os, sqlite3, bcrypt
db = r"C:/Users/W3jde/local-projects/w3j-projects/telnyx/agentops-platform/backend/webhooks/agentops.db"
new_pwd = os.environ.get("BACKEND_DEV_PASSWORD", "dev123!").encode("utf-8")
try:
    con = sqlite3.connect(db)
    cur = con.execute(
        "SELECT id, password_hash FROM users "
        "WHERE tenant_id='default' AND email='admin@default.local'"
    )
    row = cur.fetchone()
    if row:
        new_hash = bcrypt.hashpw(new_pwd, bcrypt.gensalt(rounds=12)).decode("utf-8")
        con.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (new_hash, row[0]),
        )
        con.commit()
        print("dev-reset: admin@default.local password synced to BACKEND_DEV_PASSWORD")
    else:
        print("dev-reset: no existing admin user (server will seed on first boot)")
    con.close()
except Exception as e:
    print("dev-reset: skipped (", e, ")")
'@ | Set-Content -Path $devScriptPath -Encoding UTF8
    & $venvPython $devScriptPath 2>&1 | ForEach-Object { Write-Host "  $_" }

    Write-Host "==> Starting backend on port $BackendPort (logs: $LogDir\backend.log)" -ForegroundColor Cyan
    $arg = '-m webhooks.server --host 127.0.0.1 --port ' + $BackendPort + ' --reload'
    $proc = Start-Process `
        -FilePath $venvPython `
        -ArgumentList $arg `
        -WorkingDirectory $BackendDir `
        -RedirectStandardOutput (Join-Path $LogDir 'backend.log') `
        -RedirectStandardError  (Join-Path $LogDir 'backend.err.log') `
        -PassThru -NoNewWindow
    Write-Host "  backend PID: $($proc.Id)"
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Port -Port $BackendPort) {
            Write-Host "  backend up on http://localhost:$BackendPort" -ForegroundColor Green
            return
        }
    }
    Write-Warning "  backend didn't open port $BackendPort within 15s. Tail: $LogDir\backend.log"
}

function Start-Frontend {
    if (-not (Test-Path (Join-Path $FrontendDir 'node_modules'))) {
        Write-Host "==> First run: installing frontend deps..." -ForegroundColor Cyan
        Push-Location $FrontendDir
        try { npm install } catch { Write-Error "npm install failed: $_" }
        Pop-Location
    }
    Write-Host "==> Starting frontend on port $FrontendPort (logs: $LogDir\frontend.log)" -ForegroundColor Cyan
    # On Windows, `npm` is a `.cmd` shim. `Start-Process -FilePath 'npm'`
    # fails with "%1 is not a valid Win32 application" because it picks
    # up a 0-byte stub. Use `npm.cmd` explicitly.
    $npmCmd = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
    if (-not $npmCmd) { $npmCmd = 'npm.cmd' }
    $proc = Start-Process `
        -FilePath $npmCmd `
        -ArgumentList 'run','dev','--','--host','127.0.0.1','--port',$FrontendPort `
        -WorkingDirectory $FrontendDir `
        -RedirectStandardOutput (Join-Path $LogDir 'frontend.log') `
        -RedirectStandardError  (Join-Path $LogDir 'frontend.err.log') `
        -PassThru -NoNewWindow
    Write-Host "  frontend PID: $($proc.Id)"
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-Port -Port $FrontendPort) {
            Write-Host "  frontend up on http://localhost:$FrontendPort" -ForegroundColor Green
            return
        }
    }
    Write-Warning "  frontend didn't open port $FrontendPort within 15s. Tail: $LogDir\frontend.log"
}

# ───────────────────────── dispatch ─────────────────────────────────────────
if ($Stop) { Stop-DevStack; return }
if ($Status) { Get-DevStatus; return }

$startBackend = $Backend -or (-not $Frontend)
$startFrontend = $Frontend -or (-not $Backend)

Write-Host ""
Write-Host "  ============================================" -ForegroundColor Magenta
Write-Host "   agentops dev stack" -ForegroundColor Magenta
Write-Host "  ============================================" -ForegroundColor Magenta
Write-Host "  repo: $RepoRoot"
Write-Host ""
Write-Host "  Login at http://localhost:5173  with:" -ForegroundColor Green
Write-Host "    email:    admin@default.local" -ForegroundColor Green
Write-Host "    password: dev123!   (BACKEND_DEV_PASSWORD; change in scripts\dev.ps1)" -ForegroundColor Green
Write-Host ""

if ($startBackend) { Start-Backend }
if ($startFrontend) { Start-Frontend }

Get-DevStatus

Write-Host "  Ctrl-C here to stop the watcher; use '.\scripts\dev.ps1 -Stop' to kill the processes." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Press Enter to exit (the dev processes keep running in the background)..." -ForegroundColor DarkGray
Read-Host | Out-Null
