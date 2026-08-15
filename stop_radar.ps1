# Stop Options Radar panel and free port 8787
$ErrorActionPreference = 'Continue'

try {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'options_radar' }
    $count = @($procs).Count
    if ($count -eq 0) {
        Write-Host 'No running Options Radar panel process found (already stopped).'
    } else {
        foreach ($p in $procs) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host ('Stopped PID ' + $p.ProcessId)
        }
        Write-Host ('Stopped ' + $count + ' process(es).')
    }
} catch {
    Write-Host ('Error: ' + $_.Exception.Message)
}

Start-Sleep -Seconds 2
if (Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host '[!] Port 8787 still in use. Check for leftover processes.'
} else {
    Write-Host 'Port 8787 released. Panel fully stopped.'
}
Write-Host ''
Write-Host 'Note: Futu OpenD still runs as the quote gateway; no need to close it.'
Write-Host 'To restart, double-click the start script in this folder.'
