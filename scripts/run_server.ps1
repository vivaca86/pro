param(
    [int]$Port = 8012,
    [string]$HostAddress = "127.0.0.1",
    [switch]$AllowLan
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = "python"

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
}

$SitePackages = Join-Path $Root ".venv\Lib\site-packages"
$PythonPathParts = @($Root, $SitePackages)
if ($env:PYTHONPATH) {
    $PythonPathParts += $env:PYTHONPATH
}
$env:PYTHONPATH = ($PythonPathParts -join ";")
if (-not $env:APP_DATA_DIR) {
    $env:APP_DATA_DIR = Join-Path $Root "data"
}
if (-not $env:APP_OUTPUT_DIR) {
    $env:APP_OUTPUT_DIR = Join-Path $Root "data\output"
}

Set-Location -LiteralPath $Root
if ($AllowLan) {
    & $Python (Join-Path $Root "app_server.py") $Port --allow-lan
} else {
    & $Python (Join-Path $Root "app_server.py") $Port --host $HostAddress
}
exit $LASTEXITCODE
