<#
.SYNOPSIS
    Bring the whole stack up and open the dashboard. Driven by run.cmd.

.DESCRIPTION
    Four things have to be running before the UI is worth opening: Postgres and
    Redis (in WSL - see dev-services.ps1), Ollama, the API, and the web app.
    This starts whatever is missing, waits until each actually answers, and only
    then opens the browser.

    Waiting matters. Opening the browser as soon as `npm run dev` is spawned
    lands on a connection error, because Next needs to compile first - so each
    step here polls for a real response rather than sleeping a guessed interval.

    The API and web servers get their own windows so their logs stay readable
    and Ctrl-C stops one without stopping the other.

.PARAMETER NoBrowser
    Start everything but do not open the browser.

.PARAMETER Stop
    Stop the API and web servers started by this script.
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ApiPort  = 8010
$WebPort  = 3000
$WebUrl   = "http://localhost:$WebPort"

function Write-Step  { param([string]$m) Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok    { param([string]$m) Write-Host "    $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "    $m" -ForegroundColor Yellow }

function Test-Http {
    param([string]$Url, [int]$TimeoutSec = 4)
    try {
        $null = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing
        return $true
    } catch {
        # A 4xx/5xx still means something is listening and answering.
        return $null -ne $_.Exception.Response
    }
}

function Wait-Http {
    param([string]$Url, [string]$Label, [int]$TimeoutSec = 180)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Http $Url) { Write-Ok "$Label ready"; return $true }
        Start-Sleep -Seconds 2
    }
    Write-Warn2 "$Label did not answer within ${TimeoutSec}s"
    return $false
}

# -- Stop ---------------------------------------------------------------------
if ($Stop) {
    Write-Step 'Stopping the servers this script started'

    # Stop by port ownership, not by command line. `npm run dev` is a wrapper:
    # the process actually holding 3000 runs
    # next/dist/server/lib/start-server.js and matching on "next dev" sails
    # straight past it, which is how a Stop reported success while the site
    # stayed up. Whoever holds the port is the server, however it was spawned.
    $stopped = 0
    foreach ($port in @($ApiPort, $WebPort)) {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object {
                $name = (Get-Process -Id $_ -ErrorAction SilentlyContinue).ProcessName
                Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
                Write-Ok "port $port released (pid $_ $name)"
                $stopped++
            }
    }

    # The wrappers survive their child and would otherwise linger in the window.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'node.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'uvicorn main:socket_app|next(\.js)? dev|start-server\.js' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            $stopped++
        }

    if ($stopped -eq 0) { Write-Ok 'nothing was running' }
    Write-Host ''
    Write-Host 'Postgres and Redis are left running in WSL.' -ForegroundColor DarkGray
    Write-Host 'Stop those with: ./infrastructure/dev-services.ps1 -Action Stop' -ForegroundColor DarkGray
    exit 0
}

Write-Host ''
Write-Host '  eljazari.ai' -ForegroundColor White
Write-Host '  Build and test a drone from a prompt' -ForegroundColor DarkGray
Write-Host ''

# -- 1. Postgres + Redis ------------------------------------------------------
Write-Step 'Database and cache (WSL)'
& (Join-Path $PSScriptRoot 'dev-services.ps1') -Action Start | ForEach-Object { Write-Host "    $_" }
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 'Could not start Postgres/Redis - the API will fail to connect.'
    exit 1
}

# -- 2. Ollama ----------------------------------------------------------------
Write-Step 'Ollama'
if (Test-Http "http://127.0.0.1:11434/api/tags") {
    Write-Ok 'already running'
} elseif (Get-Command ollama -ErrorAction SilentlyContinue) {
    Start-Process -WindowStyle Hidden ollama -ArgumentList 'serve'
    if (-not (Wait-Http "http://127.0.0.1:11434/api/tags" 'ollama' 60)) {
        Write-Warn2 'Design and code generation will not work without it.'
    }
} else {
    Write-Warn2 'ollama not installed - design and code generation will not work.'
}

# -- 3. API -------------------------------------------------------------------
Write-Step "API (port $ApiPort)"
if (Test-Http "http://127.0.0.1:$ApiPort/health") {
    Write-Ok 'already running'
} else {
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-NoProfile', '-Command',
        "Set-Location '$(Join-Path $RepoRoot 'apps\api')'; " +
        "`$host.UI.RawUI.WindowTitle = 'eljazari API'; " +
        "python -m uvicorn main:socket_app --host 127.0.0.1 --port $ApiPort"
    )
    if (-not (Wait-Http "http://127.0.0.1:$ApiPort/health" 'api' 180)) {
        Write-Warn2 'Check the API window for the error.'
        exit 1
    }
}

# -- 4. Web -------------------------------------------------------------------
Write-Step "Web (port $WebPort)"
if (Test-Http $WebUrl) {
    Write-Ok 'already running'
} else {
    Start-Process powershell -ArgumentList @(
        '-NoExit', '-NoProfile', '-Command',
        "Set-Location '$(Join-Path $RepoRoot 'apps\web')'; " +
        "`$host.UI.RawUI.WindowTitle = 'eljazari Web'; " +
        "npm run dev -- -p $WebPort"
    )
    # Next compiles on first request, so this is the slowest step by far.
    if (-not (Wait-Http $WebUrl 'web' 300)) {
        Write-Warn2 'Check the Web window for the error.'
        exit 1
    }
}

# -- 5. Open ------------------------------------------------------------------
Write-Host ''
Write-Host "  Ready - $WebUrl" -ForegroundColor Green
Write-Host ''
Write-Host '  API      http://127.0.0.1:8010        (its own window)' -ForegroundColor DarkGray
Write-Host '  Web      http://localhost:3000        (its own window)' -ForegroundColor DarkGray
Write-Host '  Stop     ./run.cmd -Stop' -ForegroundColor DarkGray
Write-Host ''

if (-not $NoBrowser) { Start-Process $WebUrl }
