param(
    [Parameter(Position=0)]
    [ValidateSet('doctor','collect','report','bot','tray')]
    [string]$Command = 'doctor'
)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
& .\.venv\Scripts\python.exe -m options_radar --config config.yaml $Command
