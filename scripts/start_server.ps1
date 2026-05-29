param(
    [int]$Port = 8012,
    [string]$HostAddress = "127.0.0.1",
    [switch]$AllowLan
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

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
    -WindowStyle Hidden

$listener = $null
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 500
    $listener = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        Select-Object -First 1
    if ($listener) {
        break
    }
}

if (-not $listener) {
    Write-Host "Server did not start on port $Port."
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
