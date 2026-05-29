param(
    [int]$Port = 8012
)

$ErrorActionPreference = "Stop"
$processes = Get-CimInstance Win32_Process |
    Where-Object {
        $_.Name -like "python*" -and
        $_.CommandLine -like "*app_server.py*" -and
        $_.CommandLine -like "* $Port*"
    }

foreach ($process in $processes) {
    Stop-Process -Id $process.ProcessId -Force
}

Start-Sleep -Milliseconds 300
$remaining = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" }
if ($remaining) {
    Write-Host "Port $Port is still in use by process $($remaining.OwningProcess)."
    exit 1
}

Write-Host "Stopped app_server.py on port $Port."
