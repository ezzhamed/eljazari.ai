# Run from Windows PowerShell. Uses installed Windows models on the WSL adapter.
[CmdletBinding()]
param([ValidateRange(1, 65535)][int]$Port = 11435)
$ErrorActionPreference = 'Stop'

$addresses = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
    $_.InterfaceAlias -like 'vEthernet (WSL*' -and $_.AddressState -eq 'Preferred'
})
if ($addresses.Count -ne 1) {
    throw 'Start WSL first. Expected exactly one IPv4 address on the WSL virtual adapter.'
}
$bindAddress = $addresses[0].IPAddress
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
if (-not (Test-Path $ollamaExe)) { throw "Ollama not found: $ollamaExe" }
$runtimeDir = Join-Path $env:LOCALAPPDATA 'DronePrompt'
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
$stateFile = Join-Path $runtimeDir 'ollama-wsl.json'
$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)

if ($listeners.Count -gt 0) {
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne $bindAddress) {
        throw "Port $Port is already used by a different listener."
    }
    $process = Get-Process -Id $listeners[0].OwningProcess
    if ($process.Path -ne $ollamaExe -or -not (Test-Path $stateFile)) {
        throw 'Existing listener is not a recorded DronePrompt Ollama process.'
    }
    $state = Get-Content $stateFile -Raw | ConvertFrom-Json
    if ($state.pid -ne $process.Id -or $state.host -ne "${bindAddress}:$Port") {
        throw 'Listener does not match the recorded DronePrompt process.'
    }
} else {
    $previousHost = $env:OLLAMA_HOST
    $previousKeepAlive = $env:OLLAMA_KEEP_ALIVE
    try {
        $env:OLLAMA_HOST = "${bindAddress}:$Port"
        # Release models after requests to limit contention with the desktop app.
        $env:OLLAMA_KEEP_ALIVE = '0'
        $process = Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $runtimeDir 'ollama-wsl.stdout.log') `
            -RedirectStandardError (Join-Path $runtimeDir 'ollama-wsl.stderr.log') -PassThru
        @{pid=$process.Id; host=$env:OLLAMA_HOST} | ConvertTo-Json | Set-Content $stateFile
    } finally {
        $env:OLLAMA_HOST = $previousHost
        $env:OLLAMA_KEEP_ALIVE = $previousKeepAlive
    }
}

$baseUrl = "http://${bindAddress}:$Port"
$ready = $false
for ($attempt = 0; $attempt -lt 15; $attempt++) {
    try {
        $null = Invoke-RestMethod "$baseUrl/api/tags" -TimeoutSec 2
        $ready = $true
        break
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) { throw "Ollama did not become ready. See logs in $runtimeDir" }

$localEnv = Join-Path (Split-Path $PSScriptRoot -Parent) '.env.wsl.local'
$lines = @()
if (Test-Path $localEnv) {
    $lines = @(Get-Content $localEnv | Where-Object { $_ -notmatch '^\s*OLLAMA_BASE_URL\s*=' })
}
$lines += "OLLAMA_BASE_URL=$baseUrl"
[System.IO.File]::WriteAllLines($localEnv, $lines, [System.Text.UTF8Encoding]::new($false))
Write-Output "Ollama ready: $baseUrl (PID $($process.Id))"
Write-Output 'Updated .env.wsl.local. Restart the WSL API to use this address.'
