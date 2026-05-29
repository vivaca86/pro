param(
    [int]$Port = 8012
)

$ErrorActionPreference = "Stop"

function Test-PortOpen {
    param([int]$TargetPort)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(500, $false)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

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
if (Test-PortOpen -TargetPort $Port) {
    Write-Host "Port $Port is still in use."
    exit 1
}

Write-Host "Stopped app_server.py on port $Port."
