# One-time setup of the offline Windows demo: Python environment, database, synthetic staff and
# the synthetic policy documents. Safe to run again; existing data in var\windows-local is kept.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\env.ps1"
Set-Location $KbRoot

if (-not (Test-Path $KbPython)) {
    Write-Host 'Creating Python 3.11 virtual environment in .venv'
    py -3.11 -m venv .venv
}
& $KbPython -m pip install --upgrade pip
& $KbPython -m pip install -r requirements.txt pdfplumber
& $KbPython -m kb.cli init --demo-documents
Write-Host ''
Write-Host 'Setup done. Start KBase with:  .\scripts\windows\run.ps1'
Write-Host 'The synthetic documents wait for a checker. Sign in as a checker (for example E002) to approve them.'
