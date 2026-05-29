param(
    [int]$Port = 8012,
    [string]$HostAddress = "127.0.0.1",
    [switch]$AllowLan,
    [string]$LogDir
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if (-not $LogDir) {
    $LogDir = Join-Path $Root "logs"
}
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$StdoutLog = Join-Path $LogDir "server-$Stamp.out.log"
$StderrLog = Join-Path $LogDir "server-$Stamp.err.log"

function Test-PortReady {
    param(
        [string]$Address,
        [int]$TargetPort
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($Address, $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(1000, $false)) {
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

function Write-LogTail {
    param([string]$Path)
    if (Test-Path $Path) {
        Write-Host ""
        Write-Host $Path
        Get-Content -LiteralPath $Path -Tail 80
    }
}

& (Join-Path $PSScriptRoot "stop_server.ps1") -Port $Port

$RunScript = Join-Path $PSScriptRoot "run_server.ps1"
$Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`" -Port $Port -HostAddress `"$HostAddress`""
if ($AllowLan) {
    $Arguments += " -AllowLan"
}
Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList $Arguments `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -PassThru | Out-Null

$ready = $false
for ($attempt = 0; $attempt -lt 120; $attempt++) {
    Start-Sleep -Milliseconds 500
    if (Test-PortReady -Address $HostAddress -TargetPort $Port) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    Write-Host "Server did not start on port $Port."
    Write-Host "Server logs:"
    Write-LogTail -Path $StdoutLog
    Write-LogTail -Path $StderrLog
    exit 1
}

if ($AllowLan) {
    $LanAddress = (Get-NetIPConfiguration |
        Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } |
        Select-Object -ExpandProperty IPv4Address |
        Select-Object -First 1).IPAddress
    Write-Host "Serving Game Data app at http://${LanAddress}:$Port/index.html"
} else {
    Write-Host "Serving Game Data app at http://${HostAddress}:$Port/index.html"
}
Write-Host "Server stdout log: $StdoutLog"
Write-Host "Server stderr log: $StderrLog"
