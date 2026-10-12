<#
.SYNOPSIS
    Start Postgres and Redis for local development, without Docker Desktop.

.DESCRIPTION
    Docker's only job in the daily loop on Windows was these two services.
    Everything else already runs natively: Ollama on the host, the API under
    uvicorn, the web app under next dev, and PX4 SITL inside WSL. Docker Desktop
    was therefore a heavyweight dependency for two stateful processes on
    standard ports - and a fragile one, see docker-desktop.ps1 for the socket
    bug that took it down five times in one day.

    WSL is already required for SITL, so these move there. The versions match
    what compose used, so nothing about the application changes:

        postgres:16-alpine  ->  postgresql-16   (16.15)
        redis:7-alpine      ->  redis-server    (7.0.15)

    WSL2 forwards localhost, so DATABASE_URL and REDIS_URL in .env keep working
    untouched at 127.0.0.1:5432 and 127.0.0.1:6379.

    This does NOT replace Docker for CI or deployment. Images are still built by
    CI on Linux runners, where none of this applies, and compose and k8s remain
    the deployment story. This is about the local loop only.

.NOTES
    The keepalive is not optional. A WSL distro sleeps once nothing is running
    in it, and a sleeping distro refuses connections from Windows - measured:
    `alembic current` failed with WinError 1225 between two commands that each
    worked, purely because the distro dozed off in between. systemd restarts the
    services as soon as the distro wakes, so the data is never at risk, but the
    connection in flight is. Holding one process open keeps the distro, and with
    it localhost forwarding, continuously available.

.EXAMPLE
    ./infrastructure/dev-services.ps1            # start and verify
    ./infrastructure/dev-services.ps1 -Action Status
    ./infrastructure/dev-services.ps1 -Action Stop
#>
[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status')]
    [string]$Action = 'Start',

    [string]$Distro = 'Ubuntu',
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'

function Test-Port {
    param([int]$Port)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $null = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        Start-Sleep -Milliseconds 400
        return $client.Connected -or $client.Client.Connected
    } catch { return $false } finally { $client.Close() }
}

function Test-Services {
    $pg = Test-Port 5432
    $rd = Test-Port 6379
    return [pscustomobject]@{ Postgres = $pg; Redis = $rd; Both = ($pg -and $rd) }
}

function Get-Keepalive {
    # The distro stays awake only while something runs inside it.
    Get-CimInstance Win32_Process -Filter "Name = 'wsl.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*sleep*infinity*' }
}

switch ($Action) {
    'Status' {
        $s = Test-Services
        "postgres 127.0.0.1:5432 : $(if ($s.Postgres) { 'up' } else { 'down' })"
        "redis    127.0.0.1:6379 : $(if ($s.Redis)    { 'up' } else { 'down' })"
        "keepalive               : $(if (Get-Keepalive) { 'running' } else { 'absent' })"
    }

    'Stop' {
        Get-Keepalive | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Host 'Keepalive stopped. The distro will sleep and take the services with it.'
        Write-Host 'Data is untouched: systemd restarts both on the next wake.'
    }

    'Start' {
        if (-not (Get-Keepalive)) {
            Write-Host "Holding the $Distro distro awake..."
            Start-Process wsl -ArgumentList '-d', $Distro, '--exec', 'sleep', 'infinity' -WindowStyle Hidden
        }

        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 3
            $s = Test-Services
            if ($s.Both) {
                Write-Host 'postgres 127.0.0.1:5432  up'
                Write-Host 'redis    127.0.0.1:6379  up'
                Write-Host 'Ready - no Docker required.'
                exit 0
            }
        }

        $s = Test-Services
        Write-Warning "Not ready after ${TimeoutSeconds}s (postgres=$($s.Postgres) redis=$($s.Redis))."
        Write-Host 'Check inside the distro with:'
        Write-Host "  wsl -d $Distro -e systemctl status postgresql@16-main redis-server"
        exit 1
    }
}
