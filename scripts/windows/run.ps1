# Starts KBase on http://127.0.0.1:8000 with the offline Windows settings.
# The app also runs the ingestion worker, so approved uploads are processed while it runs.
param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"
Set-Location $KbRoot
if (-not (Test-Path $KbPython)) { throw 'Run .\scripts\windows\setup.ps1 first.' }
Write-Host "KBase (offline Windows demo) on http://127.0.0.1:$Port  - press Ctrl+C to stop"
& $KbPython -m kb.cli serve --port $Port
