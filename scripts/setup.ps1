$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Test-Path '.venv')) {
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e .
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

if (-not (Test-Path 'config.yaml')) { Copy-Item 'config.example.yaml' 'config.yaml' }
if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env' }

Write-Host 'Setup complete. Edit .env and config.yaml, start Futu OpenD, then run .\start.ps1 doctor.'
