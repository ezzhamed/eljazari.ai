$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$sourceStatic = Join-Path $scriptDir ".next\static"
$standaloneWeb = Join-Path $scriptDir ".next\standalone\apps\web"
$standaloneNext = Join-Path $standaloneWeb ".next"
$targetStatic = Join-Path $standaloneNext "static"
$sourcePublic = Join-Path $scriptDir "public"
$targetPublic = Join-Path $standaloneWeb "public"

if (-not (Test-Path $sourceStatic)) {
  throw "Missing .next\static. Run npm run build before starting the standalone server."
}

New-Item -ItemType Directory -Force -Path $standaloneNext | Out-Null
if (-not (Test-Path $targetStatic)) {
  Copy-Item -Path $sourceStatic -Destination $targetStatic -Recurse -Force
} else {
  Copy-Item -Path (Join-Path $sourceStatic "*") -Destination $targetStatic -Recurse -Force
}

if (Test-Path $sourcePublic) {
  New-Item -ItemType Directory -Force -Path $targetPublic | Out-Null
  Copy-Item -Path (Join-Path $sourcePublic "*") -Destination $targetPublic -Recurse -Force -ErrorAction SilentlyContinue
}

$env:PORT = "3000"
$env:HOSTNAME = "127.0.0.1"

$nodePath = (Get-Command node.exe -ErrorAction Stop).Source
& $nodePath ".next\standalone\apps\web\server.js"
