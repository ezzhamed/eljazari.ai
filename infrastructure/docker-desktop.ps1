<#
.SYNOPSIS
    Start or stop Docker Desktop on Windows without tripping its orphaned-socket bug.

.DESCRIPTION
    Docker Desktop 4.44.3 initialises its Inference manager on every startup,
    regardless of whether the feature is enabled. That step calls remove() on
    %LOCALAPPDATA%\Docker\run\dockerInference before re-creating it. The file is
    an AF_UNIX socket reparse point, and once its owning process dies uncleanly
    Windows refuses every operation on it:

        starting services: initializing Inference manager:
        remove ...\Docker\run\dockerInference:
        The file cannot be accessed by the system.   (error 1920)

    Startup then aborts entirely - one optional feature takes the whole engine
    down, and it stays down until the file is gone. It cannot be deleted,
    renamed, or even queried by fsutil; the reparse tag has no handler once the
    owner is gone. Only a reboot clears it, or renaming the parent directory,
    which touches just the entry in Docker\ and never opens the socket.
    `docker desktop disable model-runner` does NOT prevent this: the manager is
    still constructed before the setting is consulted (verified on 4.44.3).

    Measured on this machine, not assumed: the sockets survive EVERY shutdown,
    including `docker desktop stop`, and are unremovable afterwards in all
    cases. Starting Docker Desktop directly while they exist fails every time.
    So this is not the aftermath of a force-kill or of `wsl --shutdown` - the
    application simply cannot restart itself once it has run.

    Start therefore clears them before every launch; that is the fix, not a
    tidy-up. Stop is a courtesy that shuts the engine down in an orderly way
    before `wsl --shutdown`; it does NOT prevent the sockets from being left
    behind, and nothing this script can do would.

    The real repair is upstream: a Docker Desktop version that does not
    initialise the Inference manager when the feature is off, or does not treat
    a stale socket as fatal. Until then, launch through this script.

.PARAMETER Action
    Start (default) or Stop.

.EXAMPLE
    ./infrastructure/docker-desktop.ps1 Stop      # before `wsl --shutdown`
    ./infrastructure/docker-desktop.ps1 Start
#>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop')]
    [string]$Action = 'Start',

    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = 'Stop'

$DockerExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$RunDir    = Join-Path $env:LOCALAPPDATA 'Docker\run'

function Get-EngineVersion {
    <#
        Returns the server version, or $null when the engine is not answering.

        The redirect and the local preference are both load-bearing on Windows
        PowerShell 5.1: it wraps a native command's stderr in ErrorRecords, so
        with the script-level $ErrorActionPreference = 'Stop' a perfectly normal
        "engine not up yet" message becomes a terminating error and kills the
        very script meant to start it.
    #>
    $ErrorActionPreference = 'SilentlyContinue'
    $out = & docker version --format '{{.Server.Version}}' 2>&1
    if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -First 1).ToString().Trim() }
    return $null
}

function Test-Engine { return $null -ne (Get-EngineVersion) }

function Clear-OrphanedSockets {
    <#
        Rename rather than delete. The sockets themselves cannot be opened at
        all, but renaming their parent only rewrites a directory entry, so it
        succeeds where Remove-Item, del and even fsutil fail with error 1920.
    #>
    if (-not (Test-Path $RunDir)) {
        New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
        return $false
    }

    $leftovers = @(Get-ChildItem $RunDir -Force -ErrorAction SilentlyContinue)
    if ($leftovers.Count -eq 0) { return $false }

    $stale = "run.stale-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    cmd /c "ren `"$RunDir`" `"$stale`"" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not rename $RunDir; Docker may refuse to start."
        return $false
    }
    New-Item -ItemType Directory -Path $RunDir -Force | Out-Null
    Write-Host "  cleared $($leftovers.Count) orphaned socket(s) -> $stale"
    return $true
}

function Remove-StaleDirs {
    # Best effort. The sockets inside stay undeletable until a reboot releases
    # the kernel objects, so a failure here is expected and harmless.
    Get-ChildItem (Split-Path $RunDir) -Directory -Filter 'run.stale-*' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
}

switch ($Action) {
    'Stop' {
        if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
            Write-Host 'Docker Desktop is not running.'
            break
        }
        Write-Host 'Stopping Docker Desktop cleanly...'
        # Orderly engine shutdown so containers are not cut off mid-write. It
        # does not stop the sockets being stranded - measured, they survive this
        # too - so the next launch still has to go through Start.
        $ErrorActionPreference = 'SilentlyContinue'
        & docker desktop stop 2>&1 | Out-Null
        $ErrorActionPreference = 'Stop'

        $deadline = (Get-Date).AddSeconds(60)
        while ((Get-Date) -lt $deadline -and (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
            Start-Sleep -Seconds 2
        }
        if (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue) {
            Write-Warning 'Still running after 60s. Quit it from the tray rather than killing it.'
        } else {
            Write-Host 'Stopped. `wsl --shutdown` is safe now.'
        }
    }

    'Start' {
        if (Test-Engine) { Write-Host 'Engine already up.'; break }

        # Anything left from a previous session is stale by definition: the
        # engine is not running, so nothing owns these sockets.
        Get-Process -Name 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue |
            Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3

        Clear-OrphanedSockets | Out-Null
        Remove-StaleDirs

        Write-Host 'Starting Docker Desktop...'
        Start-Process $DockerExe

        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 5
            $v = Get-EngineVersion
            if ($v) {
                Write-Host "Engine up - server $v"
                exit 0
            }
        }

        Write-Warning "Engine did not come up within ${TimeoutSeconds}s."
        $log = Join-Path $env:LOCALAPPDATA 'Docker\log\host\com.docker.backend.exe.log'
        if (Test-Path $log) {
            $line = Select-String -Path $log -Pattern 'GET /error \(' | Select-Object -Last 1
            if ($line) {
                $msg = [regex]::Match($line.Line, '"error":"(.*?)","message"').Groups[1].Value
                if ($msg) { Write-Host "Last reported error: $msg" }
            }
        }
        exit 1
    }
}
